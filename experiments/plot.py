"""Generate comparison charts from the trial CSVs."""
import argparse
import csv
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

TRIALS = ["custom", "hpa70", "hpa90"]
COLORS = {"custom": "#2563eb", "hpa70": "#dc2626", "hpa90": "#f59e0b"}
LABELS = {"custom": "Custom autoscaler",
          "hpa70": "HPA @ 70% CPU",
          "hpa90": "HPA @ 90% CPU"}
SLO_MS = 500


def load_trial(root: Path, name: str) -> dict:
    d = root / name
    replicas = []
    with open(d / "replicas.csv") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                replicas.append((float(r["ts"]), int(r["replicas"]), int(r["ready"])))
            except ValueError:
                continue
    requests = []
    with open(d / "results.csv") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                ts = float(r["t_start"])
                lat = float(r["latency_ms"])
                stat = int(r["status"])
                if lat < 0:
                    continue
                raw_srv = r.get("server_latency_s", "")
                srv = float(raw_srv) * 1000.0 if raw_srv not in (None, "") else None
                requests.append((ts, lat, stat, srv))
            except (ValueError, KeyError):
                continue
    requests.sort()
    return {"replicas": replicas, "requests": requests}


def normalize_time(series, key_idx=0):
    if not series:
        return series, 0.0
    t0 = series[0][key_idx]
    return [(t - t0, *rest) for (t, *rest) in series], t0


def plot_replicas(data: dict, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5))
    for name in TRIALS:
        if name not in data:
            continue
        reps, _ = normalize_time(data[name]["replicas"])
        if not reps:
            continue
        ts = [r[0] for r in reps]
        rs = [r[1] for r in reps]
        ax.step(ts, rs, where="post", color=COLORS[name], label=LABELS[name], linewidth=2)
    ax.set_xlabel("Time since trial start (s)")
    ax.set_ylabel("CPU cores in use (1 core per replica)")
    ax.set_title("CPU cores over time — custom vs HPA")
    ax.set_ylim(0, 9)
    ax.set_yticks(range(0, 9))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def plot_rolling_latency(data: dict, out_path: Path, window_s: int = 10) -> None:
    # 99th-percentile end-to-end latency time-series: the headline
    # comparison metric between autoscalers.
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.axhline(SLO_MS, color="black", linestyle="--", linewidth=1, alpha=0.6, label=f"SLO {SLO_MS} ms")
    for name in TRIALS:
        if name not in data:
            continue
        reqs, _ = normalize_time(data[name]["requests"])
        if not reqs:
            continue
        ts = np.array([r[0] for r in reqs])
        lats = np.array([r[1] for r in reqs])
        max_t = ts[-1]
        buckets_x = []
        buckets_p99 = []
        for start in range(0, int(max_t) + 1, window_s):
            mask = (ts >= start) & (ts < start + window_s)
            if mask.sum() >= 5:
                buckets_x.append(start + window_s / 2)
                buckets_p99.append(np.percentile(lats[mask], 99))
        ax.plot(buckets_x, buckets_p99, color=COLORS[name], label=LABELS[name], linewidth=2)
    ax.set_xlabel("Time since trial start (s)")
    ax.set_ylabel(f"p99 latency in {window_s}-second window (ms)")
    ax.set_title(f"Rolling 99th-percentile latency — {window_s}-second windows")
    ax.set_yscale("log")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def plot_latency_cdf(data: dict, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.axvline(SLO_MS, color="black", linestyle="--", linewidth=1, alpha=0.6, label=f"SLO {SLO_MS} ms")
    for name in TRIALS:
        if name not in data:
            continue
        reqs = data[name]["requests"]
        lats = sorted(r[1] for r in reqs if 200 <= r[2] < 300)
        if not lats:
            continue
        ys = np.arange(1, len(lats) + 1) / len(lats) * 100
        ax.plot(lats, ys, color=COLORS[name], label=LABELS[name], linewidth=2)
    ax.set_xscale("log")
    ax.set_xlabel("Latency (ms, log scale)")
    ax.set_ylabel("% of requests at or below")
    ax.set_title("Latency CDF — successful requests only")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def plot_server_cdf(data: dict, out_path: Path) -> None:
    """CDF of server-side latency: inference compute time, the SLO metric."""
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.axvline(SLO_MS, color="black", linestyle="--", linewidth=1, alpha=0.6, label=f"SLO {SLO_MS} ms")
    for name in TRIALS:
        if name not in data:
            continue
        reqs = data[name]["requests"]
        srv = sorted(r[3] for r in reqs if 200 <= r[2] < 300 and r[3] is not None)
        if not srv:
            continue
        ys = np.arange(1, len(srv) + 1) / len(srv) * 100
        ax.plot(srv, ys, color=COLORS[name], label=LABELS[name], linewidth=2)
    ax.set_xlabel("Server-side latency (ms)")
    ax.set_ylabel("% of requests at or below")
    ax.set_title("Server-side latency CDF — inference compute time (the SLO metric)")
    ax.set_xlim(0, 600)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def plot_summary_bars(data: dict, out_path: Path) -> None:
    metrics = [
        ("Success rate (%)", lambda d: sum(1 for r in d["requests"] if 200 <= r[2] < 300) / len(d["requests"]) * 100),
        ("SLO compliance (%)", lambda d: sum(1 for r in d["requests"] if 200 <= r[2] < 300 and r[1] < SLO_MS) / max(1, sum(1 for r in d["requests"] if 200 <= r[2] < 300)) * 100),
        ("p95 latency (ms)", lambda d: float(np.percentile([r[1] for r in d["requests"] if 200 <= r[2] < 300], 95))),
        ("Avg replicas", lambda d: float(np.mean([r[1] for r in d["replicas"]]))),
    ]
    fig, axes = plt.subplots(1, 4, figsize=(15, 4))
    trial_names = [n for n in TRIALS if n in data]
    x = np.arange(len(trial_names))
    bar_colors = [COLORS[n] for n in trial_names]
    for ax, (title, fn) in zip(axes, metrics):
        values = [fn(data[n]) for n in trial_names]
        bars = ax.bar(x, values, color=bar_colors)
        ax.set_xticks(x)
        ax.set_xticklabels([LABELS[n].replace(" autoscaler", "").replace("HPA @ ", "HPA ") for n in trial_names], rotation=15)
        ax.set_title(title)
        ax.grid(True, axis="y", alpha=0.3)
        for bar, v in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f"{v:.1f}", ha="center", va="bottom", fontsize=9)
        if "p95" in title or "Avg" in title:
            ax.set_ylim(0, max(values) * 1.2)
        else:
            ax.set_ylim(0, 105)
    fig.suptitle("Per-trial summary", fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--root", default=str(here / "results"))
    ap.add_argument("--out", default=str(here / "results" / "charts"))
    args = ap.parse_args()

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    for name in TRIALS:
        if (root / name / "results.csv").exists() and (root / name / "replicas.csv").exists():
            data[name] = load_trial(root, name)

    if not data:
        print("No trial data found")
        return

    plot_replicas(data, out / "cpu_cores.png")
    plot_rolling_latency(data, out / "rolling_p99.png")
    plot_latency_cdf(data, out / "latency_cdf.png")
    plot_server_cdf(data, out / "server_latency_cdf.png")
    plot_summary_bars(data, out / "summary.png")
    print(f"Charts written to {out}/")
    for f in sorted(out.iterdir()):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
