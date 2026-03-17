#!/usr/bin/env python3
"""Evaluate end-to-end latency for submit-to-majority-followers-commit path.

Metric:
  t_majority_followers_know_commit - t_client_submit_to_node

Where "majority followers know commit" means k-th follower
(k = floor((N-1)/2)+1) reaches commit_index >= slot_of_command.
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
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]


@dataclass
class StatusSnapshot:
    ts: float
    node_id: int
    is_leader: bool
    ballot: int
    commit_index: int


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


def latest_status(lines: List[Tuple[float, str]]) -> Optional[StatusSnapshot]:
    for ts, line in reversed(lines):
        m = STATUS_RE.search(line)
        if m:
            return StatusSnapshot(
                ts=ts,
                node_id=int(m.group(1)),
                is_leader=(m.group(2) == "yes"),
                ballot=int(m.group(3)),
                commit_index=int(m.group(4)),
            )
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
            if st and st.is_leader:
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
    prefix = f"e2e_fwd_len{length_bytes}_cmd{seq:04d}_from{submit_node}:"
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
    status_poll_interval_ms: int,
) -> List[Dict[str, object]]:
    node_ids = list(range(1, cluster_size + 1))
    nodes: Dict[int, NodeProcess] = {}
    records: List[Dict[str, object]] = []
    cursors: Dict[int, int] = {nid: 0 for nid in node_ids}

    try:
        nodes = start_nodes(binary, base_port, node_ids)

        for i in range(commands):
            leader_at_submit = wait_for_single_leader(nodes)
            followers = [nid for nid in node_ids if nid != leader_at_submit]
            majority_followers_required = (len(followers) // 2) + 1

            if submit_mode == "fixed_follower":
                if fixed_submit_node not in followers:
                    records.append(
                        {
                            "command": "",
                            "cluster_size": cluster_size,
                            "message_length_bytes": message_length,
                            "submit_mode": submit_mode,
                            "command_index": i,
                            "submit_node": fixed_submit_node,
                            "leader_at_submit": leader_at_submit,
                            "majority_followers_required": majority_followers_required,
                            "submit_ts": "",
                            "leader_commit_send_ts": "",
                            "slot": "",
                            "majority_follower_commit_ts": "",
                            "latency_ms": "",
                            "status": "fixed_submit_not_follower",
                        }
                    )
                    continue
                submit_node = fixed_submit_node
            elif submit_mode == "fixed_node":
                if fixed_submit_node not in node_ids:
                    records.append(
                        {
                            "command": "",
                            "cluster_size": cluster_size,
                            "message_length_bytes": message_length,
                            "submit_mode": submit_mode,
                            "command_index": i,
                            "submit_node": fixed_submit_node,
                            "leader_at_submit": leader_at_submit,
                            "majority_followers_required": majority_followers_required,
                            "submit_ts": "",
                            "leader_commit_send_ts": "",
                            "slot": "",
                            "majority_follower_commit_ts": "",
                            "latency_ms": "",
                            "status": "fixed_submit_not_in_cluster",
                        }
                    )
                    continue
                submit_node = fixed_submit_node
            elif submit_mode == "round_robin":
                submit_node = node_ids[i % len(node_ids)]
            else:
                submit_node = followers[i % len(followers)]

            cmd = make_command(message_length, i, submit_node)
            t_submit = time.perf_counter()
            nodes[submit_node].send_line(cmd)

            slot: Optional[int] = None
            leader_commit_send_ts: Optional[float] = None
            latest_status: Dict[int, StatusSnapshot] = {}
            follower_commit_ts: Dict[int, float] = {}
            last_status_poll: Dict[int, float] = {f: 0.0 for f in followers}

            deadline = t_submit + (timeout_ms / 1000.0)
            done = False

            while time.perf_counter() < deadline and not done:
                now = time.perf_counter()
                poll_interval_s = max(0.001, status_poll_interval_ms / 1000.0)
                for f in followers:
                    if now - last_status_poll[f] >= poll_interval_s:
                        nodes[f].send_line("/status")
                        last_status_poll[f] = now

                for nid, np in nodes.items():
                    lines = np.snapshot()
                    start = cursors[nid]
                    for ts, line in lines[start:]:
                        eval_m = EVAL_COMMIT_RE.search(line)
                        if eval_m:
                            commit_node = int(eval_m.group(1))
                            commit_slot = int(eval_m.group(2))
                            committed_cmd = eval_m.group(4).strip()
                            if commit_node == leader_at_submit and committed_cmd == cmd and slot is None:
                                slot = commit_slot
                                leader_commit_send_ts = ts
                            continue

                        # Fallback if trace line is not available.
                        commit_m = COMMIT_RE.search(line)
                        if commit_m:
                            commit_node = int(commit_m.group(1))
                            commit_slot = int(commit_m.group(2))
                            committed_cmd = commit_m.group(3).strip()
                            if commit_node == leader_at_submit and committed_cmd == cmd and slot is None:
                                slot = commit_slot
                                leader_commit_send_ts = ts
                            continue

                        st_m = STATUS_RE.search(line)
                        if st_m:
                            st_node = int(st_m.group(1))
                            latest_status[st_node] = StatusSnapshot(
                                ts=ts,
                                node_id=st_node,
                                is_leader=(st_m.group(2) == "yes"),
                                ballot=int(st_m.group(3)),
                                commit_index=int(st_m.group(4)),
                            )
                            continue

                    cursors[nid] = len(lines)

                if slot is not None:
                    for f in followers:
                        if f in follower_commit_ts:
                            continue
                        st = latest_status.get(f)
                        if st and st.commit_index >= slot:
                            follower_commit_ts[f] = st.ts

                    if len(follower_commit_ts) >= majority_followers_required:
                        kth_ts = sorted(follower_commit_ts.values())[majority_followers_required - 1]
                        latency_ms = (kth_ts - t_submit) * 1000.0
                        records.append(
                            {
                                "command": cmd,
                                "cluster_size": cluster_size,
                                "message_length_bytes": message_length,
                                "submit_mode": submit_mode,
                                "command_index": i,
                                "submit_node": submit_node,
                                "leader_at_submit": leader_at_submit,
                                "majority_followers_required": majority_followers_required,
                                "submit_ts": f"{t_submit:.9f}",
                                "leader_commit_send_ts": f"{leader_commit_send_ts:.9f}" if leader_commit_send_ts is not None else "",
                                "slot": slot,
                                "majority_follower_commit_ts": f"{kth_ts:.9f}",
                                "latency_ms": f"{latency_ms:.6f}",
                                "status": "ok",
                            }
                        )
                        done = True
                        break

                time.sleep(0.002)

            if not done:
                records.append(
                    {
                        "command": cmd,
                        "cluster_size": cluster_size,
                        "message_length_bytes": message_length,
                        "submit_mode": submit_mode,
                        "command_index": i,
                        "submit_node": submit_node,
                        "leader_at_submit": leader_at_submit,
                        "majority_followers_required": majority_followers_required,
                        "submit_ts": f"{t_submit:.9f}",
                        "leader_commit_send_ts": f"{leader_commit_send_ts:.9f}" if leader_commit_send_ts is not None else "",
                        "slot": slot if slot is not None else "",
                        "majority_follower_commit_ts": "",
                        "latency_ms": "",
                        "status": "timeout_or_leader_changed",
                    }
                )
                print(f"warning: timeout_or_leader_changed for cmd_idx={i}", file=sys.stderr)

    finally:
        if nodes:
            stop_nodes(nodes)

    return records


def load_rows_from_csv(csv_path: Path) -> List[Dict[str, object]]:
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def aggregate_rows(
    rows: List[Dict[str, object]],
) -> Tuple[str, str, List[int], List[int], List[int], Dict[int, List[float]], Dict[int, int], Dict[int, int]]:
    if not rows:
        return "", "", [], [], [], {}, {}, {}

    submit_modes = sorted({str(r["submit_mode"]) for r in rows if str(r.get("submit_mode", "")).strip()})
    submit_mode = submit_modes[0] if len(submit_modes) == 1 else "mixed"
    cluster_sizes = sorted({int(r["cluster_size"]) for r in rows if str(r.get("cluster_size", "")).strip()})
    msg_lengths = sorted({int(r["message_length_bytes"]) for r in rows if str(r.get("message_length_bytes", "")).strip()})

    if len(cluster_sizes) > 1 and len(msg_lengths) > 1:
        raise ValueError("CSV varies both cluster size and message length; sweep one axis at a time.")

    if len(cluster_sizes) > 1:
        sweep_axis = "cluster_size"
        keys = cluster_sizes
    else:
        sweep_axis = "message_length"
        keys = msg_lengths

    cdf_by_key: Dict[int, List[float]] = {k: [] for k in keys}
    total_by_key: Dict[int, int] = {k: 0 for k in keys}
    timeout_by_key: Dict[int, int] = {k: 0 for k in keys}

    for r in rows:
        key = int(r["cluster_size"]) if sweep_axis == "cluster_size" else int(r["message_length_bytes"])
        total_by_key[key] = total_by_key.get(key, 0) + 1
        if r.get("status") != "ok":
            timeout_by_key[key] = timeout_by_key.get(key, 0) + 1
            continue
        lat_raw = str(r.get("latency_ms", "")).strip()
        if not lat_raw:
            timeout_by_key[key] = timeout_by_key.get(key, 0) + 1
            continue
        cdf_by_key[key].append(float(lat_raw))

    return sweep_axis, submit_mode, cluster_sizes, msg_lengths, keys, cdf_by_key, total_by_key, timeout_by_key


def write_summary(summary_path: Path, source: str, rows: List[Dict[str, object]]) -> None:
    sweep_axis, submit_mode, cluster_sizes, msg_lengths, keys, cdf_by_key, total_by_key, timeout_by_key = aggregate_rows(rows)

    all_ok = [v for vals in cdf_by_key.values() for v in vals]
    st_all = summarize(all_ok)
    total = len(rows)
    ok = int(st_all["count"])
    timeout = total - ok
    timeout_rate = (timeout / total * 100.0) if total else 0.0

    with summary_path.open("w", encoding="utf-8") as f:
        f.write("Follower-Forward End-to-End Latency (Majority Followers Commit)\n")
        f.write("============================================================\n\n")
        f.write("Metric: t(majority followers know commit by status) - t(client submit to node)\n")
        f.write("Client: this eval script writes requests to a node selected by submit_mode.\n")
        f.write("Majority followers threshold: floor((N-1)/2)+1.\n")
        f.write("Follower commit time source: /status commit_index polling.\n\n")
        f.write(f"Source: {source}\n")
        f.write(f"Sweep axis: {sweep_axis}\n")
        f.write(f"Cluster sizes: {','.join(str(v) for v in cluster_sizes)}\n")
        f.write(f"Message lengths (bytes): {','.join(str(v) for v in msg_lengths)}\n")
        f.write(f"Submit mode: {submit_mode}\n")
        f.write(f"Total samples: {total}\n")
        f.write(f"Successful samples: {ok}\n")
        f.write(f"Timeout/fail samples: {timeout} ({timeout_rate:.2f}%)\n\n")

        f.write("All samples:\n")
        f.write(f"  mean: {st_all['mean']:.3f} ms\n")
        f.write(f"  p50:  {st_all['p50']:.3f} ms\n")
        f.write(f"  p95:  {st_all['p95']:.3f} ms\n")
        f.write(f"  p99:  {st_all['p99']:.3f} ms\n\n")

        for key in keys:
            vals = cdf_by_key.get(key, [])
            st = summarize(vals)
            n_total = total_by_key.get(key, 0)
            n_timeout = timeout_by_key.get(key, 0)
            n_timeout_rate = (n_timeout / n_total * 100.0) if n_total else 0.0
            label = f"Cluster size {key}:" if sweep_axis == "cluster_size" else f"Message length {key} bytes:"
            f.write(f"{label}\n")
            f.write(f"  successful samples: {int(st['count'])}/{n_total}\n")
            f.write(f"  timeout/fail rate:  {n_timeout_rate:.2f}%\n")
            f.write(f"  mean: {st['mean']:.3f} ms\n")
            f.write(f"  p50:  {st['p50']:.3f} ms\n")
            f.write(f"  p95:  {st['p95']:.3f} ms\n")
            f.write(f"  p99:  {st['p99']:.3f} ms\n\n")


def write_svg(fig_path: Path, rows: List[Dict[str, object]]) -> None:
    sweep_axis, submit_mode, cluster_sizes, msg_lengths, keys, cdf_by_key, total_by_key, timeout_by_key = aggregate_rows(rows)

    fixed_cluster = cluster_sizes[0] if len(cluster_sizes) == 1 else None
    fixed_length = msg_lengths[0] if len(msg_lengths) == 1 else None

    p50_by_key = {k: (percentile(cdf_by_key.get(k, []), 50) if cdf_by_key.get(k, []) else float("nan")) for k in keys}
    p95_by_key = {k: (percentile(cdf_by_key.get(k, []), 95) if cdf_by_key.get(k, []) else float("nan")) for k in keys}

    all_ok = [v for vals in cdf_by_key.values() for v in vals]
    st_all = summarize(all_ok)
    total = len(rows)
    ok = int(st_all["count"])
    timeout = total - ok

    width, height = 1200, 560
    left = (70, 90, 500, 340)
    right = (640, 90, 500, 340)
    x0, y0, w0, h0 = left
    x1, y1, w1, h1 = right

    stats_vals = [v for v in list(p50_by_key.values()) + list(p95_by_key.values()) if not math.isnan(v)]
    max_y = max([1.0] + stats_vals) * 1.15
    cdf_min_x = min(all_ok) - 20.0 if all_ok else -20.0
    cdf_max_x = max([1.0] + all_ok) * 1.05
    if cdf_max_x <= cdf_min_x:
        cdf_max_x = cdf_min_x + 1.0

    if sweep_axis == "cluster_size":
        title = "End-to-End Latency: Client Submit to Majority Followers Commit (vs Cluster Size)"
        subtitle = f"message_length={fixed_length}B"
        key_title = "cluster"
        key_label_fn = lambda k: f"N={k}"  # noqa: E731
        right_title = "p50/p95 vs cluster size"
        right_x_label = "cluster size (nodes)"
    else:
        title = "End-to-End Latency: Client Submit to Majority Followers Commit (vs Message Length)"
        subtitle = f"cluster_size={fixed_cluster}"
        key_title = "length"
        key_label_fn = lambda k: f"len={k}B"  # noqa: E731
        right_title = "p50/p95 vs message length"
        right_x_label = "message length (bytes)"

    svg: List[str] = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    svg.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
    svg.append(f'<text x="30" y="34" font-size="24" font-family="Arial">{title}</text>')
    svg.append(f'<text x="30" y="58" font-size="13" font-family="Arial">{subtitle}</text>')

    # Left: CDF
    svg.append(f'<rect x="{x0}" y="{y0}" width="{w0}" height="{h0}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y0 + h0 - (i / 5) * h0
        val = i / 5
        svg.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{x0+w0}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x0-28}" y="{gy+4:.1f}" font-size="10" font-family="Arial">{val:.1f}</text>')
    for i in range(6):
        gx = x0 + (i / 5) * w0
        val = cdf_min_x + (i / 5) * (cdf_max_x - cdf_min_x)
        svg.append(f'<line x1="{gx:.1f}" y1="{y0}" x2="{gx:.1f}" y2="{y0+h0}" stroke="#f3f3f3"/>')
        svg.append(f'<text x="{gx-14:.1f}" y="{y0+h0+18}" font-size="10" font-family="Arial">{val:.1f}</text>')

    sx = w0 / (cdf_max_x - cdf_min_x)
    sy = h0
    for i, key in enumerate(keys):
        vals = cdf_by_key.get(key, [])
        cdf = cdf_points(vals)
        if len(cdf) < 2:
            continue
        color = PALETTE[i % len(PALETTE)]
        coords = []
        for x, y in cdf:
            px = x0 + (x - cdf_min_x) * sx
            py = y0 + h0 - y * sy
            coords.append((px, py))
        d = " ".join(("M" if j == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for j, p in enumerate(coords))
        svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')

    svg.append(f'<text x="{x0}" y="{y0-12}" font-size="14" font-family="Arial">CDF</text>')
    svg.append(f'<text x="{x0 + w0/2 - 34}" y="{y0+h0+30}" font-size="12" font-family="Arial">latency (ms)</text>')
    svg.append(f'<text x="{x0-56}" y="{y0 + h0/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x0-56},{y0 + h0/2})">CDF</text>')

    # Right: p50/p95 vs key
    svg.append(f'<rect x="{x1}" y="{y1}" width="{w1}" height="{h1}" fill="none" stroke="#333"/>')
    for i in range(6):
        gy = y1 + h1 - (i / 5) * h1
        val = (i / 5) * max_y
        svg.append(f'<line x1="{x1}" y1="{gy:.1f}" x2="{x1+w1}" y2="{gy:.1f}" stroke="#eee"/>')
        svg.append(f'<text x="{x1-40}" y="{gy+4:.1f}" font-size="10" font-family="Arial">{val:.1f}</text>')

    n_keys = len(keys)
    x_points: List[float] = []
    for i, key in enumerate(keys):
        gx = x1 + (0 if n_keys == 1 else (i / (n_keys - 1)) * w1)
        x_points.append(gx)
        svg.append(f'<line x1="{gx:.1f}" y1="{y1}" x2="{gx:.1f}" y2="{y1+h1}" stroke="#f3f3f3"/>')
        svg.append(f'<text x="{gx-12:.1f}" y="{y1+h1+18}" font-size="10" font-family="Arial">{key}</text>')

    def draw_stat_line(values: List[float], color: str) -> None:
        pts: List[Tuple[float, float]] = []
        for i, v in enumerate(values):
            if math.isnan(v):
                continue
            px = x_points[i]
            py = y1 + h1 - (v / max_y) * h1
            pts.append((px, py))
        if len(pts) >= 2:
            d = " ".join(("M" if j == 0 else "L") + f" {p[0]:.2f} {p[1]:.2f}" for j, p in enumerate(pts))
            svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        for px, py in pts:
            svg.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2.8" fill="{color}"/>')

    p50s = [p50_by_key.get(k, float("nan")) for k in keys]
    p95s = [p95_by_key.get(k, float("nan")) for k in keys]
    draw_stat_line(p50s, "#1f77b4")
    draw_stat_line(p95s, "#d62728")

    svg.append(f'<text x="{x1}" y="{y1-12}" font-size="14" font-family="Arial">{right_title}</text>')
    svg.append(f'<text x="{x1 + w1/2 - 60}" y="{y1+h1+30}" font-size="12" font-family="Arial">{right_x_label}</text>')
    svg.append(f'<text x="{x1-58}" y="{y1 + h1/2}" font-size="12" font-family="Arial" transform="rotate(-90 {x1-58},{y1 + h1/2})">latency (ms)</text>')

    # Legend for keys in CDF panel
    lg_x = x0 + 12
    lg_y = y0 + 14
    lg_w = 250
    lg_h = 22 + len(keys) * 18 + 8
    svg.append(f'<rect x="{lg_x-6}" y="{lg_y-14}" width="{lg_w}" height="{lg_h}" fill="white" fill-opacity="0.88" stroke="#999"/>')
    svg.append(f'<text x="{lg_x}" y="{lg_y}" font-size="11" font-family="Arial">{key_title} / successful / timeout</text>')
    for i, key in enumerate(keys):
        color = PALETTE[i % len(PALETTE)]
        n_total = total_by_key.get(key, 0)
        n_ok = len(cdf_by_key.get(key, []))
        n_timeout = timeout_by_key.get(key, 0)
        n_timeout_rate = (n_timeout / n_total * 100.0) if n_total else 0.0
        yy = lg_y + (i + 1) * 18
        svg.append(f'<line x1="{lg_x}" y1="{yy-4}" x2="{lg_x+20}" y2="{yy-4}" stroke="{color}" stroke-width="2"/>')
        svg.append(f'<text x="{lg_x+26}" y="{yy}" font-size="11" font-family="Arial">{key_label_fn(key)} n={n_ok}/{n_total} timeout={n_timeout_rate:.1f}%</text>')

    # p50/p95 mini legend in right panel
    rg_x = x1 + 12
    rg_y = y1 + 14
    svg.append(f'<line x1="{rg_x}" y1="{rg_y}" x2="{rg_x+24}" y2="{rg_y}" stroke="#1f77b4" stroke-width="2"/>')
    svg.append(f'<text x="{rg_x+30}" y="{rg_y+4}" font-size="11" font-family="Arial">p50</text>')
    svg.append(f'<line x1="{rg_x+70}" y1="{rg_y}" x2="{rg_x+94}" y2="{rg_y}" stroke="#d62728" stroke-width="2"/>')
    svg.append(f'<text x="{rg_x+100}" y="{rg_y+4}" font-size="11" font-family="Arial">p95</text>')

    svg.append(
        f'<text x="70" y="500" font-size="12" font-family="Arial">overall n_ok={ok}/{total} timeout={timeout} mean={st_all["mean"]:.3f}ms p50={st_all["p50"]:.3f}ms p95={st_all["p95"]:.3f}ms p99={st_all["p99"]:.3f}ms</text>'
    )
    svg.append("</svg>")
    fig_path.write_text("\n".join(svg), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Submit-to-majority-followers-commit evaluator")
    p.add_argument("--binary", default="./build/paxos_node")
    p.add_argument("--base-port", type=int, default=18000)
    p.add_argument("--cluster-size", default="3", help="single value or CSV, e.g. 3 or 3,5,11")
    p.add_argument("--commands", type=int, default=100)
    p.add_argument("--timeout-ms", type=int, default=15000)
    p.add_argument("--message-length", type=int, default=256)
    p.add_argument("--message-lengths", default="", help="optional CSV sweep, e.g. 16,64,256")
    p.add_argument(
        "--submit-mode",
        choices=["round_robin", "follower_round_robin", "fixed_node", "fixed_follower", "fixed"],
        default="round_robin",
    )
    p.add_argument("--fixed-submit-node", type=int, default=2)
    p.add_argument("--status-poll-ms", type=int, default=20)
    p.add_argument("--mode", choices=["both", "run", "plot"], default="both")
    p.add_argument(
        "--input-csv",
        default="",
        help="CSV path for --mode plot (default: <out-dir>/submit_to_majority_follower_commit_samples_*.csv)",
    )
    p.add_argument("--out-dir", default="eval/eval_submit_to_majority_follower_commit")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ml_tag = args.message_lengths if args.message_lengths else str(args.message_length)
    tag = f"{ml_tag}_{args.cluster_size}_{args.submit_mode}"
    if args.mode == "plot" and args.input_csv:
        stem = Path(args.input_csv).stem
        prefix = "submit_to_majority_follower_commit_samples_"
        tag = stem[len(prefix):] if stem.startswith(prefix) else "from_csv"

    csv_path = out_dir / f"submit_to_majority_follower_commit_samples_{tag}.csv"
    summary_path = out_dir / f"submit_to_majority_follower_commit_summary_{tag}.txt"
    fig_path = out_dir / f"submit_to_majority_follower_commit_figure_{tag}.svg"

    rows: List[Dict[str, object]]

    if args.mode in ("both", "run"):
        binary = Path(args.binary)
        if not binary.exists():
            print(f"binary not found: {binary}", file=sys.stderr)
            return 2
        if args.status_poll_ms <= 0:
            print("status-poll-ms must be > 0", file=sys.stderr)
            return 2
        cluster_sizes = sorted(parse_int_csv(args.cluster_size, "--cluster-size", min_value=3))
        if args.message_lengths:
            message_lengths = sorted(parse_int_csv(args.message_lengths, "--message-lengths", min_value=1))
        else:
            if args.message_length < 1:
                print("message-length must be >= 1", file=sys.stderr)
                return 2
            message_lengths = [args.message_length]

        if len(cluster_sizes) > 1 and len(message_lengths) > 1:
            print("Please sweep one axis at a time: vary cluster-size OR message-lengths, not both.", file=sys.stderr)
            return 2

        submit_mode = args.submit_mode
        if submit_mode == "fixed":
            submit_mode = "fixed_follower"

        rows = []
        sweep_idx = 0
        for cluster_size in cluster_sizes:
            for message_length in message_lengths:
                run_rows = run_experiment(
                    binary=binary,
                    base_port=args.base_port + sweep_idx * 200,
                    cluster_size=cluster_size,
                    commands=args.commands,
                    timeout_ms=args.timeout_ms,
                    message_length=message_length,
                    submit_mode=submit_mode,
                    fixed_submit_node=args.fixed_submit_node,
                    status_poll_interval_ms=args.status_poll_ms,
                )
                rows.extend(run_rows)
                sweep_idx += 1

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "command",
                    "cluster_size",
                    "message_length_bytes",
                    "submit_mode",
                    "command_index",
                    "submit_node",
                    "leader_at_submit",
                    "majority_followers_required",
                    "submit_ts",
                    "leader_commit_send_ts",
                    "slot",
                    "majority_follower_commit_ts",
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
