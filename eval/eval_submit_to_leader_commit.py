#!/usr/bin/env python3
"""Evaluate end-to-end latency: client submit -> leader commit observed.

This script acts as the client by writing commands to node stdin.
Metric:
  latency_ms = t_when_leader_commit_line_is_observed - t_client_submit
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

STATUS_RE = re.compile(r"node\s+(\d+)\s+leader=(yes|no)\s+ballot=([-\d]+)\s+commit_index=(\d+)")
COMMIT_RE = re.compile(r"node\s+(\d+)\s+committed\s+slot\s+(\d+):\s+(.*)$")
EVAL_COMMIT_RE = re.compile(r"node=(\d+)\s+phase=commit_send\s+slot=(\d+)\s+t_ns=(\d+)\s+cmd=(.*)$")


@dataclass
class NodeProcess:
    node_id: int
    proc: subprocess.Popen
    lines: List[Tuple[float, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start_reader(self) -> None:
        def _reader() -> None:
            assert self.proc.stdout is not None
            for raw in self.proc.stdout:
                ts = time.perf_counter()
                with self.lock:
                    self.lines.append((ts, raw.rstrip("\n")))

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

    def send_line(self, line: str) -> None:
        if self.proc.poll() is not None:
            recent = "\n".join(line for _, line in self.snapshot()[-40:])
            raise RuntimeError(f"node {self.node_id} exited. Recent logs:\n{recent}")
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def snapshot(self) -> List[Tuple[float, str]]:
        with self.lock:
            return list(self.lines)


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


def latest_status(lines: List[Tuple[float, str]]) -> Optional[Tuple[int, bool, int, int]]:
    for _, line in reversed(lines):
        m = STATUS_RE.search(line)
        if m:
            return (int(m.group(1)), m.group(2) == "yes", int(m.group(3)), int(m.group(4)))
    return None


def wait_for_single_leader(nodes: Dict[int, NodeProcess], timeout_s: float = 8.0) -> int:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        for np in nodes.values():
            if np.proc.poll() is not None:
                recent = "\n".join(line for _, line in np.snapshot()[-40:])
                raise RuntimeError(f"node {np.node_id} exited. Recent logs:\n{recent}")
            np.send_line("/status")
        time.sleep(0.15)

        leaders: List[int] = []
        for nid, np in nodes.items():
            st = latest_status(np.snapshot())
            if st and st[1]:
                leaders.append(nid)

        if len(leaders) == 1:
            return leaders[0]

    raise RuntimeError("timed out waiting for a single leader")


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
            recent = "\n".join(line for _, line in np.snapshot()[-40:])
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
    prefix = f"e2e_len{length_bytes}_cmd{seq:04d}_from{submit_node}:"
    payload = "x" * max(length_bytes, 0)
    return prefix + payload


def run_experiment(
    binary: Path,
    base_port: int,
    cluster_size: int,
    commands: int,
    timeout_ms: int,
    message_length: int,
    submit_mode: str,
    fixed_submit_node: int,
) -> List[Dict[str, object]]:
    node_ids = list(range(1, cluster_size + 1))
    nodes: Dict[int, NodeProcess] = {}
    records: List[Dict[str, object]] = []
    cursors: Dict[int, int] = {nid: 0 for nid in node_ids}

    try:
        nodes = start_nodes(binary, base_port, node_ids)

        if submit_mode == "fixed" and fixed_submit_node not in node_ids:
            raise ValueError(f"--fixed-submit-node {fixed_submit_node} is not in cluster 1..{cluster_size}")

        for i in range(commands):
            leader_at_submit = wait_for_single_leader(nodes)
            if submit_mode == "leader_only":
                submit_node = leader_at_submit
            elif submit_mode == "fixed":
                submit_node = fixed_submit_node
            else:
                submit_node = node_ids[i % len(node_ids)]

            cmd = make_command(message_length, i, submit_node)
            t_submit = time.perf_counter()
            nodes[submit_node].send_line(cmd)

            leader_commit_ts: Optional[float] = None
            first_commit_ts: Optional[float] = None
            first_commit_node: Optional[int] = None
            first_commit_slot: Optional[int] = None

            deadline = t_submit + (timeout_ms / 1000.0)
            while time.perf_counter() < deadline and leader_commit_ts is None:
                for nid, np in nodes.items():
                    lines = np.snapshot()
                    start = cursors[nid]
                    for ts, line in lines[start:]:
                        eval_m = EVAL_COMMIT_RE.search(line)
                        if eval_m:
                            commit_node = int(eval_m.group(1))
                            slot = int(eval_m.group(2))
                            committed_cmd = eval_m.group(4).strip()
                            if committed_cmd == cmd:
                                if first_commit_ts is None or ts < first_commit_ts:
                                    first_commit_ts = ts
                                    first_commit_node = commit_node
                                    first_commit_slot = slot
                                if commit_node == leader_at_submit and leader_commit_ts is None:
                                    leader_commit_ts = ts
                            continue

                        # Fallback for non-trace commit lines.
                        m = COMMIT_RE.search(line)
                        if m:
                            commit_node = int(m.group(1))
                            slot = int(m.group(2))
                            committed_cmd = m.group(3).strip()
                            if committed_cmd != cmd:
                                continue

                            if first_commit_ts is None or ts < first_commit_ts:
                                first_commit_ts = ts
                                first_commit_node = commit_node
                                first_commit_slot = slot

                            if commit_node == leader_at_submit and leader_commit_ts is None:
                                leader_commit_ts = ts

                    cursors[nid] = len(lines)
                    if leader_commit_ts is not None:
                        break
                if leader_commit_ts is None:
                    time.sleep(0.002)

            if leader_commit_ts is None:
                records.append(
                    {
                        "command": cmd,
                        "cluster_size": cluster_size,
                        "message_length_bytes": message_length,
                        "submit_mode": submit_mode,
                        "submit_node": submit_node,
                        "leader_at_submit": leader_at_submit,
                        "submit_ts": f"{t_submit:.9f}",
                        "leader_commit_ts": "",
                        "latency_ms": "",
                        "first_commit_node": first_commit_node if first_commit_node is not None else "",
                        "first_commit_slot": first_commit_slot if first_commit_slot is not None else "",
                        "first_commit_ts": f"{first_commit_ts:.9f}" if first_commit_ts is not None else "",
                        "status": "timeout_or_leader_changed",
                    }
                )
                print(f"warning: timeout_or_leader_changed for cmd_idx={i}", file=sys.stderr)
                continue

            latency_ms = (leader_commit_ts - t_submit) * 1000.0
            records.append(
                {
                    "command": cmd,
                    "cluster_size": cluster_size,
                    "message_length_bytes": message_length,
                    "submit_mode": submit_mode,
                    "submit_node": submit_node,
                    "leader_at_submit": leader_at_submit,
                    "submit_ts": f"{t_submit:.9f}",
                    "leader_commit_ts": f"{leader_commit_ts:.9f}",
                    "latency_ms": f"{latency_ms:.6f}",
                    "first_commit_node": first_commit_node if first_commit_node is not None else "",
                    "first_commit_slot": first_commit_slot if first_commit_slot is not None else "",
                    "first_commit_ts": f"{first_commit_ts:.9f}" if first_commit_ts is not None else "",
                    "status": "ok",
                }
            )

    finally:
        if nodes:
            stop_nodes(nodes)

    return records


def load_rows_from_csv(csv_path: Path) -> List[Dict[str, object]]:
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_summary(summary_path: Path, source: str, rows: List[Dict[str, object]]) -> None:
    ok_lat = [float(r["latency_ms"]) for r in rows if r.get("status") == "ok" and str(r.get("latency_ms", "")).strip()]
    st = summarize(ok_lat)
    total = len(rows)
    ok = int(st["count"])
    timeout = total - ok
    timeout_rate = (timeout / total * 100.0) if total else 0.0

    cluster_sizes = sorted({int(r["cluster_size"]) for r in rows if str(r.get("cluster_size", "")).strip()})
    msg_lengths = sorted({int(r["message_length_bytes"]) for r in rows if str(r.get("message_length_bytes", "")).strip()})
    submit_modes = sorted({str(r["submit_mode"]) for r in rows if str(r.get("submit_mode", "")).strip()})

    with summary_path.open("w", encoding="utf-8") as f:
        f.write("End-to-End Client->Leader Commit Latency\n")
        f.write("=======================================\n\n")
        f.write("Metric: t(leader commit_send observed) - t(client submit)\n")
        f.write("Client: this eval script writes requests to node stdin.\n\n")
        f.write(f"Source: {source}\n")
        f.write(f"Cluster sizes: {','.join(str(v) for v in cluster_sizes)}\n")
        f.write(f"Message lengths (bytes): {','.join(str(v) for v in msg_lengths)}\n")
        f.write(f"Submit modes: {','.join(submit_modes)}\n")
        f.write(f"Total samples: {total}\n")
        f.write(f"Successful samples: {ok}\n")
        f.write(f"Timeout/fail samples: {timeout} ({timeout_rate:.2f}%)\n\n")
        f.write(f"mean: {st['mean']:.3f} ms\n")
        f.write(f"p50:  {st['p50']:.3f} ms\n")
        f.write(f"p95:  {st['p95']:.3f} ms\n")
        f.write(f"p99:  {st['p99']:.3f} ms\n")


def write_svg(fig_path: Path, rows: List[Dict[str, object]]) -> None:
    ok_rows = [r for r in rows if r.get("status") == "ok" and str(r.get("latency_ms", "")).strip()]
    lat_ms = [float(r["latency_ms"]) for r in ok_rows]
    st = summarize(lat_ms)
    total = len(rows)
    ok = int(st["count"])
    timeout = total - ok

    width, height = 1140, 540
    left = (70, 90, 470, 320)
    right = (620, 90, 450, 320)

    x0, y0, w0, h0 = left
    x1, y1, w1, h1 = right

    max_y = max([1.0] + lat_ms) * 1.15
    cdf = cdf_points(lat_ms)
    cdf_min_x = min(lat_ms) - 20.0 if lat_ms else -20.0
    cdf_max_x = max([1.0] + lat_ms) * 1.05
    if cdf_max_x <= cdf_min_x:
        cdf_max_x = cdf_min_x + 1.0

    svg: List[str] = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    svg.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
    svg.append('<text x="30" y="34" font-size="24" font-family="Arial">End-to-End Latency: Client Submit -> Leader Commit</text>')

    # left: per-command
    svg.append(f'<rect x="{x0}" y="{y0}" width="{w0}" height="{h0}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y0 + h0 - (i / 5) * h0
        val = (i / 5) * max_y
        svg.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x0+w0}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x0-40}" y="{gy+4:.1f}" font-size="10" font-family="Arial">{val:.1f}</text>')
    n = len(lat_ms)
    if n > 1:
        pts = []
        for i, v in enumerate(lat_ms):
            px = x0 + (i / (n - 1)) * w0
            py = y0 + h0 - (v / max_y) * h0
            pts.append((px, py))
        d = " ".join(("M" if i == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for i, p in enumerate(pts))
        svg.append(f'<path d="{d}" fill="none" stroke="#1f77b4" stroke-width="2"/>')
    svg.append(f'<text x="{x0}" y="{y0-12}" font-size="14" font-family="Arial">Per-command latency (successful only)</text>')
    svg.append(f'<text x="{x0 + w0/2 - 42}" y="{y0+h0+30}" font-size="12" font-family="Arial">command index</text>')
    svg.append(
        f'<text x="{x0-55}" y="{y0 + h0/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x0-55},{y0 + h0/2})">latency (ms)</text>'
    )

    # right: CDF
    svg.append(f'<rect x="{x1}" y="{y1}" width="{w1}" height="{h1}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y1 + h1 - (i / 5) * h1
        svg.append(f'<line x1="{x1}" y1="{gy:.1f}" x2="{x1+w1}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x1-28}" y="{gy+4:.1f}" font-size="10" font-family="Arial">{i/5:.1f}</text>')
    for i in range(6):
        gx = x1 + (i / 5) * w1
        val = cdf_min_x + (i / 5) * (cdf_max_x - cdf_min_x)
        svg.append(f'<line x1="{gx:.1f}" y1="{y1}" x2="{gx:.1f}" y2="{y1+h1}" stroke="#eee"/>')
        svg.append(f'<text x="{gx-14:.1f}" y="{y1+h1+18}" font-size="10" font-family="Arial">{val:.1f}</text>')

    if cdf:
        sx = w1 / (cdf_max_x - cdf_min_x)
        sy = h1
        coords = []
        for x, y in cdf:
            px = x1 + (x - cdf_min_x) * sx
            py = y1 + h1 - y * sy
            coords.append((px, py))
        d = " ".join(("M" if i == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for i, p in enumerate(coords))
        svg.append(f'<path d="{d}" fill="none" stroke="#111" stroke-width="2"/>')

    svg.append(f'<text x="{x1}" y="{y1-12}" font-size="14" font-family="Arial">CDF</text>')
    svg.append(f'<text x="{x1 + w1/2 - 34}" y="{y1+h1+30}" font-size="12" font-family="Arial">latency (ms)</text>')

    svg.append(
        f'<text x="70" y="460" font-size="12" font-family="Arial">n_ok={ok}/{total} timeout={timeout} mean={st["mean"]:.3f}ms p50={st["p50"]:.3f}ms p95={st["p95"]:.3f}ms p99={st["p99"]:.3f}ms</text>'
    )
    svg.append("</svg>")
    fig_path.write_text("\n".join(svg), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="End-to-end client submit -> leader commit evaluator")
    p.add_argument("--binary", default="./build/paxos_node")
    p.add_argument("--base-port", type=int, default=18000)
    p.add_argument("--cluster-size", type=int, default=3)
    p.add_argument("--commands", type=int, default=100)
    p.add_argument("--timeout-ms", type=int, default=15000)
    p.add_argument("--message-length", type=int, default=256)
    p.add_argument("--submit-mode", choices=["round_robin", "leader_only", "fixed"], default="round_robin")
    p.add_argument("--fixed-submit-node", type=int, default=1)
    p.add_argument("--mode", choices=["both", "run", "plot"], default="both")
    p.add_argument("--input-csv", default="", help="CSV path for --mode plot (default: <out-dir>/submit_to_leader_commit_samples_*.csv)")
    p.add_argument("--out-dir", default="eval/eval_submit_to_leader_commit")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = f"{args.message_length}_{args.cluster_size}_{args.submit_mode}"
    if args.mode == "plot" and args.input_csv:
        stem = Path(args.input_csv).stem
        prefix = "submit_to_leader_commit_samples_"
        if stem.startswith(prefix):
            tag = stem[len(prefix):]
        else:
            tag = "from_csv"

    csv_path = out_dir / f"submit_to_leader_commit_samples_{tag}.csv"
    summary_path = out_dir / f"submit_to_leader_commit_summary_{tag}.txt"
    fig_path = out_dir / f"submit_to_leader_commit_figure_{tag}.svg"

    rows: List[Dict[str, object]]

    if args.mode in ("both", "run"):
        binary = Path(args.binary)
        if not binary.exists():
            print(f"binary not found: {binary}", file=sys.stderr)
            return 2
        if args.cluster_size < 3:
            print("cluster-size must be >= 3", file=sys.stderr)
            return 2
        if args.message_length < 1:
            print("message-length must be >= 1", file=sys.stderr)
            return 2

        rows = run_experiment(
            binary=binary,
            base_port=args.base_port,
            cluster_size=args.cluster_size,
            commands=args.commands,
            timeout_ms=args.timeout_ms,
            message_length=args.message_length,
            submit_mode=args.submit_mode,
            fixed_submit_node=args.fixed_submit_node,
        )

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "command",
                    "cluster_size",
                    "message_length_bytes",
                    "submit_mode",
                    "submit_node",
                    "leader_at_submit",
                    "submit_ts",
                    "leader_commit_ts",
                    "latency_ms",
                    "first_commit_node",
                    "first_commit_slot",
                    "first_commit_ts",
                    "status",
                ],
            )
            w.writeheader()
            for r in rows:
                w.writerow(r)
    else:
        input_csv = Path(args.input_csv) if args.input_csv else csv_path
        if not input_csv.exists():
            print(f"input csv not found: {input_csv}", file=sys.stderr)
            return 2
        rows = load_rows_from_csv(input_csv)
        csv_path = input_csv

    if not rows:
        print("no samples", file=sys.stderr)
        return 2

    source = str(Path(args.binary)) if args.mode in ("both", "run") else f"csv:{csv_path}"
    write_summary(summary_path, source, rows)

    if args.mode in ("both", "plot"):
        write_svg(fig_path, rows)

    print(f"samples csv: {csv_path}")
    print(f"summary: {summary_path}")
    if args.mode in ("both", "plot"):
        print(f"figure: {fig_path}")
    else:
        print("figure: (skipped in --mode run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
