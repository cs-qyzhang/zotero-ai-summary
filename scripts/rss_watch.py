#!/usr/bin/env python3
"""Poll the RSS of a process tree and report peaks.

Usage: rss_watch.py <root_pid> <output.json> [interval_sec]
Stops on SIGTERM and writes {"peak_total_kb", "peak_child_kb", "samples"}.
"""
import json
import os
import signal
import sys
import time


def read_ppid(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'
    rest = data[data.rfind(b")") + 2 :].split()
    try:
        return int(rest[1])
    except (IndexError, ValueError):
        return None


def read_rss(pid):
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for line in f:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def tree(pid):
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        child = int(entry)
        parent = read_ppid(child)
        if parent is not None:
            children.setdefault(parent, []).append(child)
    out, stack = [], [pid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


def main():
    root = int(sys.argv[1])
    out_path = sys.argv[2]
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    peak_total = peak_child = 0
    samples = 0
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        pids = tree(root)
        total = 0
        child_max = 0
        for pid in pids:
            rss = read_rss(pid)
            total += rss
            if pid != root:
                child_max = max(child_max, rss)
        peak_total = max(peak_total, total)
        peak_child = max(peak_child, child_max)
        samples += 1
        time.sleep(interval)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "peak_total_kb": peak_total,
                "peak_child_kb": peak_child,
                "samples": samples,
            },
            f,
        )


if __name__ == "__main__":
    main()
