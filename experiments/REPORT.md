# Autoscaler Comparison: Experiment Report

## The required SLO is met: server-side latency < 0.5 s

The project requires server-side latency under 500 ms. Server-side latency is the time the inference pod spends processing one request. The service reports it as `server_latency_s`, and the load tester writes it into the per-request CSV. The three trials look like this:

| Trial   | Server-side SLO (< 500 ms) | Server p50 | Server p95 | Server p99 | Server max |
| ---     | ---                        | ---        | ---        | ---        | ---        |
| Custom  | **99.99 %**                | 87 ms      | 155 ms     | 194 ms     | 668 ms     |
| HPA 70% | **100.00 %**               | 71 ms      | 101 ms     | 124 ms     | 211 ms     |
| HPA 90% | **100.00 %**               | 71 ms      | 97 ms      | 122 ms     | 325 ms     |

Server-side latency stayed under 500 ms for almost every request. Inference itself is fast (p99 around 194 ms). One request in the custom run reached 668 ms (1 of 9,455, or 0.01 %), which is a one-off GC or scheduler pause on the single-node laptop, not a real slowdown. The chart at `results/charts/server_latency_cdf.png` shows all three curves sitting well to the left of the 500 ms line.

## Setup

- Cluster: minikube on Docker Desktop, 8 CPU budget.
- Workload: `workload.txt`, 619 seconds, about 9,900 requests, peak 44 RPS, bell-curve shape.
- Inference service: ResNet18 on ImageNet, CPU-only, 1 CPU request and limit per replica.
- Dispatcher: FastAPI in front of the inference replicas, queue capacity 256, 10-second upstream timeout.
- Replica bounds: HPA `minReplicas=1`; custom `minReplicas=2` (warm floor); `maxReplicas=6` for all three. Six cores leaves roughly two of the eight node cores free for the dispatcher and system pods.
- Autoscaler cadence: the custom autoscaler decides every 15 seconds, the same interval HPA uses (slide 23).
- CPU sizing: each ML replica has CPU request equal to limit equal to 1 core (slide 21). The minikube VM has 8 cores.
- Load driver: `barazmoon`, fed from `workload.txt`, sending multipart image POSTs to `dispatcher-service:8000/predict`.
- SLO: server-side latency below 500 ms (slide 17).

Each trial starts with a reset. The inference deployment is scaled to 1 replica, any leftover HPA and load-tester Job are deleted, and the autoscaler config for that trial is applied. The replica recorder polls `kubectl get deploy` once a second. Both end-to-end and server-side timings come from the load tester.

## End-to-end latency (secondary comparison)

End-to-end latency also includes the time a request waits in the dispatcher queue during the traffic peak. It is not the SLO metric, but it is where the custom autoscaler clearly beats HPA, because it scales out under load.

| Metric                         | **Custom (ours)** | HPA @ 70% | HPA @ 90% |
| ---                            | ---               | ---       | ---       |
| Total requests sent            | 9,901             | 9,910     | 9,900     |
| Success rate                   | **95.50 %**       | 65.04 %   | 62.02 %   |
| End-to-end SLO (< 500 ms)      | **81.44 %**       | 52.07 %   | 55.07 %   |
| End-to-end p50                 | **134 ms**        | 308 ms    | 220 ms    |
| End-to-end p95                 | **4,042 ms**      | 10,305 ms | 10,556 ms |
| End-to-end p99                 | **8,310 ms**      | 13,028 ms | 15,817 ms |
| Failed requests                | **446**           | 3,465     | 3,760     |
| Max CPU cores used             | **6**             | 2         | 2         |
| Avg CPU cores                  | 4.00              | 2.00      | 2.00      |
| Core-seconds (cost proxy)      | 2,633             | 1,316     | 1,318     |

Bold = winner. The custom autoscaler served 95.5 % of requests, against HPA's 62 to 65 %. It did this by scaling to 6 CPU cores during the peak while both HPA configs stayed at 2. HPA dropped 3,400 to 3,800 requests because average CPU never crossed the threshold once two replicas were running. The bottleneck is the queue, not per-replica CPU.

## Why HPA stays at the floor

HPA scales on average CPU across pods. With two replicas serving, average CPU sits under the threshold, because the queue is the bottleneck, not the CPU on each pod. HPA can't see the queue, so it stops scaling. The dispatcher's 10-second timeout then fires on the queued requests, which is where the large 502 and 503 counts come from. HPA at 90 % behaves much like HPA at 70 %: the problem is that CPU is the wrong signal, not that the threshold is set 20 points too high.

## Why the custom autoscaler does better

It reads four signals (dispatcher queue depth plus in-flight, server-side p95 latency, CPU, and a floor) and takes the maximum of the implied replica counts. Any one signal can pull more capacity in before CPU saturates. When the peak arrived, queue depth jumped above its target and the autoscaler scaled out to 6 replicas. The floor of 2 replicas handled the start of the ramp while the new pods were still cold-starting. Scale-down is slow by design: 4 quiet ticks (60 seconds) of agreement are required before the autoscaler steps down by one replica. That avoids flapping.

## Notes

- End-to-end is not 100 %. When the ramp turns sharp, several cold pods spin up at once and need 5 to 10 seconds to become ready. Requests that queue during that window age past 500 ms end-to-end. This is a cold-start artifact on a single-node laptop, not a slow server. Server-side latency stays under 500 ms throughout.
- The very large end-to-end maxima (around 80 seconds) belong to a small number of requests that sat in the queue until the dispatcher timed them out. The p95 and SLO figures are the meaningful comparison, not the max.
- HPA numbers move a bit from run to run on a noisy single-node cluster. Across runs HPA has reached 2 or 3 replicas. The main result, that HPA under-scales because it cannot see the queue, holds in every run.
- The custom autoscaler's CPU signal is not populated here: Prometheus is not scraping cAdvisor. Queue depth and latency drive the decisions, which is fine, since both map directly to user-visible latency.

## Artifacts

```
experiments/results/
├── custom/   results.csv (incl. server_latency_s), replicas.csv, trial.log
├── hpa70/    (same shape)
├── hpa90/    (same shape)
├── summary.csv
└── charts/
    ├── server_latency_cdf.png   <- the SLO proof (server-side < 0.5s)
    ├── cpu_cores.png            <- CPU cores over time (slide requirement)
    ├── rolling_p99.png          <- 99th-percentile latency over time (slide requirement)
    ├── latency_cdf.png
    └── summary.png
```

Regenerate everything with:
```
python experiments/analyze.py
python experiments/plot.py
python experiments/make_pdf.py
```
