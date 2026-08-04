"""
Custom multi-signal autoscaler for the inference deployment.

Polls Prometheus every TICK_INTERVAL seconds and computes the desired replica
count from four signals:

  1. Latency:   p95 server latency vs LATENCY_TARGET_S.
  2. Queue:     dispatcher queue depth + in-flight load per replica.
  3. CPU:       average CPU utilization vs CPU_TARGET.
  4. Floor:     never below MIN_REPLICAS.

Scale-up is aggressive: take the max of all signals and jump immediately.
Scale-down is conservative: require SCALE_DOWN_QUIET_TICKS consecutive ticks
agreeing on a lower target, and only reduce by 1 replica per tick.
"""

import logging
import math
import os
import time
from urllib.parse import urlencode

import requests
from kubernetes import client, config

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("autoscaler")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
NAMESPACE = os.environ.get("TARGET_NAMESPACE", "default")
DEPLOYMENT = os.environ.get("TARGET_DEPLOYMENT", "inference-deployment")
MIN_REPLICAS = int(os.environ.get("MIN_REPLICAS", "1"))
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "8"))
TICK_INTERVAL = float(os.environ.get("TICK_INTERVAL_S", "15"))
LATENCY_TARGET_S = float(os.environ.get("LATENCY_TARGET_S", "0.35"))
CPU_TARGET = float(os.environ.get("CPU_TARGET", "0.70"))
QUEUE_TARGET_PER_REPLICA = float(os.environ.get("QUEUE_TARGET_PER_REPLICA", "1.5"))
SCALE_DOWN_QUIET_TICKS = int(os.environ.get("SCALE_DOWN_QUIET_TICKS", "4"))


def prom_query(query: str) -> float | None:
    """Execute an instant PromQL query, return scalar value or None."""
    try:
        url = f"{PROMETHEUS_URL}/api/v1/query?{urlencode({'query': query})}"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        results = r.json().get("data", {}).get("result", [])
        if not results:
            return None
        values = [float(item["value"][1]) for item in results if item.get("value")]
        values = [v for v in values if not math.isnan(v)]
        return sum(values) / len(values) if values else None
    except Exception as exc:
        log.warning("prometheus query failed (%s): %s", query, exc)
        return None


def gather_signals(current_replicas: int) -> dict:
    """Read all four signals from Prometheus."""
    p95 = prom_query(
        "histogram_quantile(0.95, sum(rate(inference_predict_latency_seconds_bucket[1m])) by (le))"
    )
    queue_depth = prom_query("dispatcher_queue_depth")
    in_flight = prom_query("dispatcher_inflight_requests")
    # CPU utilization as fraction of the 1-core limit per inference pod.
    cpu_util = prom_query(
        'avg(rate(container_cpu_usage_seconds_total{pod=~"inference-deployment-.*",container="inference"}[1m]))'
    )
    return {
        "p95_latency_s": p95,
        "queue_depth": queue_depth or 0.0,
        "in_flight": in_flight or 0.0,
        "cpu_util": cpu_util,
    }


def desired_replicas(signals: dict, current: int) -> tuple[int, str]:
    """Compute desired replica count and the reason."""
    candidates: list[tuple[int, str]] = [(MIN_REPLICAS, "floor")]

    # Queue signal: each replica should comfortably absorb QUEUE_TARGET_PER_REPLICA
    # of queued + in-flight load.
    load = signals["queue_depth"] + signals["in_flight"]
    if load > 0:
        from_queue = max(1, int(-(-load // QUEUE_TARGET_PER_REPLICA)))  # ceil
        candidates.append((from_queue, f"queue={load:.1f}"))

    # Latency signal: if p95 over target, scale proportionally.
    p95 = signals["p95_latency_s"]
    if p95 is not None and p95 > LATENCY_TARGET_S:
        ratio = p95 / LATENCY_TARGET_S
        from_latency = max(current + 1, int(-(-current * ratio // 1)))
        candidates.append((from_latency, f"p95={p95*1000:.0f}ms"))

    # CPU signal: classic HPA formula.
    cpu = signals["cpu_util"]
    if cpu is not None and cpu > 0:
        from_cpu = max(1, int(-(-current * cpu / CPU_TARGET // 1)))
        candidates.append((from_cpu, f"cpu={cpu*100:.0f}%"))

    target, reason = max(candidates, key=lambda x: x[0])
    target = max(MIN_REPLICAS, min(MAX_REPLICAS, target))
    return target, reason


def get_current_replicas(apps: client.AppsV1Api) -> int:
    dep = apps.read_namespaced_deployment(name=DEPLOYMENT, namespace=NAMESPACE)
    return dep.spec.replicas or 0


def patch_replicas(apps: client.AppsV1Api, replicas: int) -> None:
    body = {"spec": {"replicas": replicas}}
    apps.patch_namespaced_deployment_scale(
        name=DEPLOYMENT, namespace=NAMESPACE, body=body
    )


def main() -> None:
    config.load_incluster_config()
    apps = client.AppsV1Api()
    log.info(
        "autoscaler starting | target=%s/%s | min=%d max=%d | tick=%.0fs | latency_target=%.2fs",
        NAMESPACE, DEPLOYMENT, MIN_REPLICAS, MAX_REPLICAS, TICK_INTERVAL, LATENCY_TARGET_S,
    )

    pending_target: int | None = None
    pending_ticks = 0

    while True:
        try:
            current = get_current_replicas(apps)
            signals = gather_signals(current)
            target, reason = desired_replicas(signals, current)

            p95_s = signals["p95_latency_s"]
            cpu_s = signals["cpu_util"]
            p95_fmt = "n/a" if p95_s is None else f"{p95_s * 1000:.0f}ms"
            cpu_fmt = "n/a" if cpu_s is None else f"{cpu_s * 100:.0f}%"
            sig_s = (
                f"p95={p95_fmt} q={signals['queue_depth']:.1f} "
                f"inflight={signals['in_flight']:.1f} cpu={cpu_fmt}"
            )

            if target > current:
                log.info("SCALE UP %d -> %d (reason=%s) | %s", current, target, reason, sig_s)
                patch_replicas(apps, target)
                pending_target = None
                pending_ticks = 0
            elif target < current:
                if pending_target == target:
                    pending_ticks += 1
                else:
                    pending_target = target
                    pending_ticks = 1
                if pending_ticks >= SCALE_DOWN_QUIET_TICKS:
                    step_target = max(target, current - 1)
                    log.info(
                        "SCALE DOWN %d -> %d (reason=%s, %d quiet ticks) | %s",
                        current, step_target, reason, pending_ticks, sig_s,
                    )
                    patch_replicas(apps, step_target)
                    pending_target = None
                    pending_ticks = 0
                else:
                    log.info(
                        "hold %d (want %d, %d/%d quiet ticks) | %s",
                        current, target, pending_ticks, SCALE_DOWN_QUIET_TICKS, sig_s,
                    )
            else:
                pending_target = None
                pending_ticks = 0
                log.info("hold %d | %s", current, sig_s)
        except Exception as exc:
            log.exception("tick failed: %s", exc)

        time.sleep(TICK_INTERVAL)


if __name__ == "__main__":
    main()
