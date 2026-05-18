#!/usr/bin/env bash
set -euo pipefail

# ── publish-benchmark.sh ──────────────────────────────────────────────
# Publish benchmark-matrix results to gh-pages.
#
# Usage: ./scripts/publish-benchmark.sh [results-dir]
#
# Default results-dir: results/benchmark-matrix/
# Copies all benchmark JSON to gh-pages under benchmarks/data/ and
# generates an HTML dashboard at benchmarks/index.html.
# ─────────────────────────────────────────────────────────────────────

RESULTS_DIR="${1:-results/benchmark-matrix}"
RESULTS_DIR="$(cd "$(dirname "$RESULTS_DIR")" 2>/dev/null && pwd)/$(basename "$RESULTS_DIR")" 2>/dev/null || {
  echo "ERROR: results dir not found: $1" >&2; exit 1
}

if [ ! -d "$RESULTS_DIR" ]; then
  echo "ERROR: results dir not found: $RESULTS_DIR" >&2
  exit 1
fi

json_files=("$RESULTS_DIR"/*.json)
if [ ! -f "${json_files[0]}" ]; then
  echo "ERROR: no JSON files in $RESULTS_DIR" >&2
  exit 1
fi

echo "==> Found ${#json_files[@]} benchmark result(s) in $RESULTS_DIR"

# ── Clone gh-pages ────────────────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_URL="$(git -C "$REPO_DIR" remote get-url origin)"

WORK=$(mktemp -d /tmp/fips-lab-bench-XXXXXX)
trap 'rm -rf "$WORK"' EXIT

if git clone --single-branch -b gh-pages "$REMOTE_URL" "$WORK/gh-pages" 2>/dev/null; then
  echo "==> Cloned existing gh-pages branch"
else
  echo "==> gh-pages branch not found, creating fresh"
  mkdir -p "$WORK/gh-pages"
  cd "$WORK/gh-pages"
  git init -b gh-pages
  git remote add origin "$REMOTE_URL"
fi

cd "$WORK/gh-pages"

# ── Copy benchmark data ───────────────────────────────────────────────

mkdir -p benchmarks/data
cp "$RESULTS_DIR"/*.json benchmarks/data/

# Redact full npubs to short prefixes in published JSON
for f in benchmarks/data/*.json; do
  if sed --version 2>/dev/null | grep -q GNU; then
    sed -i -E 's/npub1[a-z0-9]{50,}/npub_REDACTED/g' "$f"
  else
    sed -i '' -E 's/npub1[a-z0-9]{50,}/npub_REDACTED/g' "$f"
  fi
done

echo "==> Copied and redacted ${#json_files[@]} benchmark files"

# ── Generate dashboard HTML ───────────────────────────────────────────

python3 - "$WORK/gh-pages" <<'PYDASH'
import json, sys, os, html as html_mod
from datetime import datetime
from pathlib import Path

gh_pages = sys.argv[1]
data_dir = os.path.join(gh_pages, "benchmarks", "data")
out_path = os.path.join(gh_pages, "benchmarks", "index.html")

def esc(s):
    return html_mod.escape(str(s))

def fmt_ms(us):
    if us is None:
        return "N/A"
    return f"{us/1000:.1f}"

def fmt_bps(bps):
    if bps is None:
        return "N/A"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.2f} Mbps"
    if bps >= 1_000:
        return f"{bps/1_000:.1f} kbps"
    return f"{bps} bps"

def fmt_pct(rate):
    if rate is None:
        return "N/A"
    return f"{rate*100:.1f}%"

def verdict_for_result(r):
    """Derive a pass/fail indicator from benchmark result."""
    status = r.get("status", "")
    if status == "error":
        return ("FAIL", "v-fail")
    if status == "pending":
        return ("SKIP", "v-na")
    if r.get("test") == "echo":
        loss = r.get("loss_count", 0)
        median = r.get("median_us")
        if loss is not None and loss > 2:
            return ("DEGRADED", "v-degraded")
        if median is not None and median > 500000:
            return ("DEGRADED", "v-degraded")
        return ("PASS", "v-pass")
    if r.get("test") == "throughput":
        bps = r.get("achieved_bps")
        loss = r.get("frame_loss_rate", 1.0)
        if bps is not None and bps > 0 and loss < 0.5:
            return ("PASS", "v-pass")
        if bps is not None and bps > 0:
            return ("DEGRADED", "v-degraded")
        return ("FAIL", "v-fail")
    return ("N/A", "v-na")

# ── Load all benchmark runs ───────────────────────────────────────────

runs = []
for f in sorted(Path(data_dir).glob("*.json")):
    with open(f) as fh:
        data = json.load(fh)
    data["_filename"] = f.name
    runs.append(data)

if not runs:
    print("WARNING: no benchmark data found", file=sys.stderr)
    sys.exit(0)

# ── Compute summary stats ─────────────────────────────────────────────

total_runs = len(runs)
total_tests = sum(len(r.get("results", [])) for r in runs)
latest_ts = runs[-1].get("timestamp", "N/A")

pass_count = 0
fail_count = 0
skip_count = 0
for run in runs:
    for r in run.get("results", []):
        v, _ = verdict_for_result(r)
        if v == "PASS":
            pass_count += 1
        elif v == "FAIL":
            fail_count += 1
        elif v == "SKIP":
            skip_count += 1
        else:
            pass_count += 1  # count DEGRADED as partial pass

# ── Find latest echo/throughput for summary cards ─────────────────────

latest_echo = None
latest_throughput = None
for r in reversed(runs[-1].get("results", [])):
    if r.get("test") == "echo" and r.get("status") == "complete" and latest_echo is None:
        latest_echo = r
    if r.get("test") == "throughput" and r.get("status") == "complete" and latest_throughput is None:
        latest_throughput = r

# ── Build run rows ────────────────────────────────────────────────────

run_rows_html = ""
for run in reversed(runs):
    ts = run.get("timestamp", "unknown")
    results = run.get("results", [])
    fname = run.get("_filename", "")

    run_pass = 0
    run_fail = 0
    run_skip = 0
    for r in results:
        v, _ = verdict_for_result(r)
        if v == "PASS" or v == "DEGRADED":
            run_pass += 1
        elif v == "FAIL":
            run_fail += 1
        else:
            run_skip += 1

    overall = "PASS" if run_fail == 0 and run_pass > 0 else ("FAIL" if run_fail > 0 else "SKIP")
    overall_css = "v-pass" if overall == "PASS" else ("v-fail" if overall == "FAIL" else "v-na")

    # Build mini results table
    mini_rows = ""
    for r in results:
        v, vcss = verdict_for_result(r)
        test = r.get("test", "?")
        pair = r.get("pair", "?")
        if test == "echo":
            detail = f"ps={r.get('payload_size', '?')} loss={r.get('loss_count', '?')} median={fmt_ms(r.get('median_us'))}ms"
        else:
            detail = f"fs={r.get('frame_size', '?')} bps={fmt_bps(r.get('achieved_bps'))} loss={fmt_pct(r.get('frame_loss_rate'))}"
        mini_rows += f"""<tr>
          <td>{esc(test)}</td>
          <td>{esc(pair)}</td>
          <td>{esc(detail)}</td>
          <td><span class="verdict {vcss}">{v}</span></td>
        </tr>"""

    run_rows_html += f"""
  <div class="run-card">
    <div class="run-header" onclick="this.parentElement.querySelector('.run-detail').classList.toggle('collapsed')">
      <span class="run-ts">{esc(ts)}</span>
      <span class="verdict {overall_css}">{overall}</span>
      <span class="run-counts">{run_pass} pass / {run_fail} fail / {run_skip} skip</span>
      <span class="run-expand">click to expand</span>
      <a class="run-json" href="data/{esc(fname)}" target="_blank">JSON</a>
    </div>
    <div class="run-detail collapsed">
      <table class="mini-table">
        <tr><th>Test</th><th>Pair</th><th>Details</th><th>Status</th></tr>
        {mini_rows}
      </table>
    </div>
  </div>"""

# ── Build trend data for echo median RTT chart ────────────────────────

echo_trend_labels = []
echo_trend_data = {}
for run in runs:
    ts_short = run.get("timestamp", "")[:16]
    echo_trend_labels.append(ts_short)
    for r in run.get("results", []):
        if r.get("test") == "echo" and r.get("status") == "complete":
            key = f"{r.get('pair', '?')} ps={r.get('payload_size', '?')}"
            if key not in echo_trend_data:
                echo_trend_data[key] = []
            echo_trend_data[key].append(r.get("median_us"))

echo_datasets_js = ""
colors = ["#4285f4", "#ea4335", "#34a853", "#fbbc04", "#673ab7", "#e91e63", "#00bcd4", "#ff9800"]
ci = 0
for key, vals in sorted(echo_trend_data.items()):
    color = colors[ci % len(colors)]
    ci += 1
    data_str = ", ".join(str(v) if v is not None else "null" for v in vals)
    echo_datasets_js += f"""{{
      label: "{esc(key)}",
      data: [{data_str}],
      borderColor: "{color}",
      backgroundColor: "{color}22",
      tension: 0.2,
      pointRadius: 3,
      borderWidth: 2,
      fill: false,
    }},"""

echo_labels_js = ", ".join(f'"{esc(l)}"' for l in echo_trend_labels)

# ── Build trend data for throughput chart ──────────────────────────────

tp_trend_labels = []
tp_trend_data = {}
for run in runs:
    ts_short = run.get("timestamp", "")[:16]
    tp_trend_labels.append(ts_short)
    for r in run.get("results", []):
        if r.get("test") == "throughput" and r.get("status") == "complete":
            key = f"{r.get('pair', '?')} fs={r.get('frame_size', '?')}"
            if key not in tp_trend_data:
                tp_trend_data[key] = []
            tp_trend_data[key].append(r.get("achieved_bps"))

tp_datasets_js = ""
ci = 0
for key, vals in sorted(tp_trend_data.items()):
    color = colors[ci % len(colors)]
    ci += 1
    data_str = ", ".join(str(v) if v is not None else "null" for v in vals)
    tp_datasets_js += f"""{{
      label: "{esc(key)}",
      data: [{data_str}],
      borderColor: "{color}",
      backgroundColor: "{color}22",
      tension: 0.2,
      pointRadius: 3,
      borderWidth: 2,
      fill: false,
    }},"""

tp_labels_js = ", ".join(f'"{esc(l)}"' for l in tp_trend_labels)

# ── Generate HTML ─────────────────────────────────────────────────────

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIPS Lab - BLE Benchmark Results</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='15' fill='%231f4068'/%3E%3Ctext x='16' y='22' text-anchor='middle' font-family='sans-serif' font-weight='bold' font-size='18' fill='white'%3EF%3C/text%3E%3C/svg%3E">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:#f0f2f5;color:#1a1a2e;line-height:1.5}}
.header{{background:linear-gradient(135deg,#0a1628,#162447,#1f4068);color:#fff;padding:1.5rem 2rem;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
.header h1{{font-size:1.5rem;font-weight:600;letter-spacing:.5px}}
.header .subtitle{{font-size:.85rem;opacity:.8}}
.header a{{color:#fff;text-decoration:none;opacity:.7;margin-left:1.5rem;font-size:.85rem}}
.header a:hover{{opacity:1;text-decoration:underline}}
.container{{max-width:1100px;margin:2rem auto;padding:0 1rem}}
.summary{{display:flex;gap:1rem;margin-bottom:2rem;flex-wrap:wrap}}
.summary-card{{background:#fff;border-radius:8px;padding:1rem 1.5rem;flex:1;min-width:140px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.summary-card .label{{font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:#666;margin-bottom:.25rem}}
.summary-card .value{{font-size:1.5rem;font-weight:700;color:#1a1a2e}}
.section{{background:#fff;border-radius:8px;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}}
.section-header{{padding:1rem 1.5rem;border-bottom:1px solid #eee;font-size:1rem;font-weight:600}}
.chart-container{{padding:1rem 1.5rem;position:relative;height:350px}}
.run-card{{background:#fff;border-radius:8px;margin-bottom:1rem;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}}
.run-header{{padding:.75rem 1.5rem;display:flex;align-items:center;gap:1rem;cursor:pointer;border-bottom:1px solid #f0f0f0}}
.run-header:hover{{background:#fafbfc}}
.run-ts{{font-family:"SF Mono",SFMono-Regular,Consolas,monospace;font-size:.85rem;color:#555;min-width:170px}}
.run-counts{{font-size:.8rem;color:#666}}
.run-expand{{font-size:.7rem;color:#aaa;margin-left:auto}}
.run-json{{font-size:.75rem;color:#1f4068;text-decoration:none;margin-left:.5rem}}
.run-json:hover{{text-decoration:underline}}
.run-detail{{padding:1rem 1.5rem;transition:max-height .2s ease}}
.run-detail.collapsed{{display:none}}
.mini-table{{width:100%;border-collapse:collapse;font-size:.8rem}}
.mini-table th{{text-align:left;padding:.4rem .5rem;border-bottom:2px solid #eee;color:#666;font-weight:600;font-size:.7rem;text-transform:uppercase}}
.mini-table td{{padding:.4rem .5rem;border-bottom:1px solid #f5f5f5}}
.verdict{{display:inline-block;font-size:.7rem;font-weight:600;padding:2px 8px;border-radius:10px;text-transform:uppercase;letter-spacing:.3px}}
.v-pass{{background:#e6f4ea;color:#137333}}
.v-fail{{background:#fce8e6;color:#d93025}}
.v-degraded{{background:#fef7e0;color:#b06000}}
.v-na{{background:#f1f3f4;color:#80868b}}
footer{{text-align:center;padding:2rem;color:#999;font-size:.8rem}}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>BLE Benchmark Results</h1>
    <div class="subtitle">FIPS Lab testbed &mdash; echo RTT &amp; throughput measurements</div>
  </div>
  <a href="../index.html">&larr; Test Reports</a>
</div>

<div class="container">
  <div class="summary">
    <div class="summary-card">
      <div class="label">Runs</div>
      <div class="value">{total_runs}</div>
    </div>
    <div class="summary-card">
      <div class="label">Total Tests</div>
      <div class="value">{total_tests}</div>
    </div>
    <div class="summary-card">
      <div class="label">Passed</div>
      <div class="value" style="color:#137333">{pass_count}</div>
    </div>
    <div class="summary-card">
      <div class="label">Failed</div>
      <div class="value" style="color:#d93025">{fail_count}</div>
    </div>
    <div class="summary-card">
      <div class="label">Latest Run</div>
      <div class="value" style="font-size:.95rem">{esc(latest_ts[:19])}</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">Echo RTT Trend (median, by run)</div>
    <div class="chart-container">
      <canvas id="echoChart"></canvas>
    </div>
  </div>

  <div class="section">
    <div class="section-header">Throughput Trend (achieved bps, by run)</div>
    <div class="chart-container">
      <canvas id="tpChart"></canvas>
    </div>
  </div>

  <div class="section">
    <div class="section-header">Run History</div>
  </div>
  {run_rows_html}
</div>

<footer>Generated by fips-lab publish-benchmark.sh &mdash; {esc(datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}</footer>

<script>
// Echo RTT trend chart
new Chart(document.getElementById('echoChart'), {{
  type: 'line',
  data: {{
    labels: [{echo_labels_js}],
    datasets: [{echo_datasets_js}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},
      tooltip: {{ callbacks: {{
        label: (ctx) => ctx.dataset.label + ': ' + (ctx.parsed.y ? (ctx.parsed.y / 1000).toFixed(1) + ' ms' : 'N/A')
      }}}}
    }},
    scales: {{
      y: {{ title: {{ display: true, text: 'Median RTT (us)' }}, beginAtZero: true }},
      x: {{ ticks: {{ maxRotation: 45, font: {{ size: 10 }} }} }}
    }}
  }}
}});

// Throughput trend chart
new Chart(document.getElementById('tpChart'), {{
  type: 'line',
  data: {{
    labels: [{tp_labels_js}],
    datasets: [{tp_datasets_js}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},
      tooltip: {{ callbacks: {{
        label: (ctx) => ctx.dataset.label + ': ' + (ctx.parsed.y ? (ctx.parsed.y / 1000).toFixed(1) + ' kbps' : 'N/A')
      }}}}
    }},
    scales: {{
      y: {{ title: {{ display: true, text: 'Achieved (bps)' }}, beginAtZero: true }},
      x: {{ ticks: {{ maxRotation: 45, font: {{ size: 10 }} }} }}
    }}
  }}
}});
</script>
</body>
</html>"""

os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w") as fh:
    fh.write(html)

print(f"==> Generated benchmarks/index.html ({len(runs)} runs)")
PYDASH

# ── Commit and push ───────────────────────────────────────────────────

cd "$WORK/gh-pages"
git add benchmarks/
if git diff --cached --quiet; then
  echo "==> No changes to commit"
else
  git commit -m "benchmarks: update benchmark-matrix dashboard ($(date -u '+%Y-%m-%d'))"
  git push origin gh-pages
  echo "==> Published to gh-pages"
fi
