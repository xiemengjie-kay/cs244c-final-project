#!/usr/bin/env python3
"""Evaluate exact leader Accept->Commit send latency with length sweep support.

Requires paxos_node started with --eval-trace, which emits lines:
  node=<id> phase=<accept_send|commit_send> slot=<slot> t_ns=<steady_ns> cmd=<command>

Metric:
  latency_ms = t_ns(commit_send) - t_ns(accept_send)
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EVAL_RE = re.compile(
    r"node=(\d+)\s+phase=(accept_send|commit_send)\s+slot=(\d+)\s+t_ns=(\d+)\s+cmd=(.*)$"
)

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]


@dataclass
class EvalEvent:
    node_id: int
    phase: str
    slot: int
    t_ns: int
    cmd: str


@dataclass
class NodeProcess:
    node_id: int
    proc: subprocess.Popen
    lines: List[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start_reader(self) -> None:
        def _reader() -> None:
            assert self.proc.stdout is not None
            for raw in self.proc.stdout:
                with self.lock:
                    self.lines.append(raw.rstrip("\n"))

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

    def send_line(self, line: str) -> None:
        if self.proc.poll() is not None:
            recent = "\n".join(self.snapshot()[-40:])
            raise RuntimeError(f"node {self.node_id} exited. Recent logs:\n{recent}")
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def snapshot(self) -> List[str]:
        with self.lock:
            return list(self.lines)


def parse_eval_event(line: str) -> Optional[EvalEvent]:
    m = EVAL_RE.search(line)
    if not m:
        return None
    return EvalEvent(
        node_id=int(m.group(1)),
        phase=m.group(2),
        slot=int(m.group(3)),
        t_ns=int(m.group(4)),
        cmd=m.group(5).strip(),
    )


def percentile(vals: List[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * (p / 100.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    frac = idx - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def summarize(vals: List[float]) -> Dict[str, float]:
    if not vals:
        return {"count": 0.0, "mean": float("nan"), "p50": float("nan"), "p95": float("nan"), "p99": float("nan")}
    return {
        "count": float(len(vals)),
        "mean": statistics.mean(vals),
        "p50": percentile(vals, 50),
        "p95": percentile(vals, 95),
        "p99": percentile(vals, 99),
    }


def cdf_points(vals: List[float]) -> List[Tuple[float, float]]:
    if not vals:
        return []
    s = sorted(vals)
    n = len(s)
    return [(x, (i + 1) / n) for i, x in enumerate(s)]


def start_nodes(binary: Path, base_port: int, node_ids: List[int]) -> Dict[int, NodeProcess]:
    node_spec = ",".join(f"{nid}:127.0.0.1:{base_port + nid}" for nid in node_ids)
    nodes: Dict[int, NodeProcess] = {}
    for nid in node_ids:
        cmd = [str(binary), "--id", str(nid), "--nodes", node_spec, "--eval-trace"]
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        np = NodeProcess(node_id=nid, proc=p)
        np.start_reader()
        nodes[nid] = np
    time.sleep(1.0)
    for nid, np in nodes.items():
        if np.proc.poll() is not None:
            recent = "\n".join(np.snapshot()[-40:])
            raise RuntimeError(f"node {nid} exited during startup. Recent logs:\n{recent}")
    return nodes


def stop_nodes(nodes: Dict[int, NodeProcess]) -> None:
    for np in nodes.values():
        try:
            if np.proc.poll() is None:
                np.send_line("/quit")
        except Exception:
            pass
    time.sleep(0.5)
    for np in nodes.values():
        if np.proc.poll() is None:
            np.proc.terminate()
    for np in nodes.values():
        try:
            np.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            np.proc.kill()


def make_command(length_bytes: int, seq: int, submit_node: int) -> str:
    # Prefix preserves uniqueness/traceability; payload controls message length.
    prefix = f"a2c_len{length_bytes}_cmd{seq:04d}_from{submit_node}:"
    payload = "x" * max(length_bytes, 0)
    return prefix + payload


def parse_lengths(spec: str) -> List[int]:
    vals = []
    for tok in spec.split(","):
        t = tok.strip()
        if not t:
            continue
        vals.append(int(t))
    if not vals:
        raise ValueError("--message-lengths produced empty set")
    return vals


def write_svg(
    out: Path,
    cluster_size: int,
    submit_mode: str,
    lengths: List[int],
    series_by_length: Dict[int, List[float]],
    total_by_length: Dict[int, int],
    timeout_by_length: Dict[int, int],
) -> None:
    width = 1320
    cdf_box = (70, 110, 760, 390)
    stat_box = (930, 140, 350, 350)
    cdf_x0, cdf_y0, cdf_w, cdf_h = cdf_box
    st_x0, st_y0, st_w, st_h = stat_box
    bottom_content = max(cdf_y0 + cdf_h + 46, st_y0 + st_h + 40)
    height = int(bottom_content + 14)

    all_vals: List[float] = []
    for l in lengths:
        all_vals.extend(series_by_length.get(l, []))

    if all_vals:
        cdf_min_x = min(all_vals) - min(all_vals) * 0.05
        cdf_max_x = max(all_vals) * 1.05
    else:
        cdf_min_x = -20.0
        cdf_max_x = 1.0
    if cdf_max_x <= cdf_min_x:
        cdf_max_x = cdf_min_x + 1.0
    svg: List[str] = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    svg.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
    svg.append('<text x="36" y="36" font-size="26" font-family="Arial">Accept->Commit Latency vs Message Length</text>')
    svg.append(
        f'<text x="36" y="64" font-size="14" font-family="Arial">cluster_size={cluster_size} participating nodes, submit_mode={submit_mode}</text>'
    )
    # if submit_mode == "round_robin":
    #     svg.append(
    #         f'<text x="36" y="84" font-size="14" font-family="Arial">submitters rotate 1 -> 2 -> ... -> {cluster_size}</text>'
    #     )

    # CDF panel
    svg.append(f'<rect x="{cdf_x0}" y="{cdf_y0}" width="{cdf_w}" height="{cdf_h}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = cdf_y0 + cdf_h - (i / 5) * cdf_h
        svg.append(f'<line x1="{cdf_x0}" y1="{gy:.1f}" x2="{cdf_x0 + cdf_w}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{cdf_x0 - 28}" y="{gy + 4:.1f}" font-size="10" font-family="Arial">{i/5:.1f}</text>')
    for i in range(6):
        gx = cdf_x0 + (i / 5) * cdf_w
        val = cdf_min_x + (i / 5) * (cdf_max_x - cdf_min_x)
        svg.append(f'<line x1="{gx:.1f}" y1="{cdf_y0}" x2="{gx:.1f}" y2="{cdf_y0 + cdf_h}" stroke="#eee"/>')
        svg.append(f'<text x="{gx - 13:.1f}" y="{cdf_y0 + cdf_h + 18}" font-size="10" font-family="Arial">{val:.2f}</text>')
    svg.append(f'<text x="{cdf_x0}" y="{cdf_y0 - 12}" font-size="15" font-family="Arial">CDF (one line per message length)</text>')
    svg.append(
        f'<text x="{cdf_x0 + cdf_w/2 - 48}" y="{cdf_y0 + cdf_h + 36}" font-size="12" font-family="Arial">latency (ms)</text>'
    )

    sx = cdf_w / (cdf_max_x - cdf_min_x)
    sy = cdf_h

    for idx, length in enumerate(lengths):
        vals = series_by_length.get(length, [])
        pts = cdf_points(vals)
        if not pts:
            continue
        color = PALETTE[idx % len(PALETTE)]
        coords = []
        for x, y in pts:
            px = cdf_x0 + (x - cdf_min_x) * sx
            py = cdf_y0 + cdf_h - y * sy
            coords.append((px, py))
        d = " ".join(("M" if i == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for i, p in enumerate(coords))
        svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')

    # CDF legend in top-left of plotting area.
    legend_x = cdf_x0 + 12
    legend_y = cdf_y0 + 14
    legend_w = min(cdf_w - 24, 250)
    legend_h = 22 + len(lengths) * 18 + 8
    svg.append(
        f'<rect x="{legend_x-6}" y="{legend_y-14}" width="{legend_w}" height="{legend_h}" fill="white" fill-opacity="0.88" stroke="#999"/>'
    )
    svg.append(
        f'<text x="{legend_x}" y="{legend_y}" font-size="11" font-family="Arial">len / successful / timeout</text>'
    )
    for idx, length in enumerate(lengths):
        color = PALETTE[idx % len(PALETTE)]
        succ = len(series_by_length.get(length, []))
        total = total_by_length.get(length, 0)
        tout = timeout_by_length.get(length, 0)
        rate = (tout / total * 100.0) if total > 0 else 0.0
        yy = legend_y + (idx + 1) * 18
        svg.append(f'<line x1="{legend_x}" y1="{yy-4}" x2="{legend_x+20}" y2="{yy-4}" stroke="{color}" stroke-width="2"/>')
        svg.append(
            f'<text x="{legend_x+26}" y="{yy}" font-size="11" font-family="Arial">len={length}B n={succ}/{total} timeout={rate:.1f}%</text>'
        )

    # Stats panel: p50/p95 vs message length + timeout rate text
    svg.append(f'<rect x="{st_x0}" y="{st_y0}" width="{st_w}" height="{st_h}" fill="none" stroke="#333"/>')
    svg.append(f'<text x="{st_x0}" y="{st_y0 - 12}" font-size="15" font-family="Arial">p50/p95 vs message length</text>')
    svg.append(
        f'<text x="{st_x0 + st_w/2 - 60}" y="{st_y0 + st_h + 30}" font-size="11" font-family="Arial">message length (bytes)</text>'
    )
    svg.append(
        f'<text x="{st_x0 - 50}" y="{st_y0 + st_h/2}" font-size="11" font-family="Arial" transform="rotate(-90 {st_x0 - 50},{st_y0 + st_h/2})">latency (ms)</text>'
    )

    p50s = [summarize(series_by_length.get(l, []))["p50"] for l in lengths]
    p95s = [summarize(series_by_length.get(l, []))["p95"] for l in lengths]
    finite_vals = [v for v in p50s + p95s if not math.isnan(v)]
    y_max = max([1.0] + finite_vals) * 1.15

    for i in range(6):
        gy = st_y0 + st_h - (i / 5) * st_h
        val = (i / 5) * y_max
        svg.append(f'<line x1="{st_x0}" y1="{gy:.1f}" x2="{st_x0 + st_w}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{st_x0 - 40}" y="{gy + 4:.1f}" font-size="10" font-family="Arial">{val:.2f}</text>')

    n = len(lengths)
    if n >= 1:
        for i, l in enumerate(lengths):
            gx = st_x0 + (0 if n == 1 else (i / (n - 1)) * st_w)
            svg.append(f'<line x1="{gx:.1f}" y1="{st_y0}" x2="{gx:.1f}" y2="{st_y0 + st_h}" stroke="#f3f3f3"/>')
            svg.append(f'<text x="{gx - 14:.1f}" y="{st_y0 + st_h + 18}" font-size="10" font-family="Arial">{l}</text>')

    def series_path(vals: List[float], color: str) -> None:
        pts = []
        for i, v in enumerate(vals):
            if math.isnan(v):
                continue
            px = st_x0 + (0 if n == 1 else (i / (n - 1)) * st_w)
            py = st_y0 + st_h - (v / y_max) * st_h
            pts.append((px, py))
        if len(pts) < 2:
            return
        d = " ".join(("M" if i == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for i, p in enumerate(pts))
        svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        for px, py in pts:
            svg.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.8" fill="{color}"/>')

    series_path(p50s, "#1f77b4")
    series_path(p95s, "#d62728")

    # Legends
    lgx = st_x0 + 10
    lgy = st_y0 + 18
    svg.append(f'<line x1="{lgx}" y1="{lgy}" x2="{lgx+24}" y2="{lgy}" stroke="#1f77b4" stroke-width="2"/>')
    svg.append(f'<text x="{lgx+30}" y="{lgy+4}" font-size="11" font-family="Arial">p50</text>')
    svg.append(f'<line x1="{lgx+70}" y1="{lgy}" x2="{lgx+94}" y2="{lgy}" stroke="#d62728" stroke-width="2"/>')
    svg.append(f'<text x="{lgx+100}" y="{lgy+4}" font-size="11" font-family="Arial">p95</text>')

    svg.append("</svg>")
    out.write_text("\n".join(svg), encoding="utf-8")


def run_one_length(
    binary: Path,
    base_port: int,
    cluster_size: int,
    submit_mode: str,
    leader_id: int,
    commands: int,
    timeout_ms: int,
    length_bytes: int,
    sweep_idx: int,
) -> List[Dict[str, object]]:
    node_ids = list(range(1, cluster_size + 1))
    cursors: Dict[int, int] = {nid: 0 for nid in node_ids}
    pending_accept: Dict[Tuple[int, int], int] = {}
    rows: List[Dict[str, object]] = []

    nodes: Dict[int, NodeProcess] = {}
    port_base = base_port + sweep_idx * 100
    try:
        nodes = start_nodes(binary, port_base, node_ids)
        for i in range(commands):
            submit_node = leader_id if submit_mode == "leader_only" else node_ids[i % len(node_ids)]
            cmd = make_command(length_bytes, i, submit_node)
            nodes[submit_node].send_line(cmd)

            found = None
            deadline = time.perf_counter() + (timeout_ms / 1000.0)
            while time.perf_counter() < deadline and found is None:
                for nid, np in nodes.items():
                    lines = np.snapshot()
                    start = cursors[nid]
                    for line in lines[start:]:
                        ev = parse_eval_event(line)
                        if ev is None:
                            continue
                        key = (ev.node_id, ev.slot)
                        if ev.phase == "accept_send":
                            pending_accept[key] = ev.t_ns
                            continue
                        if ev.phase == "commit_send":
                            t_accept = pending_accept.get(key)
                            if t_accept is None:
                                continue
                            if ev.t_ns < t_accept:
                                continue
                            if ev.cmd != cmd:
                                continue
                            found = (ev.node_id, ev.slot, t_accept, ev.t_ns)
                            break
                    cursors[nid] = len(lines)
                    if found is not None:
                        break
                if found is None:
                    time.sleep(0.002)

            if found is None:
                rows.append(
                    {
                        "sweep_idx": sweep_idx,
                        "message_length_bytes": length_bytes,
                        "command_length_bytes": len(cmd),
                        "cluster_size": cluster_size,
                        "submit_mode": submit_mode,
                        "command_index": i,
                        "submit_node": submit_node,
                        "leader_node": "",
                        "slot": "",
                        "accept_t_ns": "",
                        "commit_t_ns": "",
                        "latency_ms": "",
                        "status": "timeout",
                    }
                )
                print(f"warning: timeout for len={length_bytes} cmd_idx={i}", file=sys.stderr)
                continue

            leader_node, slot, t_accept, t_commit = found
            latency_ms = (t_commit - t_accept) / 1_000_000.0
            rows.append(
                {
                    "sweep_idx": sweep_idx,
                    "message_length_bytes": length_bytes,
                    "command_length_bytes": len(cmd),
                    "cluster_size": cluster_size,
                    "submit_mode": submit_mode,
                    "command_index": i,
                    "submit_node": submit_node,
                    "leader_node": leader_node,
                    "slot": slot,
                    "accept_t_ns": t_accept,
                    "commit_t_ns": t_commit,
                    "latency_ms": f"{latency_ms:.6f}",
                    "status": "ok",
                }
            )

    finally:
        if nodes:
            stop_nodes(nodes)

    return rows


def aggregate_rows(
    rows: List[Dict[str, object]],
) -> Tuple[int, str, List[int], Dict[int, List[float]], Dict[int, int], Dict[int, int]]:
    if not rows:
        return 0, "", [], {}, {}, {}

    cluster_size = int(rows[0]["cluster_size"])
    submit_mode = str(rows[0]["submit_mode"])
    series_by_length: Dict[int, List[float]] = {}
    total_by_length: Dict[int, int] = {}
    timeout_by_length: Dict[int, int] = {}

    for r in rows:
        length = int(r["message_length_bytes"])
        series_by_length.setdefault(length, [])
        total_by_length[length] = total_by_length.get(length, 0) + 1
        timeout_by_length.setdefault(length, 0)

        status = str(r.get("status", ""))
        lat = str(r.get("latency_ms", "")).strip()
        if status == "ok" and lat:
            series_by_length[length].append(float(lat))
        else:
            timeout_by_length[length] += 1

    lengths = sorted(series_by_length.keys())
    return cluster_size, submit_mode, lengths, series_by_length, total_by_length, timeout_by_length


def write_summary(
    summary_path: Path,
    binary_label: str,
    cluster_size: int,
    submit_mode: str,
    lengths: List[int],
    series_by_length: Dict[int, List[float]],
    total_by_length: Dict[int, int],
    timeout_by_length: Dict[int, int],
) -> None:
    with summary_path.open("w", encoding="utf-8") as f:
        f.write("Exact Leader Accept->Commit Send Latency (Length Sweep)\n")
        f.write("====================================================\n\n")
        f.write("Metric: t(commit_send) - t(accept_send), using internal steady_clock trace.\n")
        f.write(f"Source: {binary_label}\n")
        f.write(f"Cluster size (participating nodes): {cluster_size}\n")
        f.write(f"Submit mode: {submit_mode}\n")
        if submit_mode == "round_robin":
            f.write("Round robin submit order: 1 -> 2 -> ... -> N\n")
        f.write(f"Message lengths (payload bytes): {','.join(str(l) for l in lengths)}\n\n")

        for length in lengths:
            vals = series_by_length[length]
            st = summarize(vals)
            total = total_by_length[length]
            tout = timeout_by_length[length]
            fail_rate = (tout / total * 100.0) if total else 0.0
            f.write(f"Length {length} bytes:\n")
            f.write(f"  successful samples: {int(st['count'])}/{total}\n")
            f.write(f"  timeout/fail rate:  {fail_rate:.2f}%\n")
            f.write(f"  mean: {st['mean']:.3f} ms\n")
            f.write(f"  p50:  {st['p50']:.3f} ms\n")
            f.write(f"  p95:  {st['p95']:.3f} ms\n")
            f.write(f"  p99:  {st['p99']:.3f} ms\n\n")

        f.write("Timeout handling policy:\n")
        f.write("  - Timeout points are kept in CSV with status=timeout.\n")
        f.write("  - They are excluded from percentile calculations.\n")
        f.write("  - They are included in timeout/fail-rate reporting.\n")


def load_rows_from_csv(csv_path: Path) -> List[Dict[str, object]]:
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        out: List[Dict[str, object]] = []
        for row in reader:
            out.append(dict(row))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate exact leader accept_send->commit_send latency")
    ap.add_argument("--binary", default="./build/paxos_node")
    ap.add_argument("--base-port", type=int, default=18000)
    ap.add_argument("--cluster-size", type=int, default=3)
    ap.add_argument("--commands", type=int, default=100)
    ap.add_argument("--timeout-ms", type=int, default=10000)
    ap.add_argument("--submit-mode", choices=["round_robin", "leader_only"], default="round_robin")
    ap.add_argument("--leader-id", type=int, default=1)
    ap.add_argument("--message-lengths", default="32", help="comma-separated payload lengths, e.g. 16,64,256")
    ap.add_argument("--mode", choices=["both", "run", "plot"], default="both")
    ap.add_argument("--input-csv", default="", help="CSV path for --mode plot (default: <out-dir>/accept_to_commit_samples.csv)")
    ap.add_argument("--out-dir", default="eval/eval_accept_to_commit")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / ("accept_to_commit_samples_" + str(args.message_lengths) + "_" + str(args.cluster_size) + "_" + str(args.submit_mode) + ".csv")
    summary_path = out_dir / ("accept_to_commit_summary_" + str(args.message_lengths) + "_" + str(args.cluster_size) + "_" + str(args.submit_mode) + ".txt")
    fig_path = out_dir / ("accept_to_commit_figure_" + str(args.message_lengths) + "_" + str(args.cluster_size) + "_" + str(args.submit_mode) + ".svg")

    all_rows: List[Dict[str, object]] = []

    if args.mode in ("both", "run"):
        binary = Path(args.binary)
        if not binary.exists():
            print(f"binary not found: {binary}", file=sys.stderr)
            return 2

        if args.cluster_size < 3:
            print("cluster_size must be >= 3 for this Paxos setup", file=sys.stderr)
            return 2

        lengths = sorted(parse_lengths(args.message_lengths))
        for sweep_idx, length in enumerate(lengths):
            rows = run_one_length(
                binary=binary,
                base_port=args.base_port,
                cluster_size=args.cluster_size,
                submit_mode=args.submit_mode,
                leader_id=args.leader_id,
                commands=args.commands,
                timeout_ms=args.timeout_ms,
                length_bytes=length,
                sweep_idx=sweep_idx,
            )
            all_rows.extend(rows)

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "sweep_idx",
                    "message_length_bytes",
                    "command_length_bytes",
                    "cluster_size",
                    "submit_mode",
                    "command_index",
                    "submit_node",
                    "leader_node",
                    "slot",
                    "accept_t_ns",
                    "commit_t_ns",
                    "latency_ms",
                    "status",
                ],
            )
            w.writeheader()
            for r in all_rows:
                w.writerow(r)
    else:
        input_csv = Path(args.input_csv) if args.input_csv else csv_path
        if not input_csv.exists():
            print(f"input csv not found: {input_csv}", file=sys.stderr)
            return 2
        all_rows = load_rows_from_csv(input_csv)
        csv_path = input_csv

    if not all_rows:
        print("no rows available to summarize/plot", file=sys.stderr)
        return 2

    cluster_size, submit_mode, lengths, series_by_length, total_by_length, timeout_by_length = aggregate_rows(all_rows)
    source_label = str(Path(args.binary)) if args.mode in ("both", "run") else f"csv:{csv_path}"

    write_summary(
        summary_path=summary_path,
        binary_label=source_label,
        cluster_size=cluster_size,
        submit_mode=submit_mode,
        lengths=lengths,
        series_by_length=series_by_length,
        total_by_length=total_by_length,
        timeout_by_length=timeout_by_length,
    )

    if args.mode in ("both", "plot"):
        write_svg(
            fig_path,
            cluster_size=cluster_size,
            submit_mode=submit_mode,
            lengths=lengths,
            series_by_length=series_by_length,
            total_by_length=total_by_length,
            timeout_by_length=timeout_by_length,
        )

    print(f"samples csv: {csv_path}")
    print(f"summary: {summary_path}")
    if args.mode in ("both", "plot"):
        print(f"figure: {fig_path}")
    else:
        print("figure: (skipped in --mode run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
