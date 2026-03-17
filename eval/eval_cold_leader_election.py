#!/usr/bin/env python3
"""Evaluate cold leader-election latency with cluster-size sweep support.

Metric:
  t(first leader_elected trace) - t(first node_start trace in that trial)

Requires paxos_node started with --eval-trace and emitting:
  node=<id> phase=node_start t_ns=<steady_ns>
  node=<id> phase=leader_elected ballot=<ballot> t_ns=<steady_ns>
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

LEADER_ELECTED_RE = re.compile(r"node=(\d+)\s+phase=leader_elected\s+ballot=([-\d]+)\s+t_ns=(\d+)\s*$")
NODE_START_RE = re.compile(r"node=(\d+)\s+phase=node_start\s+t_ns=(\d+)\s*$")
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]


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


@dataclass
class LeaderEvent:
    node_id: int
    ballot: int
    t_ns: int


@dataclass
class NodeStartEvent:
    node_id: int
    t_ns: int


def parse_int_csv(spec: str, arg_name: str, min_value: int) -> List[int]:
    vals: List[int] = []
    for tok in spec.split(","):
        t = tok.strip()
        if not t:
            continue
        v = int(t)
        if v < min_value:
            raise ValueError(f"{arg_name} values must be >= {min_value}")
        vals.append(v)
    if not vals:
        raise ValueError(f"{arg_name} produced an empty set")
    return vals


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
    return [(v, (i + 1) / n) for i, v in enumerate(s)]


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
    return nodes


def stop_nodes(nodes: Dict[int, NodeProcess]) -> None:
    for np in nodes.values():
        try:
            if np.proc.poll() is None:
                np.send_line("/quit")
        except Exception:
            pass
    time.sleep(0.2)
    for np in nodes.values():
        if np.proc.poll() is None:
            np.proc.terminate()
    for np in nodes.values():
        try:
            np.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            np.proc.kill()


def wait_for_first_leader_event(
    nodes: Dict[int, NodeProcess], cursors: Dict[int, int], timeout_ms: int, scan_ms: int
) -> Optional[LeaderEvent]:
    deadline = time.perf_counter() + (timeout_ms / 1000.0)

    while time.perf_counter() < deadline:
        earliest: Optional[LeaderEvent] = None

        for nid, np in nodes.items():
            if np.proc.poll() is not None:
                recent = "\n".join(np.snapshot()[-40:])
                raise RuntimeError(f"node {nid} exited during election. Recent logs:\n{recent}")

            lines = np.snapshot()
            start = cursors.get(nid, 0)
            for line in lines[start:]:
                m = LEADER_ELECTED_RE.search(line)
                if not m:
                    continue
                event = LeaderEvent(node_id=int(m.group(1)), ballot=int(m.group(2)), t_ns=int(m.group(3)))
                if earliest is None or event.t_ns < earliest.t_ns:
                    earliest = event
            cursors[nid] = len(lines)

        if earliest is not None:
            return earliest

        time.sleep(max(0.001, scan_ms / 1000.0))

    return None


def wait_for_node_start_events(
    nodes: Dict[int, NodeProcess], cursors: Dict[int, int], timeout_ms: int, scan_ms: int
) -> Optional[Dict[int, NodeStartEvent]]:
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    starts: Dict[int, NodeStartEvent] = {}

    while time.perf_counter() < deadline:
        for nid, np in nodes.items():
            if np.proc.poll() is not None:
                recent = "\n".join(np.snapshot()[-40:])
                raise RuntimeError(f"node {nid} exited during startup. Recent logs:\n{recent}")

            lines = np.snapshot()
            start = cursors.get(nid, 0)
            for line in lines[start:]:
                m = NODE_START_RE.search(line)
                if not m:
                    continue
                event = NodeStartEvent(node_id=int(m.group(1)), t_ns=int(m.group(2)))
                prev = starts.get(event.node_id)
                if prev is None or event.t_ns < prev.t_ns:
                    starts[event.node_id] = event
            cursors[nid] = len(lines)

        if len(starts) == len(nodes):
            return starts
        time.sleep(max(0.001, scan_ms / 1000.0))

    return None


def run_trial(
    binary: Path,
    cluster_size: int,
    base_port: int,
    timeout_ms: int,
    scan_ms: int,
    trial_index: int,
) -> Dict[str, object]:
    node_ids = list(range(1, cluster_size + 1))
    nodes: Dict[int, NodeProcess] = {}
    cursors: Dict[int, int] = {nid: 0 for nid in node_ids}
    try:
        nodes = start_nodes(binary, base_port, node_ids)
        start_events = wait_for_node_start_events(nodes, cursors, timeout_ms=timeout_ms, scan_ms=scan_ms)
        if start_events is None:
            return {
                "cluster_size": cluster_size,
                "trial": trial_index,
                "base_port": base_port,
                "cluster_start_ns": "",
                "leader_elected_ns": "",
                "latency_ms": "",
                "leader_id": "",
                "leader_ballot": "",
                "status": "timeout_wait_node_start",
            }

        cluster_start_ns = min(ev.t_ns for ev in start_events.values())
        event = wait_for_first_leader_event(nodes, cursors, timeout_ms=timeout_ms, scan_ms=scan_ms)
        if event is None:
            return {
                "cluster_size": cluster_size,
                "trial": trial_index,
                "base_port": base_port,
                "cluster_start_ns": cluster_start_ns,
                "leader_elected_ns": "",
                "latency_ms": "",
                "leader_id": "",
                "leader_ballot": "",
                "status": "timeout",
            }

        latency_ms = (event.t_ns - cluster_start_ns) / 1e6
        return {
            "cluster_size": cluster_size,
            "trial": trial_index,
            "base_port": base_port,
            "cluster_start_ns": cluster_start_ns,
            "leader_elected_ns": event.t_ns,
            "latency_ms": f"{latency_ms:.6f}",
            "leader_id": event.node_id,
            "leader_ballot": event.ballot,
            "status": "ok",
        }
    finally:
        if nodes:
            stop_nodes(nodes)


def load_rows_from_csv(csv_path: Path) -> List[Dict[str, object]]:
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def aggregate_rows(rows: List[Dict[str, object]]) -> Tuple[List[int], Dict[int, List[float]], Dict[int, int], Dict[int, int]]:
    cluster_sizes = sorted({int(r["cluster_size"]) for r in rows if str(r.get("cluster_size", "")).strip()})
    latencies_by_n: Dict[int, List[float]] = {n: [] for n in cluster_sizes}
    total_by_n: Dict[int, int] = {n: 0 for n in cluster_sizes}
    timeout_by_n: Dict[int, int] = {n: 0 for n in cluster_sizes}

    for r in rows:
        n = int(r["cluster_size"])
        total_by_n[n] += 1
        if str(r.get("status", "")) != "ok":
            timeout_by_n[n] += 1
            continue
        raw = str(r.get("latency_ms", "")).strip()
        if not raw:
            timeout_by_n[n] += 1
            continue
        latencies_by_n[n].append(float(raw))

    return cluster_sizes, latencies_by_n, total_by_n, timeout_by_n


def write_summary(summary_path: Path, source: str, rows: List[Dict[str, object]], scan_ms: int) -> None:
    cluster_sizes, by_n, total_by_n, timeout_by_n = aggregate_rows(rows)

    all_ok = [v for vals in by_n.values() for v in vals]
    st_all = summarize(all_ok)
    total = len(rows)
    ok = int(st_all["count"])
    timeout = total - ok
    timeout_rate = (timeout / total * 100.0) if total else 0.0

    with summary_path.open("w", encoding="utf-8") as f:
        f.write("Cold Leader Election Latency\n")
        f.write("============================\n\n")
        f.write("Metric: t(first leader_elected trace) - t(first node_start trace in trial)\n")
        f.write(f"Trace scan interval: {scan_ms} ms\n\n")
        f.write(f"Source: {source}\n")
        f.write(f"Cluster sizes: {','.join(str(n) for n in cluster_sizes)}\n")
        f.write(f"Total trials: {total}\n")
        f.write(f"Successful trials: {ok}\n")
        f.write(f"Timeout/fail trials: {timeout} ({timeout_rate:.2f}%)\n\n")
        f.write("All successful trials:\n")
        f.write(f"  mean: {st_all['mean']:.3f} ms\n")
        f.write(f"  p50:  {st_all['p50']:.3f} ms\n")
        f.write(f"  p95:  {st_all['p95']:.3f} ms\n")
        f.write(f"  p99:  {st_all['p99']:.3f} ms\n\n")

        for n in cluster_sizes:
            vals = by_n.get(n, [])
            st = summarize(vals)
            n_total = total_by_n.get(n, 0)
            n_timeout = timeout_by_n.get(n, 0)
            timeout_pct = (n_timeout / n_total * 100.0) if n_total else 0.0
            f.write(f"Cluster size {n}:\n")
            f.write(f"  successful: {int(st['count'])}/{n_total}\n")
            f.write(f"  timeout rate: {timeout_pct:.2f}%\n")
            f.write(f"  mean: {st['mean']:.3f} ms\n")
            f.write(f"  p50:  {st['p50']:.3f} ms\n")
            f.write(f"  p95:  {st['p95']:.3f} ms\n")
            f.write(f"  p99:  {st['p99']:.3f} ms\n\n")


def write_svg(fig_path: Path, rows: List[Dict[str, object]], scan_ms: int) -> None:
    cluster_sizes, by_n, total_by_n, timeout_by_n = aggregate_rows(rows)

    p50_by_n = {n: (percentile(by_n[n], 50) if by_n[n] else float("nan")) for n in cluster_sizes}
    p95_by_n = {n: (percentile(by_n[n], 95) if by_n[n] else float("nan")) for n in cluster_sizes}

    all_ok = [v for vals in by_n.values() for v in vals]
    st_all = summarize(all_ok)
    total = len(rows)
    ok = int(st_all["count"])
    timeout = total - ok

    width, height = 1200, 540
    left = (70, 90, 500, 330)
    right = (640, 90, 500, 330)
    x0, y0, w0, h0 = left
    x1, y1, w1, h1 = right

    cdf_min_x = (min(all_ok) - 5.0) if all_ok else 0.0
    cdf_max_x = (max(all_ok) * 1.05) if all_ok else 1.0
    if cdf_max_x <= cdf_min_x:
        cdf_max_x = cdf_min_x + 1.0

    stat_vals = [v for v in list(p50_by_n.values()) + list(p95_by_n.values()) if not math.isnan(v)]
    stat_max_y = max([1.0] + stat_vals) * 1.15

    svg: List[str] = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    svg.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
    svg.append('<text x="30" y="34" font-size="24" font-family="Arial">Cold Leader Election Latency vs Cluster Size</text>')
    svg.append(f'<text x="30" y="58" font-size="13" font-family="Arial">first leader_elected trace</text>')

    svg.append(f'<rect x="{x0}" y="{y0}" width="{w0}" height="{h0}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y0 + h0 - (i / 5) * h0
        val = i / 5
        svg.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x0 + w0}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x0 - 28}" y="{gy + 4:.1f}" font-size="10" font-family="Arial">{val:.1f}</text>')
    for i in range(6):
        gx = x0 + (i / 5) * w0
        val = cdf_min_x + (i / 5) * (cdf_max_x - cdf_min_x)
        svg.append(f'<line x1="{gx:.1f}" y1="{y0}" x2="{gx:.1f}" y2="{y0 + h0}" stroke="#f3f3f3"/>')
        svg.append(f'<text x="{gx - 14:.1f}" y="{y0 + h0 + 18}" font-size="10" font-family="Arial">{val:.1f}</text>')

    sx = w0 / (cdf_max_x - cdf_min_x)
    sy = h0
    for i, n in enumerate(cluster_sizes):
        points = cdf_points(by_n.get(n, []))
        if len(points) < 2:
            continue
        color = PALETTE[i % len(PALETTE)]
        coords = []
        for x, y in points:
            px = x0 + (x - cdf_min_x) * sx
            py = y0 + h0 - y * sy
            coords.append((px, py))
        path = " ".join(("M" if j == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for j, p in enumerate(coords))
        svg.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')

    svg.append(f'<text x="{x0}" y="{y0 - 12}" font-size="14" font-family="Arial">CDF</text>')
    svg.append(f'<text x="{x0 + w0/2 - 36}" y="{y0 + h0 + 30}" font-size="12" font-family="Arial">latency (ms)</text>')
    svg.append(
        f'<text x="{x0 - 56}" y="{y0 + h0/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x0 - 56},{y0 + h0/2})">CDF</text>'
    )

    svg.append(f'<rect x="{x1}" y="{y1}" width="{w1}" height="{h1}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y1 + h1 - (i / 5) * h1
        val = (i / 5) * stat_max_y
        svg.append(f'<line x1="{x1}" y1="{gy:.1f}" x2="{x1 + w1}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x1 - 40}" y="{gy + 4:.1f}" font-size="10" font-family="Arial">{val:.1f}</text>')

    n_keys = len(cluster_sizes)
    x_points: List[float] = []
    for i, n in enumerate(cluster_sizes):
        gx = x1 + (0 if n_keys == 1 else (i / (n_keys - 1)) * w1)
        x_points.append(gx)
        svg.append(f'<line x1="{gx:.1f}" y1="{y1}" x2="{gx:.1f}" y2="{y1 + h1}" stroke="#f3f3f3"/>')
        svg.append(f'<text x="{gx - 12:.1f}" y="{y1 + h1 + 18}" font-size="10" font-family="Arial">{n}</text>')

    def draw_series(values: List[float], color: str) -> None:
        pts: List[Tuple[float, float]] = []
        for i, v in enumerate(values):
            if math.isnan(v):
                continue
            px = x_points[i]
            py = y1 + h1 - (v / stat_max_y) * h1
            pts.append((px, py))
        if len(pts) >= 2:
            d = " ".join(("M" if j == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for j, p in enumerate(pts))
            svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        for px, py in pts:
            svg.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.8" fill="{color}"/>')

    draw_series([p50_by_n.get(n, float("nan")) for n in cluster_sizes], "#1f77b4")
    draw_series([p95_by_n.get(n, float("nan")) for n in cluster_sizes], "#d62728")

    svg.append(f'<text x="{x1}" y="{y1 - 12}" font-size="14" font-family="Arial">p50/p95 vs cluster size</text>')
    svg.append(f'<text x="{x1 + w1/2 - 62}" y="{y1 + h1 + 30}" font-size="12" font-family="Arial">cluster size (nodes)</text>')
    svg.append(
        f'<text x="{x1 - 58}" y="{y1 + h1/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x1 - 58},{y1 + h1/2})">latency (ms)</text>'
    )

    lg_w = 220
    lg_h = 22 + len(cluster_sizes) * 18 + 8
    lg_x = x1 - lg_w - 70
    lg_y = y1 + h1 - lg_h - 10
    if lg_y < y1 + 20:
        lg_y = y1 + 20
    svg.append(f'<rect x="{lg_x - 6}" y="{lg_y - 14}" width="{lg_w}" height="{lg_h}" fill="white" fill-opacity="0.88" stroke="#999"/>')
    svg.append(f'<text x="{lg_x}" y="{lg_y}" font-size="11" font-family="Arial">cluster / successful / timeout</text>')
    for i, n in enumerate(cluster_sizes):
        color = PALETTE[i % len(PALETTE)]
        n_total = total_by_n.get(n, 0)
        n_ok = len(by_n.get(n, []))
        n_timeout = timeout_by_n.get(n, 0)
        timeout_rate = (n_timeout / n_total * 100.0) if n_total else 0.0
        yy = lg_y + (i + 1) * 18
        svg.append(f'<line x1="{lg_x}" y1="{yy - 4}" x2="{lg_x + 20}" y2="{yy - 4}" stroke="{color}" stroke-width="2"/>')
        svg.append(f'<text x="{lg_x + 26}" y="{yy}" font-size="11" font-family="Arial">N={n} n={n_ok}/{n_total} timeout={timeout_rate:.1f}%</text>')

    rg_x = x1 + 12
    rg_y = y1 + 14
    svg.append(f'<line x1="{rg_x}" y1="{rg_y}" x2="{rg_x + 24}" y2="{rg_y}" stroke="#1f77b4" stroke-width="2"/>')
    svg.append(f'<text x="{rg_x + 30}" y="{rg_y + 4}" font-size="11" font-family="Arial">p50</text>')
    svg.append(f'<line x1="{rg_x + 70}" y1="{rg_y}" x2="{rg_x + 94}" y2="{rg_y}" stroke="#d62728" stroke-width="2"/>')
    svg.append(f'<text x="{rg_x + 100}" y="{rg_y + 4}" font-size="11" font-family="Arial">p95</text>')

    svg.append(
        f'<text x="70" y="500" font-size="12" font-family="Arial">overall n_ok={ok}/{total} timeout={timeout} mean={st_all["mean"]:.3f}ms p50={st_all["p50"]:.3f}ms p95={st_all["p95"]:.3f}ms p99={st_all["p99"]:.3f}ms</text>'
    )
    svg.append("</svg>")
    fig_path.write_text("\n".join(svg), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Cold leader-election latency evaluator")
    p.add_argument("--binary", default="./build/paxos_node")
    p.add_argument("--base-port", type=int, default=18000)
    p.add_argument("--cluster-size", default="3", help="single value or CSV, e.g. 3 or 3,5,11")
    p.add_argument("--trials", type=int, default=30, help="trials per cluster size")
    p.add_argument("--timeout-ms", type=int, default=10000)
    p.add_argument("--scan-ms", type=int, default=5, help="log scan interval while waiting for first leader event")
    p.add_argument("--mode", choices=["both", "run", "plot"], default="both")
    p.add_argument("--input-csv", default="", help="CSV path for --mode plot")
    p.add_argument("--out-dir", default="eval/eval_cold_leader_election")
    args = p.parse_args()

    if args.scan_ms <= 0:
        print("--scan-ms must be > 0", file=sys.stderr)
        return 2
    if args.trials <= 0:
        print("--trials must be > 0", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = f"{args.cluster_size}_trials{args.trials}"
    if args.mode == "plot" and args.input_csv:
        stem = Path(args.input_csv).stem
        prefix = "cold_leader_election_samples_"
        tag = stem[len(prefix):] if stem.startswith(prefix) else "from_csv"

    csv_path = out_dir / f"cold_leader_election_samples_{tag}.csv"
    summary_path = out_dir / f"cold_leader_election_summary_{tag}.txt"
    fig_path = out_dir / f"cold_leader_election_figure_{tag}.svg"

    rows: List[Dict[str, object]]

    if args.mode in ("both", "run"):
        binary = Path(args.binary)
        if not binary.exists():
            print(f"binary not found: {binary}", file=sys.stderr)
            return 2

        cluster_sizes = sorted(parse_int_csv(args.cluster_size, "--cluster-size", min_value=3))
        max_n = max(cluster_sizes)
        port_stride = max_n + 50
        run_index = 0
        rows = []

        for n in cluster_sizes:
            for trial in range(args.trials):
                trial_base_port = args.base_port + run_index * port_stride
                row = run_trial(
                    binary=binary,
                    cluster_size=n,
                    base_port=trial_base_port,
                    timeout_ms=args.timeout_ms,
                    scan_ms=args.scan_ms,
                    trial_index=trial,
                )
                rows.append(row)
                if row["status"] != "ok":
                    print(f"warning: timeout for cluster_size={n} trial={trial}", file=sys.stderr)
                run_index += 1
                time.sleep(0.1)

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "cluster_size",
                    "trial",
                    "base_port",
                    "cluster_start_ns",
                    "leader_elected_ns",
                    "latency_ms",
                    "leader_id",
                    "leader_ballot",
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
    write_summary(summary_path, source, rows, scan_ms=args.scan_ms)
    if args.mode in ("both", "plot"):
        write_svg(fig_path, rows, scan_ms=args.scan_ms)

    print(f"samples csv: {csv_path}")
    print(f"summary: {summary_path}")
    if args.mode in ("both", "plot"):
        print(f"figure: {fig_path}")
    else:
        print("figure: (skipped in --mode run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
