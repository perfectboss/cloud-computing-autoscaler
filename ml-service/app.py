import io
import time
import os
import torch
from fastapi import FastAPI, UploadFile, File, Response
from PIL import Image
from torchvision.models import resnet18, ResNet18_Weights
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CONTENT_TYPE_LATEST,
    generate_latest,
)

torch.set_num_threads(1)

app = FastAPI()

weights = ResNet18_Weights.IMAGENET1K_V1
model = resnet18(weights=weights)
model.eval()
preprocessor = weights.transforms()
categories = weights.meta["categories"]

POD_NAME = os.environ.get("HOSTNAME", "unknown")

m_predict_total = Counter("inference_predict_total", "Total predictions completed")
m_predict_inflight = Gauge("inference_predict_inflight", "Predictions currently running")
m_predict_latency = Histogram(
    "inference_predict_latency_seconds",
    "Server-side prediction latency",
    buckets=(0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0),
)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    m_predict_inflight.inc()
    start = time.perf_counter()
    try:
        raw = await file.read()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        inp = preprocessor(image).unsqueeze(0)
        with torch.inference_mode():
            preds = model(inp).squeeze(0)
        top5_idx = preds.sort()[1][-5:].flip(0).tolist()
        labels = [categories[i] for i in top5_idx]
        server_latency = time.perf_counter() - start
        m_predict_latency.observe(server_latency)
        m_predict_total.inc()
        return {
            "labels": labels,
            "server_latency_s": server_latency,
            "pod": POD_NAME,
        }
    finally:
        m_predict_inflight.dec()
