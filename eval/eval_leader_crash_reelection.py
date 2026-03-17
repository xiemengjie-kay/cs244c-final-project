#!/usr/bin/env python3
"""Evaluate leader crash -> re-election latency.

Metric:
  t(first new leader_elected after crash) - t(node_crashed)

Sweep parameters:
  --election-timeout-base
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
NODE_CRASHED_RE = re.compile(r"node=(\d+)\s+phase=node_crashed\s+t_ns=(\d+)\s*$")
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#17becf", "#8c564b"]


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
class CrashEvent:
    node_id: int
    t_ns: int


def parse_int_csv(spec: str, arg_name: str, min_value: int = 1) -> List[int]:
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
        raise ValueError(f"{arg_name} produced empty set")
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


def start_nodes(
    binary: Path,
    base_port: int,
    node_ids: List[int],
    heartbeat_ticks: int,
    election_timeout_base: int,
    election_timeout_step: int,
) -> Dict[int, NodeProcess]:
    node_spec = ",".join(f"{nid}:127.0.0.1:{base_port + nid}" for nid in node_ids)
    nodes: Dict[int, NodeProcess] = {}
    for nid in node_ids:
        cmd = [
            str(binary),
            "--id",
            str(nid),
            "--nodes",
            node_spec,
            "--eval-trace",
            "--heartbeat-ticks",
            str(heartbeat_ticks),
            "--election-timeout-base",
            str(election_timeout_base),
            "--election-timeout-step",
            str(election_timeout_step),
        ]
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
    time.sleep(0.3)
    for np in nodes.values():
        if np.proc.poll() is None:
            np.proc.terminate()
    for np in nodes.values():
        try:
            np.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            np.proc.kill()


def parse_leader_events(lines: List[str], start: int) -> Tuple[List[LeaderEvent], int]:
    out: List[LeaderEvent] = []
    end = len(lines)
    for line in lines[start:end]:
        m = LEADER_ELECTED_RE.search(line)
        if not m:
            continue
        out.append(LeaderEvent(node_id=int(m.group(1)), ballot=int(m.group(2)), t_ns=int(m.group(3))))
    return out, end


def parse_crash_events(lines: List[str], start: int) -> Tuple[List[CrashEvent], int]:
    out: List[CrashEvent] = []
    end = len(lines)
    for line in lines[start:end]:
        m = NODE_CRASHED_RE.search(line)
        if not m:
            continue
        out.append(CrashEvent(node_id=int(m.group(1)), t_ns=int(m.group(2))))
    return out, end


def wait_for_initial_leader(
    nodes: Dict[int, NodeProcess], cursors: Dict[int, int], timeout_ms: int, scan_ms: int
) -> Optional[LeaderEvent]:
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    while time.perf_counter() < deadline:
        earliest: Optional[LeaderEvent] = None
        for nid, np in nodes.items():
            if np.proc.poll() is not None:
                recent = "\n".join(np.snapshot()[-40:])
                raise RuntimeError(f"node {nid} exited before initial leader election. Recent logs:\n{recent}")
            lines = np.snapshot()
            events, next_cursor = parse_leader_events(lines, cursors[nid])
            cursors[nid] = next_cursor
            for ev in events:
                if earliest is None or ev.t_ns < earliest.t_ns:
                    earliest = ev
        if earliest is not None:
            return earliest
        time.sleep(max(0.001, scan_ms / 1000.0))
    return None


def wait_for_crash_event(
    node: NodeProcess, cursor: int, timeout_ms: int, scan_ms: int
) -> Tuple[Optional[CrashEvent], int]:
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    cur = cursor
    while time.perf_counter() < deadline:
        if node.proc.poll() is not None:
            recent = "\n".join(node.snapshot()[-40:])
            raise RuntimeError(f"crashed leader node {node.node_id} exited unexpectedly. Recent logs:\n{recent}")
        lines = node.snapshot()
        events, next_cursor = parse_crash_events(lines, cur)
        cur = next_cursor
        if events:
            earliest = min(events, key=lambda e: e.t_ns)
            return earliest, cur
        time.sleep(max(0.001, scan_ms / 1000.0))
    return None, cur


def wait_for_reelection(
    nodes: Dict[int, NodeProcess],
    cursors: Dict[int, int],
    crashed_node: int,
    crash_t_ns: int,
    timeout_ms: int,
    scan_ms: int,
) -> Optional[LeaderEvent]:
    deadline = time.perf_counter() + (timeout_ms / 1000.0)
    while time.perf_counter() < deadline:
        earliest: Optional[LeaderEvent] = None
        for nid, np in nodes.items():
            if np.proc.poll() is not None:
                recent = "\n".join(np.snapshot()[-40:])
                raise RuntimeError(f"node {nid} exited while waiting re-election. Recent logs:\n{recent}")
            lines = np.snapshot()
            events, next_cursor = parse_leader_events(lines, cursors[nid])
            cursors[nid] = next_cursor
            for ev in events:
                if ev.node_id == crashed_node or ev.t_ns <= crash_t_ns:
                    continue
                if earliest is None or ev.t_ns < earliest.t_ns:
                    earliest = ev
        if earliest is not None:
            return earliest
        time.sleep(max(0.001, scan_ms / 1000.0))
    return None


def run_trial(
    binary: Path,
    cluster_size: int,
    base_port: int,
    heartbeat_ticks: int,
    election_timeout_base: int,
    election_timeout_step: int,
    timeout_ms: int,
    scan_ms: int,
    trial_index: int,
) -> Dict[str, object]:
    node_ids = list(range(1, cluster_size + 1))
    nodes: Dict[int, NodeProcess] = {}
    cursors: Dict[int, int] = {nid: 0 for nid in node_ids}

    try:
        nodes = start_nodes(
            binary=binary,
            base_port=base_port,
            node_ids=node_ids,
            heartbeat_ticks=heartbeat_ticks,
            election_timeout_base=election_timeout_base,
            election_timeout_step=election_timeout_step,
        )
        initial = wait_for_initial_leader(nodes, cursors, timeout_ms=timeout_ms, scan_ms=scan_ms)
        if initial is None:
            return {
                "cluster_size": cluster_size,
                "trial": trial_index,
                "base_port": base_port,
                "heartbeat_ticks": heartbeat_ticks,
                "election_timeout_base": election_timeout_base,
                "election_timeout_step": election_timeout_step,
                "initial_leader_id": "",
                "initial_leader_ballot": "",
                "crash_t_ns": "",
                "new_leader_id": "",
                "new_leader_ballot": "",
                "reelected_t_ns": "",
                "latency_ms": "",
                "status": "timeout_initial_leader",
            }

        crashed_leader = initial.node_id
        nodes[crashed_leader].send_line("/crash")
        crash_event, new_cursor = wait_for_crash_event(
            nodes[crashed_leader], cursors[crashed_leader], timeout_ms=timeout_ms, scan_ms=scan_ms
        )
        cursors[crashed_leader] = new_cursor
        if crash_event is None:
            return {
                "cluster_size": cluster_size,
                "trial": trial_index,
                "base_port": base_port,
                "heartbeat_ticks": heartbeat_ticks,
                "election_timeout_base": election_timeout_base,
                "election_timeout_step": election_timeout_step,
                "initial_leader_id": initial.node_id,
                "initial_leader_ballot": initial.ballot,
                "crash_t_ns": "",
                "new_leader_id": "",
                "new_leader_ballot": "",
                "reelected_t_ns": "",
                "latency_ms": "",
                "status": "timeout_crash_event",
            }

        reelected = wait_for_reelection(
            nodes=nodes,
            cursors=cursors,
            crashed_node=crashed_leader,
            crash_t_ns=crash_event.t_ns,
            timeout_ms=timeout_ms,
            scan_ms=scan_ms,
        )
        if reelected is None:
            return {
                "cluster_size": cluster_size,
                "trial": trial_index,
                "base_port": base_port,
                "heartbeat_ticks": heartbeat_ticks,
                "election_timeout_base": election_timeout_base,
                "election_timeout_step": election_timeout_step,
                "initial_leader_id": initial.node_id,
                "initial_leader_ballot": initial.ballot,
                "crash_t_ns": crash_event.t_ns,
                "new_leader_id": "",
                "new_leader_ballot": "",
                "reelected_t_ns": "",
                "latency_ms": "",
                "status": "timeout_reelection",
            }

        latency_ms = (reelected.t_ns - crash_event.t_ns) / 1e6
        return {
            "cluster_size": cluster_size,
            "trial": trial_index,
            "base_port": base_port,
            "heartbeat_ticks": heartbeat_ticks,
            "election_timeout_base": election_timeout_base,
            "election_timeout_step": election_timeout_step,
            "initial_leader_id": initial.node_id,
            "initial_leader_ballot": initial.ballot,
            "crash_t_ns": crash_event.t_ns,
            "new_leader_id": reelected.node_id,
            "new_leader_ballot": reelected.ballot,
            "reelected_t_ns": reelected.t_ns,
            "latency_ms": f"{latency_ms:.6f}",
            "status": "ok",
        }
    finally:
        if nodes:
            stop_nodes(nodes)


def load_rows_from_csv(csv_path: Path) -> List[Dict[str, object]]:
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def aggregate_rows(
    rows: List[Dict[str, object]],
) -> Tuple[
    List[Tuple[int, int]],
    List[int],
    List[int],
    Dict[Tuple[int, int], List[float]],
    Dict[Tuple[int, int], int],
    Dict[Tuple[int, int], int],
]:
    combos = sorted(
        {
            (int(r["heartbeat_ticks"]), int(r["election_timeout_base"]))
            for r in rows
            if str(r.get("heartbeat_ticks", "")).strip() and str(r.get("election_timeout_base", "")).strip()
        }
    )
    heartbeats = sorted({hb for hb, _ in combos})
    et_bases = sorted({eto for _, eto in combos})

    series_by_combo: Dict[Tuple[int, int], List[float]] = {c: [] for c in combos}
    total_by_combo: Dict[Tuple[int, int], int] = {c: 0 for c in combos}
    timeout_by_combo: Dict[Tuple[int, int], int] = {c: 0 for c in combos}

    for r in rows:
        combo = (int(r["heartbeat_ticks"]), int(r["election_timeout_base"]))
        total_by_combo[combo] = total_by_combo.get(combo, 0) + 1
        if r.get("status") != "ok":
            timeout_by_combo[combo] = timeout_by_combo.get(combo, 0) + 1
            continue
        raw = str(r.get("latency_ms", "")).strip()
        if not raw:
            timeout_by_combo[combo] = timeout_by_combo.get(combo, 0) + 1
            continue
        series_by_combo[combo].append(float(raw))

    return combos, heartbeats, et_bases, series_by_combo, total_by_combo, timeout_by_combo


def write_summary(summary_path: Path, source: str, rows: List[Dict[str, object]], cluster_size: int) -> None:
    combos, heartbeats, _, series_by_combo, total_by_combo, timeout_by_combo = aggregate_rows(rows)
    all_ok = [v for vals in series_by_combo.values() for v in vals]
    st_all = summarize(all_ok)
    total = len(rows)
    ok = int(st_all["count"])
    timeout = total - ok
    timeout_rate = (timeout / total * 100.0) if total else 0.0

    with summary_path.open("w", encoding="utf-8") as f:
        f.write("Leader Crash -> Re-election Latency\n")
        f.write("==================================\n\n")
        f.write("Metric: t(first new leader_elected after crash) - t(node_crashed)\n")
        f.write(f"Cluster size: {cluster_size}\n")
        if len(heartbeats) == 1:
            f.write(f"Heartbeat ticks (fixed): {heartbeats[0]}\n")
        else:
            f.write(f"Heartbeat ticks (mixed): {','.join(str(h) for h in heartbeats)}\n")
        f.write(f"Source: {source}\n")
        f.write(f"Total trials: {total}\n")
        f.write(f"Successful trials: {ok}\n")
        f.write(f"Timeout/fail trials: {timeout} ({timeout_rate:.2f}%)\n\n")
        f.write("All successful samples:\n")
        f.write(f"  mean: {st_all['mean']:.3f} ms\n")
        f.write(f"  p50:  {st_all['p50']:.3f} ms\n")
        f.write(f"  p95:  {st_all['p95']:.3f} ms\n")
        f.write(f"  p99:  {st_all['p99']:.3f} ms\n\n")

        for hb, eto in combos:
            vals = series_by_combo.get((hb, eto), [])
            st = summarize(vals)
            n_total = total_by_combo.get((hb, eto), 0)
            n_timeout = timeout_by_combo.get((hb, eto), 0)
            t_rate = (n_timeout / n_total * 100.0) if n_total else 0.0
            if len(heartbeats) == 1:
                f.write(f"election_timeout_base={eto}:\n")
            else:
                f.write(f"hb={hb}, election_timeout_base={eto}:\n")
            f.write(f"  successful: {int(st['count'])}/{n_total}\n")
            f.write(f"  timeout rate: {t_rate:.2f}%\n")
            f.write(f"  mean: {st['mean']:.3f} ms\n")
            f.write(f"  p50:  {st['p50']:.3f} ms\n")
            f.write(f"  p95:  {st['p95']:.3f} ms\n")
            f.write(f"  p99:  {st['p99']:.3f} ms\n\n")


def write_svg(fig_path: Path, rows: List[Dict[str, object]], cluster_size: int, timeout_ms: int) -> None:
    combos, heartbeats, et_bases, series_by_combo, total_by_combo, timeout_by_combo = aggregate_rows(rows)
    all_ok = [v for vals in series_by_combo.values() for v in vals]
    st_all = summarize(all_ok)
    total = len(rows)
    ok = int(st_all["count"])
    timeout = total - ok

    width, height = 1280, 560
    left = (70, 90, 600, 350)
    right = (740, 90, 480, 350)
    x0, y0, w0, h0 = left
    x1, y1, w1, h1 = right

    cdf_min_x = 0.0
    cdf_max_x = (max(all_ok) * 1.05) if all_ok else 1.0
    if cdf_max_x <= cdf_min_x:
        cdf_max_x = cdf_min_x + 1.0

    p50_by_combo = {c: (percentile(series_by_combo.get(c, []), 50) if series_by_combo.get(c, []) else float("nan")) for c in combos}
    p95_by_combo = {c: (percentile(series_by_combo.get(c, []), 95) if series_by_combo.get(c, []) else float("nan")) for c in combos}
    stat_vals = [v for v in list(p50_by_combo.values()) + list(p95_by_combo.values()) if not math.isnan(v)]
    stat_max_y = max([1.0] + stat_vals) * 1.15

    svg: List[str] = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    svg.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
    svg.append('<text x="30" y="34" font-size="24" font-family="Arial">Leader Crash -> Re-election Latency Sweep</text>')
    # if len(heartbeats) == 1:
    #     svg.append(f'<text x="30" y="58" font-size="13" font-family="Arial">cluster_size={cluster_size}, heartbeat_ticks={heartbeats[0]}</text>')
    # else:
    #     svg.append(f'<text x="30" y="58" font-size="13" font-family="Arial">cluster_size={cluster_size}, heartbeat_ticks=mixed</text>')

    # Left: CDF lines.
    svg.append(f'<rect x="{x0}" y="{y0}" width="{w0}" height="{h0}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y0 + h0 - (i / 5) * h0
        svg.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x0 + w0}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x0 - 28}" y="{gy + 4:.1f}" font-size="10" font-family="Arial">{i/5:.1f}</text>')
    for i in range(6):
        gx = x0 + (i / 5) * w0
        xv = cdf_min_x + (i / 5) * (cdf_max_x - cdf_min_x)
        svg.append(f'<line x1="{gx:.1f}" y1="{y0}" x2="{gx:.1f}" y2="{y0 + h0}" stroke="#f3f3f3"/>')
        svg.append(f'<text x="{gx - 14:.1f}" y="{y0 + h0 + 18}" font-size="10" font-family="Arial">{xv:.1f}</text>')

    sx = w0 / (cdf_max_x - cdf_min_x)
    sy = h0
    for i, combo in enumerate(combos):
        vals = series_by_combo.get(combo, [])
        pts = cdf_points(vals)
        if len(pts) < 2:
            continue
        color = PALETTE[i % len(PALETTE)]
        coords = []
        for x, y in pts:
            px = x0 + (x - cdf_min_x) * sx
            py = y0 + h0 - y * sy
            coords.append((px, py))
        d = " ".join(("M" if j == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for j, p in enumerate(coords))
        svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')

    if len(heartbeats) == 1:
        svg.append(f'<text x="{x0}" y="{y0 - 12}" font-size="14" font-family="Arial">CDF by election timeout</text>')
    else:
        svg.append(f'<text x="{x0}" y="{y0 - 12}" font-size="14" font-family="Arial">CDF by (heartbeat, election_timeout)</text>')
    svg.append(f'<text x="{x0 + w0/2 - 36}" y="{y0 + h0 + 30}" font-size="12" font-family="Arial">latency (ms)</text>')
    svg.append(f'<text x="{x0 - 56}" y="{y0 + h0/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x0 - 56},{y0 + h0/2})">CDF</text>')

    # Right: p50/p95 vs election timeout.
    svg.append(f'<rect x="{x1}" y="{y1}" width="{w1}" height="{h1}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y1 + h1 - (i / 5) * h1
        yv = (i / 5) * stat_max_y
        svg.append(f'<line x1="{x1}" y1="{gy:.1f}" x2="{x1 + w1}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x1 - 40}" y="{gy + 4:.1f}" font-size="10" font-family="Arial">{yv:.1f}</text>')

    n_x = len(et_bases)
    x_points: Dict[int, float] = {}
    for i, eto in enumerate(et_bases):
        gx = x1 + (0 if n_x == 1 else (i / (n_x - 1)) * w1)
        x_points[eto] = gx
        svg.append(f'<line x1="{gx:.1f}" y1="{y1}" x2="{gx:.1f}" y2="{y1 + h1}" stroke="#f3f3f3"/>')
        svg.append(f'<text x="{gx - 16:.1f}" y="{y1 + h1 + 18}" font-size="10" font-family="Arial">{eto}</text>')

    def draw_line(yvals: List[float], color: str, dashed: bool = False) -> None:
        pts: List[Tuple[float, float]] = []
        for j, y in enumerate(yvals):
            if math.isnan(y):
                continue
            eto = et_bases[j]
            px = x_points[eto]
            py = y1 + h1 - (y / stat_max_y) * h1
            pts.append((px, py))
        if len(pts) >= 2:
            d = " ".join(("M" if k == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for k, p in enumerate(pts))
            dash_attr = ' stroke-dasharray="5 4"' if dashed else ""
            svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"{dash_attr}/>')
        for px, py in pts:
            svg.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.8" fill="{color}"/>')

    for i, hb in enumerate(heartbeats):
        color = PALETTE[i % len(PALETTE)]
        p50s = [p50_by_combo.get((hb, eto), float("nan")) for eto in et_bases]
        p95s = [p95_by_combo.get((hb, eto), float("nan")) for eto in et_bases]
        draw_line(p50s, color, dashed=False)
        draw_line(p95s, color, dashed=True)

    if len(heartbeats) == 1:
        svg.append(f'<text x="{x1}" y="{y1 - 12}" font-size="14" font-family="Arial">p50 solid / p95 dashed vs election timeout</text>')
    else:
        svg.append(f'<text x="{x1}" y="{y1 - 12}" font-size="14" font-family="Arial">p50 solid / p95 dashed vs election timeout (per heartbeat)</text>')
    svg.append(f'<text x="{x1 + w1/2 - 74}" y="{y1 + h1 + 30}" font-size="12" font-family="Arial">election timeout (ticks)</text>')
    svg.append(f'<text x="{x1 - 58}" y="{y1 + h1/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x1 - 58},{y1 + h1/2})">latency (ms)</text>')

    # Combo legend
    lg_x = x0 + 12
    lg_y = y0 + 14
    lg_w = 220
    lg_h = 22 + len(combos) * 16 + 8
    if lg_h > h0 - 20:
        lg_h = h0 - 20
    svg.append(f'<rect x="{lg_x - 6}" y="{lg_y - 14}" width="{lg_w}" height="{lg_h}" fill="white" fill-opacity="0.88" stroke="#999"/>')
    if len(heartbeats) == 1:
        svg.append(f'<text x="{lg_x}" y="{lg_y}" font-size="11" font-family="Arial">eto / success / timeout</text>')
    else:
        svg.append(f'<text x="{lg_x}" y="{lg_y}" font-size="11" font-family="Arial">hb,eto / success / timeout</text>')
    max_rows = max(0, int((lg_h - 28) / 16))
    for i, combo in enumerate(combos[:max_rows]):
        color = PALETTE[i % len(PALETTE)]
        hb, eto = combo
        n_total = total_by_combo.get(combo, 0)
        n_ok = len(series_by_combo.get(combo, []))
        n_timeout = timeout_by_combo.get(combo, 0)
        t_rate = (n_timeout / n_total * 100.0) if n_total else 0.0
        yy = lg_y + (i + 1) * 16
        svg.append(f'<line x1="{lg_x}" y1="{yy - 4}" x2="{lg_x + 18}" y2="{yy - 4}" stroke="{color}" stroke-width="2"/>')
        if len(heartbeats) == 1:
            svg.append(
                f'<text x="{lg_x + 24}" y="{yy}" font-size="10" font-family="Arial">eto={eto} n={n_ok}/{n_total} timeout={t_rate:.1f}%</text>'
            )
        else:
            svg.append(
                f'<text x="{lg_x + 24}" y="{yy}" font-size="10" font-family="Arial">hb={hb},eto={eto} n={n_ok}/{n_total} timeout={t_rate:.1f}%</text>'
            )

    if len(heartbeats) > 1:
        hb_lg_x = x1 + 12
        hb_lg_y = y1 + 14
        for i, hb in enumerate(heartbeats):
            color = PALETTE[i % len(PALETTE)]
            yy = hb_lg_y + i * 14
            if yy > y1 + 100:
                break
            svg.append(f'<line x1="{hb_lg_x}" y1="{yy}" x2="{hb_lg_x + 20}" y2="{yy}" stroke="{color}" stroke-width="2"/>')
            svg.append(f'<text x="{hb_lg_x + 26}" y="{yy + 4}" font-size="10" font-family="Arial">hb={hb}</text>')

    svg.append(
        f'<text x="70" y="510" font-size="12" font-family="Arial">overall n_ok={ok}/{total} timeout={timeout} mean={st_all["mean"]:.3f}ms p50={st_all["p50"]:.3f}ms p95={st_all["p95"]:.3f}ms p99={st_all["p99"]:.3f}ms</text>'
    )
    timeout_s = timeout_ms / 1000.0
    svg.append(
        f'<text x="70" y="532" font-size="12" font-family="Arial">note: trial timeout={timeout_s:g}s; after this duration, the trial is marked failed.</text>'
    )
    svg.append("</svg>")
    fig_path.write_text("\n".join(svg), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Leader crash -> re-election latency evaluator")
    p.add_argument("--binary", default="./build/paxos_node")
    p.add_argument("--base-port", type=int, default=18000)
    p.add_argument("--cluster-size", type=int, default=5)
    p.add_argument("--trials", type=int, default=30, help="trials per election-timeout value")
    p.add_argument("--timeout-ms", type=int, default=12000)
    p.add_argument("--scan-ms", type=int, default=5, help="stdout scan interval while waiting for events")
    p.add_argument("--heartbeat-ticks", type=int, default=2, help="fixed heartbeat interval in ticks")
    p.add_argument("--election-timeout-base", default="14", help="single value or CSV, e.g. 8,14,20,30")
    p.add_argument("--election-timeout-step", type=int, default=3)
    p.add_argument("--mode", choices=["both", "run", "plot"], default="both")
    p.add_argument("--input-csv", default="", help="CSV path for --mode plot")
    p.add_argument("--out-dir", default="eval/eval_leader_crash_reelection")
    args = p.parse_args()

    if args.cluster_size < 3:
        print("--cluster-size must be >= 3", file=sys.stderr)
        return 2
    if args.trials <= 0:
        print("--trials must be > 0", file=sys.stderr)
        return 2
    if args.timeout_ms <= 0:
        print("--timeout-ms must be > 0", file=sys.stderr)
        return 2
    if args.scan_ms <= 0:
        print("--scan-ms must be > 0", file=sys.stderr)
        return 2
    if args.heartbeat_ticks <= 0:
        print("--heartbeat-ticks must be > 0", file=sys.stderr)
        return 2
    if args.election_timeout_step < 0:
        print("--election-timeout-step must be >= 0", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    hb_tag = str(args.heartbeat_ticks)
    eto_tag = args.election_timeout_base
    tag = f"n{args.cluster_size}_hb{hb_tag}_eto{eto_tag}_trials{args.trials}"
    if args.mode == "plot" and args.input_csv:
        stem = Path(args.input_csv).stem
        prefix = "leader_crash_reelection_samples_"
        tag = stem[len(prefix):] if stem.startswith(prefix) else "from_csv"

    csv_path = out_dir / f"leader_crash_reelection_samples_{tag}.csv"
    summary_path = out_dir / f"leader_crash_reelection_summary_{tag}.txt"
    fig_path = out_dir / f"leader_crash_reelection_figure_{tag}.svg"

    rows: List[Dict[str, object]]

    if args.mode in ("both", "run"):
        binary = Path(args.binary)
        if not binary.exists():
            print(f"binary not found: {binary}", file=sys.stderr)
            return 2

        eto_vals = sorted(parse_int_csv(args.election_timeout_base, "--election-timeout-base", min_value=1))

        rows = []
        run_index = 0
        port_stride = args.cluster_size + 50
        hb = args.heartbeat_ticks
        for eto in eto_vals:
            for trial in range(args.trials):
                trial_base_port = args.base_port + run_index * port_stride
                row = run_trial(
                    binary=binary,
                    cluster_size=args.cluster_size,
                    base_port=trial_base_port,
                    heartbeat_ticks=hb,
                    election_timeout_base=eto,
                    election_timeout_step=args.election_timeout_step,
                    timeout_ms=args.timeout_ms,
                    scan_ms=args.scan_ms,
                    trial_index=trial,
                )
                rows.append(row)
                if row.get("status") != "ok":
                    print(
                        f"warning: status={row.get('status')} hb={hb} eto={eto} trial={trial}",
                        file=sys.stderr,
                    )
                run_index += 1
                time.sleep(0.1)

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "cluster_size",
                    "trial",
                    "base_port",
                    "heartbeat_ticks",
                    "election_timeout_base",
                    "election_timeout_step",
                    "initial_leader_id",
                    "initial_leader_ballot",
                    "crash_t_ns",
                    "new_leader_id",
                    "new_leader_ballot",
                    "reelected_t_ns",
                    "latency_ms",
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

    cluster_size = int(rows[0]["cluster_size"]) if rows and str(rows[0].get("cluster_size", "")).strip() else args.cluster_size
    source = str(Path(args.binary)) if args.mode in ("both", "run") else f"csv:{csv_path}"
    write_summary(summary_path, source=source, rows=rows, cluster_size=cluster_size)
    if args.mode in ("both", "plot"):
        write_svg(fig_path, rows=rows, cluster_size=cluster_size, timeout_ms=args.timeout_ms)

    print(f"samples csv: {csv_path}")
    print(f"summary: {summary_path}")
    if args.mode in ("both", "plot"):
        print(f"figure: {fig_path}")
    else:
        print("figure: (skipped in --mode run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
