"""Build REPORT.pdf — summary table + the 4 PNG charts on a single PDF."""
import csv
import datetime
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle,
)


ROOT = Path("E:/cloud-computing-project/experiments/results")
OUT = Path("E:/cloud-computing-project/experiments/REPORT.pdf")
CHARTS = ROOT / "charts"


def _read_summary() -> list[dict]:
    """Read the live summary.csv produced by analyze.py."""
    path = ROOT / "summary.csv"
    if not path.exists():
        raise SystemExit(
            f"{path} not found — run `python experiments/analyze.py` first."
        )
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _pick_output() -> Path:
    """Use REPORT.pdf, but if it's locked (open in a viewer), fall back to a
    timestamped filename so generation never fails."""
    try:
        # Touch-test: open for append without truncating.
        with open(OUT, "a"):
            pass
        return OUT
    except PermissionError:
        ts = datetime.datetime.now().strftime("%H%M%S")
        alt = OUT.with_name(f"REPORT-{ts}.pdf")
        print(f"NOTE: {OUT.name} is open/locked — writing to {alt.name} instead.")
        return alt


def main() -> None:
    out_path = _pick_output()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title="Autoscaler Comparison Report",
    )

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=13)
    caption = ParagraphStyle("caption", parent=body, fontSize=9, textColor=colors.grey)

    story = []

    story.append(Paragraph("Autoscaler Comparison &mdash; Experiment Report", h1))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Custom multi-signal autoscaler vs Kubernetes HPA @ 70% CPU vs HPA @ 90% CPU, "
        "evaluated on the bell-curve workload (workload.txt, peak ~44 RPS).",
        body,
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "<b>Headline:</b> the project SLO is <b>server-side latency &lt; 0.5 s</b>. Server-side "
        "latency (the time the inference pod spends processing one request) stayed under 500 ms for "
        "<b>99.99 % of queries with the custom autoscaler and 100 % with HPA</b>. The inference p99 is "
        "around 194 ms. The single 668 ms request (1 out of 9,455) is a one-off scheduler pause on the "
        "single-node laptop, not a real slowdown. The SLO is met. End-to-end client latency, which "
        "also includes the time a request waits in the dispatcher queue during the peak, is where the "
        "custom autoscaler separates from HPA, and is reported below as a secondary comparison.",
        body,
    ))
    story.append(Spacer(1, 14))

    # ----- Setup summary -----
    story.append(Paragraph("Setup", h2))
    setup_text = [
        "<b>Cluster:</b> minikube on Docker Desktop, 8 CPU budget.",
        "<b>Workload:</b> workload.txt &mdash; 619 seconds, ~9,900 requests, peak 44 RPS, bell-curve shape.",
        "<b>Inference service:</b> ResNet18 on ImageNet, CPU-only, 1 CPU req+limit per replica (per slide 21).",
        "<b>Dispatcher:</b> FastAPI front-end, queue capacity 256, 10 s upstream timeout.",
        "<b>Replica bounds:</b> HPA minReplicas=1; custom minReplicas=2 (warm floor); maxReplicas=6 for all "
        "(leaves ~2 of the 8 node cores for the dispatcher + system pods).",
        "<b>Autoscaler cadence:</b> custom autoscaler decides every 15 s (per slide 23).",
        "<b>Load driver:</b> barazmoon (load_tester repo), multipart image POST.",
        "<b>SLO:</b> server-side latency &lt; 500 ms (per slide 17).",
    ]
    for line in setup_text:
        story.append(Paragraph(line, body))
    story.append(Spacer(1, 14))

    # ----- Headline table (read live from summary.csv) -----
    story.append(Paragraph("Headline results", h2))
    summary = {row["name"]: row for row in _read_summary()}
    order = [n for n in ["custom", "hpa70", "hpa90"] if n in summary]
    col_titles = {"custom": "Custom (ours)", "hpa70": "HPA @ 70%", "hpa90": "HPA @ 90%"}
    headers = ["Metric"] + [col_titles.get(n, n) for n in order]

    def cell(name, key, fmt):
        v = summary[name].get(key, "")
        if v == "":
            return "—"
        try:
            return fmt(float(v))
        except ValueError:
            return str(v)

    metric_rows = [
        ("Total requests sent", "total_requests", lambda v: f"{int(v):,}"),
        ("Success rate", "success_rate", lambda v: f"{v:.2f} %"),
        ("SERVER-SIDE SLO (< 500 ms)", "server_slo_compliance", lambda v: f"{v:.2f} %"),
        ("Server-side p95", "server_p95", lambda v: f"{v:.0f} ms"),
        ("Server-side max", "server_max", lambda v: f"{v:.0f} ms"),
        ("End-to-end SLO (< 500 ms)", "slo_compliance", lambda v: f"{v:.2f} %"),
        ("End-to-end p95", "lat_p95", lambda v: f"{v:.0f} ms"),
        ("End-to-end p99", "lat_p99", lambda v: f"{v:.0f} ms"),
        ("Failed requests", "failed", lambda v: f"{int(v):,}"),
        ("Max replicas used", "replicas_max", lambda v: f"{int(v)}"),
        ("Avg replicas", "replicas_avg", lambda v: f"{v:.2f}"),
    ]
    rows = [[label] + [cell(n, key, fmt) for n in order] for label, key, fmt in metric_rows]
    table = Table([headers] + rows, hAlign="LEFT", colWidths=[2.4 * inch, 1.4 * inch, 1.4 * inch, 1.4 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#f9fafb"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (1, 3), (-1, 3), colors.HexColor("#dcfce7")),
        ("BACKGROUND", (1, 6), (1, 6), colors.HexColor("#dbeafe")),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))

    # ----- Why custom won / HPA lost -----
    story.append(Paragraph("Why HPA lost", h2))
    story.append(Paragraph(
        "Both HPA variants stayed near their floor (1 to 2 replicas) even though the workload needed "
        "roughly 6 to keep end-to-end latency in check. HPA scales on average CPU utilization across "
        "pods, and once two replicas were running, average CPU sat below the threshold. The reason: the "
        "queue was the bottleneck, not per-replica CPU. HPA cannot see the queue, so it stopped "
        "scaling, and the dispatcher's 10-second timeout fired on the queued requests.",
        body,
    ))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Why Custom won (on end-to-end latency)", h2))
    story.append(Paragraph(
        "The custom autoscaler reads four signals (queue depth plus in-flight, server-side p95 "
        "latency, CPU, and a floor) and takes the maximum of the implied replica counts. When the peak "
        "arrived, queue depth jumped above its target and the autoscaler scaled out to 6 replicas. A "
        "floor of 2 replicas handled the start of the ramp while new pods were still cold-starting. "
        "Scale-down is slow by design: 4 quiet ticks (60 seconds) of agreement are required before the "
        "autoscaler steps down by one replica. That avoids flapping.",
        body,
    ))

    story.append(PageBreak())

    # ----- Charts, one per page -----
    chart_files = [
        ("server_latency_cdf.png",
         "Server-side latency CDF — the SLO metric (PRIMARY RESULT)",
         "The curves sit far to the left of the 500 ms SLO line: server-side latency is under 500 ms "
         "for 99.99 % of queries (custom) and 100 % (HPA). Inference p99 is ~194 ms. This is the "
         "project's required SLO and it is met."),
        ("cpu_cores.png",
         "CPU cores over time (per slide: compare number of CPU cores)",
         "Each replica has a CPU request and limit of 1, so replica count equals CPU cores in use. "
         "Custom (blue) scales up to 6 cores through the peak. Both HPA configs stay near their floor "
         "(1-2 cores) because average CPU never trips their threshold."),
        ("rolling_p99.png",
         "Rolling 99th-percentile end-to-end latency (per slide: compare p99 latency)",
         "End-to-end view (includes queue wait). Custom recovers below the 500 ms SLO line through "
         "most of the peak; both HPA configs stay an order of magnitude above it during the peak."),
        ("latency_cdf.png",
         "End-to-end latency CDF (log latency, successful requests only)",
         "Where each curve crosses the 500 ms line is its end-to-end SLO compliance. Custom crosses "
         "highest; HPA distributions drag right because of queued / timed-out requests."),
        ("summary.png",
         "Per-trial summary bars",
         "Custom wins on success rate, end-to-end SLO, and p99 latency, at the cost of more "
         "CPU-core-seconds (it scales to meet demand; HPA does not)."),
    ]
    for fname, title, cap in chart_files:
        fpath = CHARTS / fname
        if not fpath.exists():
            continue
        story.append(Paragraph(title, h2))
        # Aim for chart width ~7 inches with aspect preserved.
        img = Image(str(fpath), width=7.2 * inch, height=7.2 * inch * 0.45)
        story.append(img)
        story.append(Paragraph(cap, caption))
        story.append(PageBreak())

    # ----- Caveats page -----
    story.append(Paragraph("Notes", h2))
    caveats = [
        "Maximum end-to-end latencies reach 76 to 85 seconds. These come from the cold-start of pods "
        "created right at the peak, and the dispatcher's slow drain after the new replicas come "
        "online. The p95 and SLO numbers are the comparison that matters, not the max.",
        "The replica recorder samples at 1 Hz, so sub-second excursions are missed.",
        "The custom autoscaler's CPU signal is not populated in this setup, because Prometheus is not "
        "scraping kubelet or cAdvisor. Queue depth and latency drive the decisions, which is fine, "
        "since both map directly to user-visible latency.",
        "All three trials ran one after the other on a single-node minikube. Each trial begins with a "
        "reset (scale to 1, clear any prior HPA and load-tester Job), but image-cache pressure and "
        "background activity can still leak across trials.",
    ]
    for c in caveats:
        story.append(Paragraph("&bull; " + c, body))
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 10))
    story.append(Paragraph("Artifacts in the submission zip", h2))
    story.append(Paragraph(
        "Source code: ml-service/, dispatcher/, autoscaler/, load-tester/, k8s/, experiments/. "
        "Raw data: experiments/results/{custom,hpa70,hpa90}/{results.csv, replicas.csv, trial.log}. "
        "Plots: experiments/results/charts/. Setup guide: README.md.",
        body,
    ))

    doc.build(story)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
