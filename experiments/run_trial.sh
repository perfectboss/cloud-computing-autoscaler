#!/usr/bin/env bash
# Run one full experiment trial.
#
# Args:
#   $1 = trial name (e.g. "custom", "hpa70", "hpa90")
#   $2 = autoscaler mode: "custom", "hpa70", "hpa90"
#   $3 = limit-seconds (0 = full workload)
#   $4 = results dir (absolute)

set -u  # don't set -e — we want partial artifacts on failure

NAME="$1"
MODE="$2"
LIMIT="${3:-0}"
OUT="$4"
PROJECT_ROOT="E:/cloud-computing-project"
K8S="$PROJECT_ROOT/k8s"
TRIAL_DIR="$OUT/$NAME"
mkdir -p "$TRIAL_DIR"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$TRIAL_DIR/trial.log"; }

log "=== Trial: $NAME (mode=$MODE limit=${LIMIT}s) ==="

# 1) Reset baseline: scale to 1, ensure clean autoscaler state.
log "Step 1: Reset to 1 replica + clear prior load-tester job + clear prior HPA"
kubectl scale deployment/inference-deployment --replicas=1 >/dev/null 2>&1
kubectl delete job load-tester --ignore-not-found >/dev/null 2>&1
kubectl delete hpa inference-hpa --ignore-not-found >/dev/null 2>&1

# 2) Configure autoscaler for this trial.
log "Step 2: Configure autoscaler ($MODE)"
case "$MODE" in
  custom)
    # Ensure custom autoscaler is running.
    kubectl scale deployment/autoscaler --replicas=1 >/dev/null 2>&1
    kubectl rollout status deployment/autoscaler --timeout=30s >/dev/null 2>&1
    ;;
  hpa70)
    # Disable custom; apply HPA70.
    kubectl scale deployment/autoscaler --replicas=0 >/dev/null 2>&1
    sleep 3
    kubectl apply -f "$K8S/hpa-inference-70.yaml" >/dev/null
    ;;
  hpa90)
    kubectl scale deployment/autoscaler --replicas=0 >/dev/null 2>&1
    sleep 3
    kubectl apply -f "$K8S/hpa-inference-90.yaml" >/dev/null
    ;;
  *)
    log "ERROR: unknown mode $MODE"
    exit 1
    ;;
esac

# 3) Wait for the deployment to settle at 1 ready replica.
log "Step 3: Wait for 1 ready replica"
for i in $(seq 1 30); do
  READY=$(kubectl get deploy inference-deployment -o jsonpath='{.status.readyReplicas}')
  CURRENT_REPLICAS=$(kubectl get deploy inference-deployment -o jsonpath='{.spec.replicas}')
  if [ "$READY" = "1" ] && [ "$CURRENT_REPLICAS" = "1" ]; then
    log "  Ready in ${i}s"
    break
  fi
  sleep 1
done

# 4) Decide how long the recorder runs.
WORKLOAD_LEN=$(awk '{print NF-1}' "$PROJECT_ROOT/workload.txt")
if [ "$LIMIT" -gt 0 ]; then
  WORKLOAD_LEN="$LIMIT"
fi
RECORDER_DURATION=$((WORKLOAD_LEN + 30))  # extra to capture scale-down

# 5) Start replica recorder in background.
log "Step 4: Start replica recorder (duration=${RECORDER_DURATION}s)"
python "$PROJECT_ROOT/experiments/record_replicas.py" \
  --duration "$RECORDER_DURATION" \
  > "$TRIAL_DIR/replicas.csv" 2>&1 &
REC_PID=$!
log "  Recorder PID: $REC_PID"

# 6) Launch load-tester Job.
log "Step 5: Launch load-tester Job (limit-seconds=$LIMIT)"
# Apply job manifest with limit injected.
TMP_JOB=$(mktemp)
sed -e "s|--limit-seconds=30|--limit-seconds=$LIMIT|" \
    -e "s|args:|args:\n            - --experiment-name=$NAME|" \
    "$K8S/load-tester-job.yaml" > "$TMP_JOB"
kubectl apply -f "$TMP_JOB" >/dev/null
rm -f "$TMP_JOB"

# 7) Wait for job completion.
log "Step 6: Wait for load-tester completion"
WAIT_MAX=$((WORKLOAD_LEN + 120))
if ! kubectl wait --for=condition=complete --timeout="${WAIT_MAX}s" job/load-tester 2>>"$TRIAL_DIR/trial.log"; then
  log "WARN: load-tester did not complete in ${WAIT_MAX}s"
fi

# 8) Capture results.
log "Step 7: Capture logs + CSV"
kubectl logs job/load-tester > "$TRIAL_DIR/load_tester.log" 2>&1
# Extract CSV between sentinels.
awk -v name="$NAME" '
  /RESULTS_CSV_BEGIN/ { capture=1; next }
  /RESULTS_CSV_END/   { capture=0; next }
  capture==1 { print }
' "$TRIAL_DIR/load_tester.log" > "$TRIAL_DIR/results.csv"

# 9) Wait for recorder to finish.
log "Step 8: Wait for replica recorder to finish"
wait "$REC_PID" 2>/dev/null || true

# 10) Summary.
ROWS=$(($(wc -l < "$TRIAL_DIR/results.csv") - 1))
log "Done. Captured $ROWS request rows. Artifacts in $TRIAL_DIR"
log "=== Trial $NAME complete ==="
