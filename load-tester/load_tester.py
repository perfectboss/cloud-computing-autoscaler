"""
Load tester for the inference dispatcher, built on barazmoon.

Reads workload.txt (one space-separated integer per second), fetches a small
pool of sample ImageNet images, and fires multipart-encoded image POSTs to
the dispatcher at the specified RPS schedule. Each request's id, timestamps,
latency, and HTTP status are recorded to a per-second CSV slice. At the end,
all slices are concatenated into one results.csv.
"""
import argparse
import asyncio
import csv
import glob
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

import aiohttp
import httpx
from barazmoon import BarAzmoon


SAMPLE_NAMES = [
    "n02123045_tabby.JPEG",
    "n01440764_tench.JPEG",
    "n01514859_hen.JPEG",
    "n02085620_Chihuahua.JPEG",
    "n02123159_tiger_cat.JPEG",
    "n02124075_Egyptian_cat.JPEG",
    "n02085936_Maltese_dog.JPEG",
    "n02086079_Pekinese.JPEG",
    "n02086240_Shih-Tzu.JPEG",
    "n02086646_Blenheim_spaniel.JPEG",
]


def parse_workload(path: str) -> list[int]:
    raw = Path(path).read_text().strip()
    # Strip Excel-style leading "1\t" if present.
    if "\t" in raw:
        raw = raw.split("\t", 1)[1]
    return [int(x) for x in raw.split()]


def load_images(local_dir: str) -> list[bytes]:
    images: list[bytes] = []
    local_files = glob.glob(f"{local_dir}/*.JPEG") if local_dir else []
    for p in local_files[:10]:
        images.append(Path(p).read_bytes())
    if images:
        print(f"Loaded {len(images)} images from {local_dir}", flush=True)
        return images
    print(f"No local images at {local_dir}; fetching from GitHub...", flush=True)
    base = "https://raw.githubusercontent.com/EliSchwartz/imagenet-sample-images/master/"
    with httpx.Client() as c:
        for n in SAMPLE_NAMES:
            try:
                r = c.get(base + n, timeout=15)
                if r.status_code == 200:
                    images.append(r.content)
                    print(f"  fetched {n} ({len(r.content)} bytes)", flush=True)
            except Exception as e:
                print(f"  failed {n}: {e}", flush=True)
    if not images:
        sys.exit("Could not load any images, aborting")
    return images


class InferenceLoadTester(BarAzmoon):
    """Subclass that builds multipart image uploads and records per-request data."""

    def __init__(self, *, workload, endpoint, results_dir, images, **kwargs):
        super().__init__(workload=workload, endpoint=endpoint, http_method="post", **kwargs)
        self.results_dir = results_dir
        self.images = images
        os.makedirs(results_dir, exist_ok=True)

    def get_request_data(self):
        img = random.choice(self.images)
        data = aiohttp.FormData()
        data.add_field("file", img, filename="img.jpg", content_type="image/jpeg")
        return str(uuid.uuid4()), data

    async def predict(self, delay, session):
        await asyncio.sleep(delay)
        rid, data = self.get_request_data()
        t_start = time.time()
        status = 0
        server_latency = ""  # seconds, as reported by the inference pod
        try:
            async with session.post(self.endpoint, data=data) as response:
                body = await response.read()
                status = response.status
                t_end = time.time()
                ok = 200 <= status < 300
                if ok:
                    # The inference service reports its own pure compute time;
                    # this is the "server-side latency" the SLO is defined on.
                    try:
                        server_latency = json.loads(body).get("server_latency_s", "")
                    except Exception:
                        server_latency = ""
        except Exception:
            t_end = time.time()
            ok = False
        self._record(rid, t_start, t_end, status, server_latency)
        return 1 if ok else 0

    def _record(self, rid, t_start, t_end, status, server_latency=""):
        # Defensive: barazmoon SIGTERMs surviving subprocesses, and a partial
        # write under that race can corrupt rows. Drop rows we can already see
        # are invalid (and never emit negative latency).
        if t_end < t_start:
            return
        slice_path = os.path.join(self.results_dir, f"slice_{os.getpid()}.csv")
        with open(slice_path, "a") as f:
            f.write(f"{rid},{t_start:.6f},{t_end:.6f},{(t_end - t_start) * 1000:.2f},{status},{server_latency}\n")


def concat_slices(results_dir: str, out_path: str) -> int:
    rows = []
    for p in glob.glob(f"{results_dir}/slice_*.csv"):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(line.split(","))
    rows.sort(key=lambda r: float(r[1]))  # by t_start
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["request_id", "t_start", "t_end", "latency_ms", "status", "server_latency_s"])
        for r in rows:
            w.writerow(r)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", default="/workload/workload.txt")
    ap.add_argument("--endpoint", default="http://dispatcher-service:8000/predict")
    ap.add_argument("--results-dir", default="/results")
    ap.add_argument("--images-dir", default="/images")
    ap.add_argument("--limit-seconds", type=int, default=0,
                    help="Truncate workload to first N seconds (0 = no truncation)")
    ap.add_argument("--experiment-name", default="run",
                    help="Tag for the results sentinel block (e.g. 'custom', 'hpa70')")
    args = ap.parse_args()

    workload = parse_workload(args.workload)
    if args.limit_seconds > 0:
        workload = workload[: args.limit_seconds]
    total = sum(workload)
    print(f"Workload: {len(workload)} seconds, {total} total requests, "
          f"peak {max(workload)} RPS", flush=True)

    images = load_images(args.images_dir)

    print(f"Endpoint: {args.endpoint}", flush=True)
    print(f"Results dir: {args.results_dir}", flush=True)

    # Clean any prior slices.
    for p in glob.glob(f"{args.results_dir}/slice_*.csv"):
        os.remove(p)

    tester = InferenceLoadTester(
        workload=workload,
        endpoint=args.endpoint,
        results_dir=args.results_dir,
        images=images,
        timeout=15,
    )
    sent, ok = tester.start()
    print(f"Sent {sent} requests; {ok} reported success by barazmoon", flush=True)

    out_path = os.path.join(args.results_dir, "results.csv")
    n_rows = concat_slices(args.results_dir, out_path)
    print(f"Wrote {n_rows} rows to {out_path}", flush=True)

    # Dump the CSV to stdout between sentinels so it's retrievable via
    # `kubectl logs` even after the Job pod terminates.
    tag = args.experiment_name
    print(f"=== RESULTS_CSV_BEGIN {tag} ===", flush=True)
    with open(out_path) as f:
        sys.stdout.write(f.read())
    print(f"=== RESULTS_CSV_END {tag} ===", flush=True)


if __name__ == "__main__":
    main()
