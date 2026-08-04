"""Poll the inference deployment every second and emit (ts, replicas, ready) to stdout."""
import argparse
import subprocess
import sys
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, required=True)
    ap.add_argument("--deployment", default="inference-deployment")
    ap.add_argument("--namespace", default="default")
    args = ap.parse_args()

    end = time.time() + args.duration
    print("ts,replicas,ready", flush=True)
    while time.time() < end:
        tick = time.time()
        try:
            out = subprocess.run(
                ["kubectl", "get", "deploy", args.deployment,
                 "-n", args.namespace,
                 "-o", "jsonpath={.spec.replicas},{.status.readyReplicas}"],
                capture_output=True, text=True, timeout=4,
            )
            parts = (out.stdout.strip() + ",0").split(",")
            replicas = parts[0] or "0"
            ready = parts[1] or "0"
            print(f"{tick:.3f},{replicas},{ready}", flush=True)
        except Exception as exc:
            print(f"{tick:.3f},err,err  # {exc}", flush=True)
        elapsed = time.time() - tick
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)


if __name__ == "__main__":
    main()
