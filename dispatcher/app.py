import asyncio
import os
import time
from collections import deque

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CONTENT_TYPE_LATEST,
    generate_latest,
)

INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://inference-service:8000/predict")
QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "256"))
FORWARD_TIMEOUT_S = float(os.environ.get("FORWARD_TIMEOUT_S", "10"))

app = FastAPI()

state = {
    "in_flight": 0,
    "queue_depth": 0,
    "queue_max": QUEUE_MAX,
    "total_received": 0,
    "total_completed": 0,
    "total_dropped": 0,
    "total_failed": 0,
    "recent_latencies_s": deque(maxlen=2000),
}

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None

# Prometheus metrics — the autoscaler will scrape these in Phase 5.
m_received = Counter("dispatcher_requests_received_total", "Requests received by dispatcher")
m_completed = Counter("dispatcher_requests_completed_total", "Requests completed successfully")
m_dropped = Counter("dispatcher_requests_dropped_total", "Requests rejected because queue was full")
m_failed = Counter("dispatcher_requests_failed_total", "Requests that errored at upstream")
m_inflight = Gauge("dispatcher_inflight_requests", "Requests currently being processed upstream")
m_queue_depth = Gauge("dispatcher_queue_depth", "Requests waiting for a worker slot")
m_queue_max = Gauge("dispatcher_queue_capacity", "Configured queue capacity")
m_queue_max.set(QUEUE_MAX)
m_latency = Histogram(
    "dispatcher_request_latency_seconds",
    "End-to-end latency observed by dispatcher (queue + upstream)",
    buckets=(0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0),
)


@app.on_event("startup")
async def startup() -> None:
    global _client, _semaphore
    # NB: keep-alive disabled so every request triggers a fresh TCP connection
    # to inference-service. ClusterIP load-balances per-connection, not per-request,
    # so keep-alive would pin all traffic to one replica.
    limits = httpx.Limits(max_connections=QUEUE_MAX, max_keepalive_connections=0)
    _client = httpx.AsyncClient(timeout=FORWARD_TIMEOUT_S, limits=limits)
    _semaphore = asyncio.Semaphore(QUEUE_MAX)


@app.on_event("shutdown")
async def shutdown() -> None:
    if _client is not None:
        await _client.aclose()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stats")
def stats() -> dict:
    latencies = list(state["recent_latencies_s"])
    p50 = p99 = avg = 0.0
    if latencies:
        sorted_lat = sorted(latencies)
        p50 = sorted_lat[len(sorted_lat) // 2]
        p99 = sorted_lat[max(0, int(len(sorted_lat) * 0.99) - 1)]
        avg = sum(sorted_lat) / len(sorted_lat)
    return {
        "in_flight": state["in_flight"],
        "queue_depth": state["queue_depth"],
        "queue_max": state["queue_max"],
        "total_received": state["total_received"],
        "total_completed": state["total_completed"],
        "total_dropped": state["total_dropped"],
        "total_failed": state["total_failed"],
        "latency_avg_s": avg,
        "latency_p50_s": p50,
        "latency_p99_s": p99,
        "sample_window": len(latencies),
    }


@app.post("/predict")
async def predict(request: Request) -> Response:
    state["total_received"] += 1
    m_received.inc()

    if _semaphore is None or _client is None:
        raise HTTPException(status_code=503, detail="dispatcher not ready")

    if _semaphore.locked() and state["in_flight"] >= QUEUE_MAX:
        state["total_dropped"] += 1
        m_dropped.inc()
        raise HTTPException(status_code=503, detail="queue full")

    raw = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")

    state["queue_depth"] += 1
    m_queue_depth.set(state["queue_depth"])
    try:
        async with _semaphore:
            state["queue_depth"] -= 1
            m_queue_depth.set(state["queue_depth"])
            state["in_flight"] += 1
            m_inflight.set(state["in_flight"])
            start = time.perf_counter()
            try:
                resp = await _client.post(
                    INFERENCE_URL,
                    content=raw,
                    headers={"Content-Type": content_type},
                )
                elapsed = time.perf_counter() - start
                state["recent_latencies_s"].append(elapsed)
                m_latency.observe(elapsed)
                if resp.status_code >= 500:
                    state["total_failed"] += 1
                    m_failed.inc()
                else:
                    state["total_completed"] += 1
                    m_completed.inc()
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"),
                )
            except httpx.HTTPError as exc:
                state["total_failed"] += 1
                m_failed.inc()
                raise HTTPException(status_code=502, detail=f"upstream error: {exc}")
            finally:
                state["in_flight"] -= 1
                m_inflight.set(state["in_flight"])
    except HTTPException:
        if state["queue_depth"] > 0:
            state["queue_depth"] -= 1
            m_queue_depth.set(state["queue_depth"])
        raise
