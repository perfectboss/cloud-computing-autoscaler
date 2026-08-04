import urllib.request
import json

with open("/tmp/cat.jpg", "rb") as f:
    data = f.read()

boundary = "----SmokeBoundary"
body = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="file"; filename="cat.jpg"\r\n'
    f"Content-Type: image/jpeg\r\n\r\n"
).encode() + data + f"\r\n--{boundary}--\r\n".encode()

req = urllib.request.Request(
    "http://127.0.0.1:8000/predict",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
)
resp = urllib.request.urlopen(req, timeout=60)
result = json.loads(resp.read().decode())
print("Top-5 labels:", result["labels"])
print(f"Server latency: {result['server_latency_s']*1000:.1f} ms")
print(f"Pod hostname: {result['pod']}")
print(f"Under 500ms SLO: {result['server_latency_s'] < 0.5}")
