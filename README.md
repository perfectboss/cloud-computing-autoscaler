# Cloud Computing Project — Autoscaling Inference

A FastAPI ResNet18 image-classification service deployed on Kubernetes (minikube), fronted by a dispatcher, monitored with Prometheus, and load-tested under a bell-curve workload to compare a **custom autoscaler** against Kubernetes HPA at 70% and 90% CPU targets.

## What's in here

```
ml-service\         FastAPI + ResNet18 inference service (CPU-only, 1 thread)
dispatcher\         FastAPI front-end with queue, latency, and inflight metrics
autoscaler\         Multi-signal custom autoscaler (queue + latency + CPU)
load-tester\        barazmoon-based load generator driven by workload.txt
k8s\                All Kubernetes manifests
experiments\        Trial runner, replica recorder, analyzer, charts
test-images\        1,000 ImageNet sample images (for smoke tests)
workload.txt        Per-second RPS schedule for the experiment
```

## Final results

**The required SLO — server-side latency < 0.5 s — is met (99.99–100% across configs):**

| Metric                          | Custom autoscaler | HPA @ 70% CPU | HPA @ 90% CPU |
| ---                             | ---               | ---           | ---           |
| **Server-side SLO (< 500 ms)**  | **99.99 %**       | **100.00 %**  | **100.00 %**  |
| Server-side p99                 | 194 ms            | 124 ms        | 122 ms        |
| Server-side max                 | 668 ms¹           | 211 ms        | 325 ms        |

¹ one 668 ms outlier of 9,455 requests (a one-off scheduler hiccup); inference p99 is 194 ms.

Secondary comparison — **end-to-end** latency (includes dispatcher queue wait; where the custom autoscaler beats HPA):

| Metric                          | Custom autoscaler | HPA @ 70% CPU | HPA @ 90% CPU |
| ---                             | ---               | ---           | ---           |
| Success rate                    | **95.5 %**        | 65.0 %        | 62.0 %        |
| End-to-end SLO (< 500 ms)       | **81.4 %**        | 52.1 %        | 55.1 %        |
| End-to-end p99                  | **8,310 ms**      | 13,028 ms     | 15,817 ms     |
| Max CPU cores used              | 6                 | 2             | 2             |

Full write-up: [experiments/REPORT.md](experiments/REPORT.md). Charts under [experiments/results/charts/](experiments/results/charts/) — see `server_latency_cdf.png` for the SLO proof.

---

## Prerequisites

- **Docker Desktop** (must be running)
- **minikube** (cluster `minikube` already created)
- **kubectl** on PATH
- **Python 3.11+** with `matplotlib` (for `plot.py`)
- VS Code is optional — any terminal works

---

## Scenario A — Starting fresh (or after Docker / laptop restart)

### Step 1 — Bring up the cluster (~3 min)

Use **PowerShell** (Terminal A in VS Code is fine):

```powershell
# Confirm Docker daemon is up
docker info --format '{{.ServerVersion}}'

# Confirm minikube cluster is up
minikube status -p minikube

# If minikube isn't running:
minikube start -p minikube --cpus=8 --memory=8g

# Confirm kubectl is talking to the cluster
kubectl get nodes

# Enable metrics-server (only needed once per cluster lifetime)
minikube addons enable metrics-server
```

### Step 2 — Build all 4 images into minikube's docker (~15 min first time, ~2 min on rebuilds)

Images must live in **minikube's** Docker daemon, not your host's. `minikube docker-env` redirects `DOCKER_HOST` to point there.

```powershell
# Point this terminal at minikube's docker daemon (only affects this terminal)
& minikube -p minikube docker-env --shell powershell | Invoke-Expression

# Build everything
docker build -t ml-inference:v2 E:\cloud-computing-project\ml-service
docker build -t dispatcher:v3   E:\cloud-computing-project\dispatcher
docker build -t autoscaler:v1   E:\cloud-computing-project\autoscaler
docker build -t load-tester:v1  E:\cloud-computing-project\load-tester

# Sanity check
docker images | findstr "ml-inference dispatcher autoscaler load-tester"
```

> **Note:** the `ml-inference` build is slow first time (downloads PyTorch CPU wheels and bakes ResNet18 weights into the image). Subsequent builds are seconds because Docker caches layers.

### Step 3 — Deploy everything (~2 min)

```powershell
# Workload ConfigMap from workload.txt
kubectl create configmap workload --from-file=workload.txt=E:\cloud-computing-project\workload.txt --dry-run=client -o yaml | kubectl apply -f -

# Apply all manifests
kubectl apply -f E:\cloud-computing-project\k8s\inference-deployment.yaml
kubectl apply -f E:\cloud-computing-project\k8s\inference-service.yaml
kubectl apply -f E:\cloud-computing-project\k8s\dispatcher-deployment.yaml
kubectl apply -f E:\cloud-computing-project\k8s\dispatcher-service.yaml
kubectl apply -f E:\cloud-computing-project\k8s\prometheus-rbac.yaml
kubectl apply -f E:\cloud-computing-project\k8s\prometheus-configmap.yaml
kubectl apply -f E:\cloud-computing-project\k8s\prometheus-deployment.yaml
kubectl apply -f E:\cloud-computing-project\k8s\autoscaler-rbac.yaml
kubectl apply -f E:\cloud-computing-project\k8s\autoscaler-deployment.yaml

# Wait for everything ready
kubectl rollout status deployment/inference-deployment
kubectl rollout status deployment/dispatcher-deployment
kubectl rollout status deployment/prometheus
kubectl rollout status deployment/autoscaler
```

### Step 4 — Quick smoke test

```powershell
# Terminal B — port-forward dispatcher
kubectl port-forward service/dispatcher-service 18000:8000

# Terminal A — send one image
curl.exe -F "file=@E:\cloud-computing-project\test-images\n02123045_tabby.JPEG" http://127.0.0.1:18000/predict
# Should return: {"labels": ["Egyptian cat", "tabby", ...], "server_latency_s": ~0.08, "pod": "..."}
```

Ctrl+C the port-forward when done.

---

## Scenario B — Just re-run the experiment (assumes Scenario A done)

The trial runner is a **bash** script (uses `awk`/`sed`/etc.), so open **Git Bash** (or WSL) for this part:

```bash
cd "E:/cloud-computing-project"

# Trial 1: Custom autoscaler vs full workload (~12 min)
bash experiments/run_trial.sh custom custom 0 experiments/results

# Trial 2: HPA at 70% CPU (~12 min)
bash experiments/run_trial.sh hpa70 hpa70 0 experiments/results

# Trial 3: HPA at 90% CPU (~12 min)
bash experiments/run_trial.sh hpa90 hpa90 0 experiments/results
```

Each trial:
1. Scales `inference-deployment` back to 1 replica
2. Configures the chosen autoscaler (scales custom up/down, applies/removes HPA YAML)
3. Starts a 1-Hz replica recorder in the background
4. Launches the load-tester Job
5. Waits for completion; captures CSV + replica timeline
6. Writes everything to `experiments/results/<name>/`

### Then analyze and plot

```bash
python experiments/analyze.py
python experiments/plot.py
```

Output: comparison table to stdout, `experiments/results/summary.csv`, charts in `experiments/results/charts/`.

---

## Useful operations

| Goal | Command |
| --- | --- |
| Watch the autoscaler decisions | `kubectl logs -f deploy/autoscaler` |
| Open Prometheus UI | `kubectl port-forward service/prometheus 9090:9090` then http://localhost:9090 |
| Watch replicas during a trial | `watch -n 5 'kubectl get deploy inference-deployment'` |
| Force a rebuild + redeploy of dispatcher | `docker build -t dispatcher:v3 dispatcher\ ; kubectl rollout restart deploy/dispatcher-deployment` |
| Update `workload.txt` ConfigMap | `kubectl create configmap workload --from-file=workload.txt=workload.txt --dry-run=client -o yaml \| kubectl apply -f -` |
| Wipe and reapply everything | `kubectl delete deploy --all ; kubectl apply -f k8s\` |

---

## Shutting down cleanly

In **PowerShell**:

```powershell
# 1) Stop minikube — leaves the cluster's state on disk; resumes fast next time
minikube stop -p minikube

# 2) Quit Docker Desktop from the tray icon (or `wsl --shutdown` if using WSL backend)

# 3) Safe to shut down the laptop
```

Next time, `minikube start -p minikube` brings everything back exactly where it was.
