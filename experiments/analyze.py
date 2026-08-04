"""Aggregate results from all trial directories into a comparison report."""
import argparse
import csv
import os
import statistics
from pathlib import Path

SLO_MS = 500


def stats_for_trial(trial_dir: Path) -> dict:
    results_csv = trial_dir / "results.csv"
    replicas_csv = trial_dir / "replicas.csv"

    requests = []
    bad_rows = 0
    if results_csv.exists():
        with open(results_csv) as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    t_start = float(r["t_start"])
                    t_end = float(r["t_end"])
                    latency = float(r["latency_ms"])
                    status = int(r["status"])
                except (ValueError, KeyError):
                    bad_rows += 1
                    continue
                # Filter physically-impossible rows (barazmoon SIGTERM race).
                if t_end < t_start or latency < 0:
                    bad_rows += 1
                    continue
                # server_latency_s is optional (older CSVs / failed requests omit it).
                server_ms = None
                raw_srv = r.get("server_latency_s", "")
                if raw_srv not in (None, ""):
                    try:
                        server_ms = float(raw_srv) * 1000.0
                    except ValueError:
                        server_ms = None
                requests.append({
                    "t_start": t_start, "t_end": t_end,
                    "latency_ms": latency, "status": status,
                    "server_ms": server_ms,
                })

    replicas_over_time = []
    if replicas_csv.exists():
        with open(replicas_csv) as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    replicas_over_time.append({
                        "ts": float(r["ts"]),
                        "replicas": int(r["replicas"]),
                        "ready": int(r["ready"]),
                    })
                except (ValueError, KeyError):
                    continue

    if not requests:
        return {"name": trial_dir.name, "error": "no results"}

    successful = [r for r in requests if 200 <= r["status"] < 300]
    failed = [r for r in requests if r["status"] == 0 or r["status"] >= 500]
    sub_slo = [r for r in successful if r["latency_ms"] < SLO_MS]
    lats = sorted(r["latency_ms"] for r in successful)

    # Server-side latency: pure inference compute time reported by the pod.
    # This is the latency the PDF's SLO is actually defined on.
    server_lats = sorted(r["server_ms"] for r in successful if r["server_ms"] is not None)
    server_sub_slo = [v for v in server_lats if v < SLO_MS]

    def pct(p):
        if not lats:
            return 0.0
        idx = min(len(lats) - 1, int(len(lats) * p))
        return lats[idx]

    def spct(p):
        if not server_lats:
            return 0.0
        idx = min(len(server_lats) - 1, int(len(server_lats) * p))
        return server_lats[idx]

    replica_values = [r["replicas"] for r in replicas_over_time]
    replicas_sum = sum(replica_values)  # ~replica-seconds, scaled (1 sample/sec)
    return {
        "name": trial_dir.name,
        "bad_rows": bad_rows,
        "total_requests": len(requests),
        "successful": len(successful),
        "failed": len(failed),
        "success_rate": len(successful) / len(requests) * 100 if requests else 0,
        "sub_slo_count": len(sub_slo),
        "slo_compliance": len(sub_slo) / len(successful) * 100 if successful else 0,
        "lat_mean": statistics.mean(lats) if lats else 0,
        "lat_p50": pct(0.50),
        "lat_p95": pct(0.95),
        "lat_p99": pct(0.99),
        "lat_max": max(lats) if lats else 0,
        "server_samples": len(server_lats),
        "server_slo_compliance": len(server_sub_slo) / len(server_lats) * 100 if server_lats else 0,
        "server_p50": spct(0.50),
        "server_p95": spct(0.95),
        "server_p99": spct(0.99),
        "server_max": max(server_lats) if server_lats else 0,
        "replicas_max": max(replica_values) if replica_values else 0,
        "replicas_avg": statistics.mean(replica_values) if replica_values else 0,
        "replicas_seconds": replicas_sum,  # cost proxy
        "duration_s": int(requests[-1]["t_end"] - requests[0]["t_start"]) if requests else 0,
    }


def fmt(stat, key, suffix="", fmt_str="{:.1f}"):
    val = stat.get(key)
    if val is None:
        return "—"
    return fmt_str.format(val) + suffix


def print_table(stats: list[dict]) -> None:
    rows = [
        ("Trial",            lambda s: s["name"]),
        ("Total requests",   lambda s: f"{s['total_requests']}"),
        ("Dropped (bad rows)", lambda s: f"{s.get('bad_rows', 0)}"),
        ("Successful",       lambda s: f"{s['successful']}"),
        ("Failed",           lambda s: f"{s['failed']}"),
        ("Success rate",     lambda s: f"{s['success_rate']:.2f}%"),
        ("-- END-TO-END (client round-trip) --", lambda s: ""),
        ("E2E SLO compliance (<500ms)", lambda s: f"{s['slo_compliance']:.2f}%"),
        ("E2E latency p50",  lambda s: f"{s['lat_p50']:.1f} ms"),
        ("E2E latency p95",  lambda s: f"{s['lat_p95']:.1f} ms"),
        ("E2E latency p99",  lambda s: f"{s['lat_p99']:.1f} ms"),
        ("E2E latency max",  lambda s: f"{s['lat_max']:.1f} ms"),
        ("-- SERVER-SIDE (inference compute) --", lambda s: ""),
        ("SRV SLO compliance (<500ms)", lambda s: f"{s.get('server_slo_compliance', 0):.2f}%"),
        ("SRV latency p50",  lambda s: f"{s.get('server_p50', 0):.1f} ms"),
        ("SRV latency p95",  lambda s: f"{s.get('server_p95', 0):.1f} ms"),
        ("SRV latency p99",  lambda s: f"{s.get('server_p99', 0):.1f} ms"),
        ("SRV latency max",  lambda s: f"{s.get('server_max', 0):.1f} ms"),
        ("-- SCALING --",    lambda s: ""),
        ("Max replicas",     lambda s: f"{s['replicas_max']}"),
        ("Avg replicas",     lambda s: f"{s['replicas_avg']:.2f}"),
        ("Replica-seconds (cost proxy)", lambda s: f"{s['replicas_seconds']}"),
        ("Duration",         lambda s: f"{s['duration_s']}s"),
    ]

    label_w = max(len(r[0]) for r in rows)
    col_w = max(20, max(len(r[1](s)) for r in rows for s in stats) + 2)

    header = "Metric".ljust(label_w) + "".join(s["name"].ljust(col_w) for s in stats)
    print(header)
    print("-" * len(header))
    for label, getter in rows:
        line = label.ljust(label_w) + "".join(getter(s).ljust(col_w) for s in stats)
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="E:/cloud-computing-project/experiments/results")
    args = ap.parse_args()
    root = Path(args.root)
    trial_dirs = sorted([p for p in root.iterdir() if p.is_dir() and (p / "results.csv").exists()])
    if not trial_dirs:
        print(f"No trial directories under {root}")
        return

    stats = []
    skipped = []
    for td in trial_dirs:
        s = stats_for_trial(td)
        if "error" in s:
            skipped.append((td.name, s["error"]))
            continue
        stats.append(s)

    if skipped:
        print("# Skipped (incomplete):")
        for name, err in skipped:
            print(f"#   {name}: {err}")
        print()

    if not stats:
        print("No complete trials found yet.")
        return

    print_table(stats)
    print()
    print("# SLO target: 500 ms server-side latency per the PDF.")
    print("# Replica-seconds = cumulative replicas × 1-second samples (proxy for resource cost).")
    # Save a flat summary CSV too.
    summary_csv = root / "summary.csv"
    keys = list(stats[0].keys()) if stats else []
    with open(summary_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for s in stats:
            w.writerow(s)
    print(f"\nSummary CSV: {summary_csv}")


if __name__ == "__main__":
    main()
