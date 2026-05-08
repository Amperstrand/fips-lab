#!/usr/bin/env bash
set -euo pipefail

# ── publish-report.sh ────────────────────────────────────────────────
# Publish a FIPS Lab test run to gh-pages, preserving existing
# reports and generating a self-contained HTML dashboard.
#
# Usage: ./scripts/publish-report.sh <run-dir>
#
# <run-dir> must contain:
#   metadata.json    — timestamp, scenario, git commit, devices
#   analysis.json    — verdict, assertions (optional; shows N/A if missing)
#
# The entire run directory is copied to gh-pages under:
#   reports/<commit-short>/<timestamp>/
# ─────────────────────────────────────────────────────────────────────

RUN_DIR="${1:?Usage: $0 <run-dir>}"
RUN_DIR="$(cd "$(dirname "$RUN_DIR")" && pwd)/$(basename "$RUN_DIR")"

if [ ! -d "$RUN_DIR" ]; then
  echo "ERROR: run dir not found: $RUN_DIR" >&2
  exit 1
fi

if [ ! -f "$RUN_DIR/metadata.json" ]; then
  echo "ERROR: metadata.json not found in $RUN_DIR" >&2
  exit 1
fi

# ── JSON helpers (no jq) ────────────────────────────────────────────

json_string() {
  local file="$1" key="$2"
  grep -o "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$file" \
    | head -1 \
    | sed "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\(.*\)\"/\1/"
}

json_number() {
  local file="$1" key="$2"
  grep -o "\"${key}\"[[:space:]]*:[[:space:]]*[0-9][0-9.]*" "$file" \
    | head -1 \
    | sed "s/.*\"${key}\"[[:space:]]*:[[:space:]]*//"
}

json_nested_string() {
  local file="$1" parent="$2" key="$3"
  python3 -c "
import json, sys
with open('${file}') as f:
    d = json.load(f)
v = d.get('${parent}', {}).get('${key}')
if v is not None:
    print(v)
" 2>/dev/null
}

# ── Read metadata from metadata.json ─────────────────────────────────

COMMIT="$(json_string "$RUN_DIR/metadata.json" commit)"
if [ -z "$COMMIT" ]; then
  echo "ERROR: git commit not found in metadata.json" >&2
  exit 1
fi

SCENARIO="$(json_string "$RUN_DIR/metadata.json" scenario || true)"
SCENARIO="${SCENARIO:-unknown}"
TIMESTAMP="$(json_string "$RUN_DIR/metadata.json" timestamp || true)"
DURATION_SECS="$(json_number "$RUN_DIR/metadata.json" duration_secs || true)"
DURATION_SECS="${DURATION_SECS:-0}"

FIPS_COMMIT="$(json_nested_string "$RUN_DIR/metadata.json" fips_git commit || true)"
FIPS_COMMIT="${FIPS_COMMIT:-$COMMIT}"
FIPS_BRANCH="$(json_nested_string "$RUN_DIR/metadata.json" fips_git branch || true)"
FIPS_DIRTY="$(json_nested_string "$RUN_DIR/metadata.json" fips_git dirty || true)"

if [ -z "$TIMESTAMP" ]; then
  TIMESTAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
fi

DIR_TIMESTAMP="$(echo "$TIMESTAMP" | sed 's/[:+ ]/-/g' | sed 's/[^0-9T.Z-]//g')"

SHORT="${FIPS_COMMIT:0:12}"
KEEP="${FIPS_LAB_KEEP_REPORTS:-50}"

# ── Read analysis.json (optional) ────────────────────────────────────

VERDICT="N/A"
ASSERTIONS_TOTAL=0
ASSERTIONS_PASSED=0

if [ -f "$RUN_DIR/analysis.json" ]; then
  VERDICT="$(json_string "$RUN_DIR/analysis.json" verdict || true)"
  VERDICT="${VERDICT:-N/A}"

  # Count assertions: count lines with "passed": true and "passed": false
  if grep -q '"passed"' "$RUN_DIR/analysis.json" 2>/dev/null; then
    ASSERTIONS_PASSED="$(grep -c '"passed"[[:space:]]*:[[:space:]]*true' "$RUN_DIR/analysis.json" || true)"
    local_total="$(grep -c '"passed"[[:space:]]*:' "$RUN_DIR/analysis.json" || true)"
    ASSERTIONS_TOTAL="${local_total:-0}"
  fi
fi

echo "==> Publishing report for FIPS commit ${SHORT} branch=${FIPS_BRANCH:-?} scenario=${SCENARIO} verdict=${VERDICT}..."

# ── Clone or create gh-pages ─────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_URL="$(git -C "$REPO_DIR" remote get-url origin)"

WORK=$(mktemp -d /tmp/fips-lab-report-XXXXXX)
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

# ── Add the new run (with redaction) ─────────────────────────────────

TARGET_DIR="reports/${SHORT}/${DIR_TIMESTAMP}"
mkdir -p "$TARGET_DIR"

# Copy everything, then remove sensitive files that should stay local.
cp -r "$RUN_DIR"/* "$TARGET_DIR/"

# ── Remove sensitive files from the published copy ────────────────────
# Keylogs contain raw Noise protocol encryption keys.
# BTSnoop captures contain raw BLE traffic (decryptable with keylogs).
# devices.yaml contains host paths, usernames, SSH targets.

rm -f "$TARGET_DIR"/keylog-*.txt
rm -f "$TARGET_DIR"/keylog-results.json
rm -f "$TARGET_DIR"/*.btsnoop
rm -f "$TARGET_DIR"/*.pcap
rm -f "$TARGET_DIR"/devices.yaml
rm -f "$TARGET_DIR"/runner.log

echo "==> Copied run to ${TARGET_DIR} (keylogs, captures, devices.yaml redacted)"

# ── Redact local paths and hostnames from published JSON ──────────────

redact_file() {
  local file="$1"
  [ ! -f "$file" ] && return 0

  if sed --version 2>/dev/null | grep -q GNU; then
    sed -i \
      -e 's|/Users/[^"\\ ]*|/Users/REDACTED|g' \
      -e 's|/home/[^"\\ ]*|/home/REDACTED|g' \
      -e 's|/tmp/[^"\\ ]*|/tmp/REDACTED|g' \
      -e 's|/run/[^"\\ ]*|/run/REDACTED|g' \
      -e 's|/etc/[^"\\ ]*|/etc/REDACTED|g' \
      -e 's|/usr/local/etc/[^"\\ ]*|/usr/local/etc/REDACTED|g' \
      -e 's|"user": "ubuntu"|"user": "REDACTED"|g' \
      -e 's|"user": "[^"]*"|"user": "REDACTED"|g' \
      "$file" 2>/dev/null
  else
    sed -i '' \
      -e 's|/Users/[^"\\ ]*|/Users/REDACTED|g' \
      -e 's|/home/[^"\\ ]*|/home/REDACTED|g' \
      -e 's|/tmp/[^"\\ ]*|/tmp/REDACTED|g' \
      -e 's|/run/[^"\\ ]*|/run/REDACTED|g' \
      -e 's|/etc/[^"\\ ]*|/etc/REDACTED|g' \
      -e 's|/usr/local/etc/[^"\\ ]*|/usr/local/etc/REDACTED|g' \
      -e 's|"user": "ubuntu"|"user": "REDACTED"|g' \
      -e 's|"user": "[^"]*"|"user": "REDACTED"|g' \
      "$file" 2>/dev/null
  fi
}

for json_file in "$TARGET_DIR"/*.json "$TARGET_DIR"/*.md; do
  [ -f "$json_file" ] && redact_file "$json_file"
done
for yaml_file in "$TARGET_DIR"/*.yaml; do
  [ -f "$yaml_file" ] && redact_file "$yaml_file"
done

echo "==> Redacted local paths and usernames from published JSON/YAML"

# ── Generate report.html for the current run ─────────────────────────

generate_report_html() {
  local target_dir="$1"

  [ ! -f "$target_dir/analysis.json" ] && return 0

  python3 - <<'PYREPORT' "$target_dir"
import json, sys, os, html as html_mod
from datetime import datetime

target_dir = sys.argv[1]

# ── Load data ──────────────────────────────────────────────────────────

with open(os.path.join(target_dir, "analysis.json")) as f:
    analysis = json.load(f)

meta = {}
meta_path = os.path.join(target_dir, "metadata.json")
if os.path.exists(meta_path):
    with open(meta_path) as f:
        meta = json.load(f)

# ── Helpers ────────────────────────────────────────────────────────────

def esc(s):
    return html_mod.escape(str(s))

def fmt_ts(ts):
    if not ts:
        return "N/A"
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H-%M-%SZ"):
        try:
            dt = datetime.strptime(ts, fmt)
            months = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
            return f"{months[dt.month]} {dt.day}, {dt.year} {dt.hour:02d}:{dt.minute:02d} UTC"
        except ValueError:
            continue
    return ts

def fmt_dur(secs):
    secs = int(secs or 0)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"

def verdict_css(v):
    return {
        "PASS": "v-pass",
        "FAIL": "v-fail",
        "DEGRADED": "v-degraded",
        "INSUFFICIENT_DATA": "v-na",
    }.get(v, "v-na")

def verdict_icon(v):
    return {"PASS": "✅", "FAIL": "❌", "DEGRADED": "⚠️", "INSUFFICIENT_DATA": "❓"}.get(v, "❓")

def bool_icon(b):
    return "✅" if b else "❌"

def size_fmt(b):
    b = int(b or 0)
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.1f} MB"

# ── Extract metadata ──────────────────────────────────────────────────

verdict = analysis.get("verdict", "N/A")
scenario = analysis.get("scenario_name", meta.get("scenario", "unknown"))
timestamp = analysis.get("timestamp", meta.get("timestamp", ""))
duration = analysis.get("duration_secs", meta.get("duration_secs", 0))
commit = meta.get("commit", "")
fips_git = meta.get("fips_git", {})
fips_commit = fips_git.get("commit", commit)
fips_branch = fips_git.get("branch", "")
fips_dirty = fips_git.get("dirty", False)

microfips_git = meta.get("microfips_git", {})
microfips_commit = microfips_git.get("commit", "")
microfips_branch = microfips_git.get("branch", "")
microfips_mode = microfips_git.get("mode", "")

# ── Build chart HTML ──────────────────────────────────────────────────

chart_files = ["chart-rtt.svg", "chart-peers.svg", "chart-rekeys.svg", "chart-rssi.svg"]
chart_labels = {
    "chart-rtt.svg": "Round-Trip Time",
    "chart-peers.svg": "Peer Count",
    "chart-rekeys.svg": "Rekey Events",
    "chart-rssi.svg": "BLE RSSI",
}
charts_html = ""
for cf in chart_files:
    full = os.path.join(target_dir, cf)
    if os.path.exists(full):
        charts_html += f'''<div class="chart-card">
  <a href="{esc(cf)}" target="_blank" title="Open raw SVG">
    <img src="{esc(cf)}" alt="{esc(chart_labels.get(cf, cf))}" loading="lazy"/>
    <div class="chart-overlay"><span>Click to view full size</span></div>
  </a>
  <div class="chart-label">{esc(chart_labels.get(cf, cf))}</div>
</div>
'''

# ── Build table sections ─────────────────────────────────────────────

def table(headers, rows, empty_msg="No data"):
    if not rows:
        return f'<div class="empty-state"><span class="empty-icon">📭</span><span class="empty-msg">{esc(empty_msg)}</span></div>'
    h_html = "".join(f"<th>{esc(h)}</th>" for h in headers)
    r_html = ""
    for row in rows:
        r_html += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
    return f'<table><thead><tr>{h_html}</tr></thead><tbody>{r_html}</tbody></table>'

# Connections
conn_rows = []
for c in analysis.get("connections", []):
    icon = bool_icon(c.get("connected", False))
    conn_rows.append([
        esc(c.get("pair", "")),
        icon,
        str(c.get("packets_sent", 0)),
        str(c.get("packets_recv", 0)),
    ])
conn_table = table(["Pair", "Connected", "Packets Sent", "Packets Recv"], conn_rows, "No connections")

# Peer Metrics (MMP)
pm_rows = []
for pm in analysis.get("peer_metrics", []):
    loss = f'{pm.get("loss_min", 0)*100:.1f}% / {pm.get("loss_avg", 0)*100:.1f}% / {pm.get("loss_max", 0)*100:.1f}%'
    if pm.get("rtt_min") is not None:
        rtt = f'{pm["rtt_min"]:.0f}ms / {pm["rtt_avg"]:.0f}ms / {pm["rtt_max"]:.0f}ms'
    else:
        rtt = "N/A"
    pm_rows.append([esc(pm.get("pair", "")), esc(loss), esc(rtt), str(pm.get("samples", 0))])
pm_table = table(["Pair", "Loss (min/avg/max)", "RTT (min/avg/max)", "Samples"], pm_rows, "No MMP metrics")

# Key Exchange
ke_rows = []
for ke in analysis.get("key_exchange", []):
    ke_rows.append([esc(ke.get("pair", "")), str(ke.get("link_keys", 0)), str(ke.get("session_keys", 0)), esc(ke.get("coverage", ""))])
ke_table = table(["Pair", "Link Keys", "Session Keys", "Coverage"], ke_rows, "No key exchange data")

# Rekey Stats
rk_rows = []
for rs in analysis.get("rekey_stats", []):
    interval = f'{rs["rekey_interval_avg_secs"]:.1f}s' if rs.get("rekey_interval_avg_secs") else "N/A"
    rph = f'{rs["rekeys_per_hour"]:.0f}' if rs.get("rekeys_per_hour") else "N/A"
    rk_rows.append([esc(rs.get("pair", "")), str(rs.get("total_rekeys", 0)), esc(interval), esc(rph)])
rk_table = table(["Pair", "Total Rekeys", "Avg Interval", "Rekeys/Hour"], rk_rows, "No rekey data")

# Disconnects
dc_rows = []
for d in analysis.get("disconnects", []):
    dc_rows.append([esc(d.get("pair", "")), f'{d.get("t", 0)}s', bool_icon(d.get("reconnected", False))])
dc_table = table(["Pair", "Time", "Reconnected"], dc_rows, "No disconnects detected")

# Keylog Verification
kv_rows = []
for kv in analysis.get("keylog_verification", []):
    kv_rows.append([esc(kv.get("pair", "")), str(kv.get("local_keys_parsed", 0)), str(kv.get("parse_errors", 0)),
                     bool_icon(kv.get("decryption_ready", False)), esc(kv.get("note", ""))])
kv_table = table(["Pair", "Keys Parsed", "Parse Errors", "Decryption Ready", "Note"], kv_rows, "No keylog verification data")

# Assertions
as_rows = []
for a in analysis.get("assertions", []):
    tag = bool_icon(a.get("passed", False))
    as_rows.append([tag, esc(a.get("name", "")), esc(a.get("expected", "")), esc(a.get("actual", ""))])
as_table = table(["Result", "Check", "Expected", "Actual"], as_rows, "No assertions")

as_total = len(analysis.get("assertions", []))
as_passed = sum(1 for a in analysis.get("assertions", []) if a.get("passed", False))
as_pct = (as_passed / as_total * 100) if as_total > 0 else 100
if as_pct == 100:
    as_pf_class = "pf-green"
elif as_pct >= 50:
    as_pf_class = "pf-yellow"
else:
    as_pf_class = "pf-red"
as_summary_html = ""
if as_total > 0:
    as_summary_html = f'<div class="assertion-summary"><span class="summary-text">{as_passed}/{as_total} passed ({as_pct:.0f}%)</span><div class="progress-bar" style="flex:1;max-width:200px"><div class="progress-fill {as_pf_class}" style="width:{as_pct:.1f}%"></div></div></div>'

# RSSI Stats
rssi_rows = []
for rs in analysis.get("rssi_stats", []):
    rssi_rows.append([esc(rs.get("device", "")), esc(rs.get("ble_addr", "")), str(rs.get("samples", 0)),
                       str(rs.get("min", "")), str(rs.get("avg", "")), str(rs.get("max", ""))])
rssi_table = table(["Device", "BLE Address", "Samples", "Min", "Avg", "Max"], rssi_rows, "No RSSI data")

# Decryption Summary
ds = analysis.get("decryption_summary")
ds_html = ""
if ds:
    dec = ds.get("decryption", {})
    dec_ok = int(dec.get("decrypted_successfully", 0))
    dec_total = int(dec.get("total_attempted", 0))
    dec_pct = (dec_ok / dec_total * 100) if dec_total > 0 else 0
    if dec_pct >= 95:
        pf_class = "pf-green"
    elif dec_pct >= 50:
        pf_class = "pf-yellow"
    else:
        pf_class = "pf-red"
    msg_html = ""
    msg_types = ds.get("link_messages", {})
    if msg_types:
        mt_rows = []
        for name, info in sorted(msg_types.items(), key=lambda x: x[1].get("type_id", -1)):
            mt_rows.append([esc(name), str(info.get("count", 0))])
        msg_html = table(["Message Type", "Count"], mt_rows, "")
    ds_html = f'''<div class="ds-summary">
  <div class="ds-row"><span>Capture</span><span><code>{esc(ds.get("capture_file", ""))}</code></span></div>
  <div class="ds-row"><span>Filter</span><span>{esc(ds.get("filter_method", ""))}</span></div>
  <div class="ds-row"><span>FIPS L2CAP Frames</span><span>{ds.get("fips_l2cap_frames", 0)}</span></div>
  <div class="ds-row"><span>Decrypted</span><span>{dec_ok}/{dec_total} ({dec_pct:.1f}%)</span></div>
</div>
<div style="padding:0 1.5rem 1rem">
  <div class="progress-bar"><div class="progress-fill {pf_class}" style="width:{dec_pct:.1f}%"></div></div>
</div>
{msg_html}'''

# ── Breadcrumbs ───────────────────────────────────────────────────────

short_commit = (fips_commit or commit or "")[:12]
breadcrumbs = f'''<nav class="breadcrumb">
  <a href="../../index.html">Dashboard</a>
  <span class="sep">›</span>
  <a href="https://github.com/Amperstrand/fips/tree/rebuild/macos-ble-upstream/{esc(fips_commit or commit or "")}">{esc(short_commit)}</a>
  <span class="sep">›</span>
  <span>{esc(scenario)}</span>
</nav>'''

# ── Assemble full HTML ────────────────────────────────────────────────

report_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIPS Lab — {esc(scenario)}</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='15' fill='%231f4068'/%3E%3Ctext x='16' y='22' text-anchor='middle' font-family='sans-serif' font-weight='bold' font-size='18' fill='white'%3EF%3C/text%3E%3C/svg%3E">
<meta name="description" content="FIPS Lab test report for {esc(scenario)}">
<meta property="og:title" content="FIPS Lab — {esc(scenario)}">
<meta property="og:description" content="Test report for {esc(scenario)} scenario — Verdict: {esc(verdict)}">
<meta property="og:type" content="article">
<style>
html{{scroll-behavior:smooth}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  background:#f0f2f5;color:#1a1a2e;line-height:1.6;font-size:15px}}
.header{{background:linear-gradient(135deg,#0a1628,#162447,#1f4068);color:#fff;padding:2rem;
  box-shadow:0 2px 8px rgba(0,0,0,.2)}}
.header h1{{font-size:1.4rem;font-weight:600;letter-spacing:.5px;margin-bottom:.5rem}}
.verdict-banner{{display:inline-block;font-size:1.1rem;font-weight:700;padding:.6rem 1.6rem;
  border-radius:8px;text-transform:uppercase;letter-spacing:.5px;margin:.5rem 0}}
.v-pass{{background:#e6f4ea;color:#137333}}
.v-fail{{background:#fce8e6;color:#d93025}}
.v-degraded{{background:#fef7e0;color:#b06000}}
.v-na{{background:#f1f3f4;color:#80868b}}
.meta-grid{{display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:1rem;font-size:.9rem;opacity:.9}}
.meta-grid dt{{font-weight:600;margin-right:.3rem}}
.meta-grid dd{{margin-right:1.5rem;font-family:"SF Mono",SFMono-Regular,Consolas,monospace;font-size:.85rem}}
.breadcrumb{{font-size:.85rem;padding:1rem 0;max-width:960px;margin:0 auto}}
.breadcrumb a{{color:#1f4068;text-decoration:none;font-weight:500}}
.breadcrumb a:hover{{text-decoration:underline}}
.breadcrumb .sep{{margin:0 .4rem;color:#999}}
.container{{max-width:960px;margin:0 auto;padding:0 1rem 3rem}}
section{{background:#fff;border-radius:8px;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}}
.section-header{{padding:1rem 1.5rem;border-bottom:1px solid #eee;font-size:1rem;font-weight:600;
  background:#fafbfc;color:#0a1628;display:flex;align-items:center;gap:.5rem;
  border-left:4px solid #1f4068}}
.section-header .icon{{font-size:1.1rem}}
.section-body{{padding:0}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;padding:.6rem 1rem;font-size:.75rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.5px;color:#666;background:#fafbfc;border-bottom:1px solid #eee}}
td{{padding:.55rem 1rem;font-size:.875rem;border-bottom:1px solid #f5f5f5;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#fafbfc}}
code{{font-family:"SF Mono",SFMono-Regular,Consolas,monospace;font-size:.82rem;
  background:#f1f3f4;padding:1px 5px;border-radius:3px}}
.empty-state{{padding:2rem;text-align:center;border:2px dashed #dde1e6;border-radius:6px;
  margin:1rem;color:#999;font-size:.9rem}}
.empty-state .empty-icon{{font-size:1.8rem;opacity:.35;display:block;margin-bottom:.5rem}}
.empty-state .empty-msg{{color:#80868b}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:1.5rem}}
.chart-card{{background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);
  transition:transform .2s ease,box-shadow .2s ease;position:relative}}
.chart-card:hover{{transform:scale(1.02);box-shadow:0 8px 24px rgba(0,0,0,.15)}}
.chart-card a{{display:block;position:relative}}
.chart-card img{{width:100%;height:auto;display:block}}
.chart-card .chart-overlay{{position:absolute;inset:0;background:rgba(15,22,40,.45);display:flex;
  align-items:center;justify-content:center;opacity:0;transition:opacity .2s ease;pointer-events:none}}
.chart-card:hover .chart-overlay{{opacity:1}}
.chart-overlay span{{color:#fff;font-size:.85rem;font-weight:600;padding:.4rem 1rem;
  background:rgba(255,255,255,.15);border-radius:4px;backdrop-filter:blur(2px)}}
.chart-label{{padding:.5rem 1rem;font-size:.8rem;font-weight:600;color:#666;text-align:center;
  background:#fafbfc;border-top:1px solid #eee}}
.ds-summary{{display:grid;grid-template-columns:200px 1fr;gap:.4rem 1rem;padding:1rem 1.5rem;font-size:.9rem}}
.ds-row{{display:contents}}
.ds-row span:first-child{{font-weight:600;color:#555}}
.ds-row span:last-child{{font-family:"SF Mono",SFMono-Regular,Consolas,monospace;font-size:.85rem}}
.back-link{{display:inline-block;margin-top:.5rem;font-size:.8rem;color:rgba(255,255,255,.7);text-decoration:none}}
.back-link:hover{{color:#fff}}
details[open] summary .icon{{}}
details summary::-webkit-details-marker{{display:none}}
details summary::marker{{display:none;content:""}}
details[open] summary span:first-of-type{{}}
@media(max-width:640px){{
  .charts{{grid-template-columns:1fr}}
  .meta-grid{{flex-direction:column;gap:.3rem}}
  th,td{{padding:.4rem .6rem;font-size:.8rem}}
  .header{{padding:1.2rem}}
  .verdict-banner{{font-size:.9rem;padding:.5rem 1rem}}
}}
.progress-bar{{background:#e8eaed;border-radius:4px;height:8px;overflow:hidden;margin-top:.3rem}}
.progress-fill{{height:100%;border-radius:4px;transition:width .4s ease}}
.pf-green{{background:#137333}}
.pf-yellow{{background:#b06000}}
.pf-red{{background:#d93025}}
.assertion-summary{{display:flex;align-items:center;gap:.75rem;padding:.75rem 1.5rem;font-size:.875rem;font-weight:600;border-bottom:1px solid #eee}}
.assertion-summary .summary-text{{white-space:nowrap}}
.back-to-top{{position:fixed;bottom:2rem;right:2rem;background:#1f4068;color:#fff;text-decoration:none;
  padding:.5rem 1rem;border-radius:6px;font-size:.8rem;font-weight:600;opacity:.7;transition:opacity .2s;z-index:100;
  box-shadow:0 2px 8px rgba(0,0,0,.2)}}
.back-to-top:hover{{opacity:1}}
.microfips-tag{{display:inline-block;font-size:.7rem;font-weight:600;padding:3px 10px;border-radius:12px;background:#fce4ec;color:#880e4f;letter-spacing:.3px;vertical-align:middle;white-space:nowrap}}
@media(prefers-color-scheme:dark){{
  body{{background:#0d1117;color:#c9d1d9}}
  section{{background:#161b22;box-shadow:0 1px 3px rgba(0,0,0,.3)}}
  .section-header{{background:#161b22;color:#c9d1d9;border-bottom-color:#30363d}}
  .chart-card{{background:#161b22}}
  .chart-label{{background:#161b22;color:#8b949e;border-top-color:#30363d}}
  th{{background:#21262d;color:#8b949e;border-bottom-color:#30363d}}
  td{{border-bottom-color:#21262d}}
  tr:hover td{{background:#1c2128}}
  code{{background:#21262d;color:#c9d1d9}}
  .empty-state{{border-color:#30363d}}
  .empty-state .empty-msg{{color:#8b949e}}
  .breadcrumb a{{color:#58a6ff}}
  .breadcrumb .sep{{color:#484f58}}
  .ds-row span:first-child{{color:#8b949e}}
  .v-pass{{background:#0f2918;color:#3fb950}}
  .v-fail{{background:#3d1214;color:#f85149}}
  .v-degraded{{background:#3d2e00;color:#d29922}}
  .v-na{{background:#21262d;color:#8b949e}}
  .progress-bar{{background:#21262d}}
  .pf-green{{background:#3fb950}}
  .pf-yellow{{background:#d29922}}
  .pf-red{{background:#f85149}}
  .assertion-summary{{border-bottom-color:#30363d}}
  .back-to-top{{background:#30363d;color:#c9d1d9}}
  .microfips-tag{{background:#3d1520;color:#f06292}}
  details summary .section-header{{background:#161b22;color:#c9d1d9}}
  details summary span:last-of-type{{color:#8b949e}}
  ul a{{color:#58a6ff !important}}
}}
</style>
</head>
<body>
<div class="header">
  {breadcrumbs}
  <h1>{esc(scenario)}</h1>
  <div class="verdict-banner {verdict_css(verdict)}">{verdict_icon(verdict)} {esc(verdict)}</div>
  <dl class="meta-grid">
    <div><dt>Timestamp:</dt><dd>{esc(fmt_ts(timestamp))}</dd></div>
    <div><dt>Duration:</dt><dd>{esc(fmt_dur(duration))}</dd></div>
    <div><dt>Commit:</dt><dd><code>{esc(short_commit)}</code></dd></div>
    {'<div><dt>Branch:</dt><dd>' + esc(fips_branch) + '</dd></div>' if fips_branch else ''}
    {'<div><dt>Dirty:</dt><dd>yes</dd></div>' if fips_dirty else ''}
    {'<div><dt>microfips:</dt><dd><code>' + esc(microfips_commit[:12]) + '</code> ' + ('<span class="microfips-tag">' + esc(microfips_mode) + '</span>' if microfips_mode else '') + '</dd></div>' if microfips_commit else ''}
  </dl>
  <a class="back-link" href="../../index.html">← Back to Dashboard</a>
</div>
<div class="container">
'''

# ── Charts ─────────────────────────────────────────────────────────────

if charts_html:
    report_html += f'''<section>
  <div class="section-header"><span class="icon">📊</span> Charts</div>
  <div class="section-body"><div class="charts">{charts_html}</div></div>
</section>
'''

# ── Assertions ─────────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">🔍</span> Assertions</div>
  <div class="section-body">{as_summary_html}{as_table}</div>
</section>
'''

# ── Connections ────────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">🔗</span> Connections</div>
  <div class="section-body">{conn_table}</div>
</section>
'''

# ── MMP Metrics ────────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">📈</span> MMP Metrics</div>
  <div class="section-body">{pm_table}</div>
</section>
'''

# ── Key Exchange ───────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">🔑</span> Key Exchange</div>
  <div class="section-body">{ke_table}</div>
</section>
'''

# ── Rekey Stats ────────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">🔄</span> Rekey Analysis</div>
  <div class="section-body">{rk_table}</div>
</section>
'''

# ── Disconnects ────────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">⚡</span> Disconnects</div>
  <div class="section-body">{dc_table}</div>
</section>
'''

# ── Keylog Verification ───────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">🔐</span> Keylog Verification</div>
  <div class="section-body">{kv_table}</div>
</section>
'''

# ── RSSI ───────────────────────────────────────────────────────────────

report_html += f'''<section>
  <div class="section-header"><span class="icon">📶</span> BLE RSSI</div>
  <div class="section-body">{rssi_table}</div>
</section>
'''

# ── Decryption Summary ────────────────────────────────────────────────

if ds_html:
    report_html += f'''<section>
  <div class="section-header"><span class="icon">🔓</span> BLE Capture Decryption</div>
  <div class="section-body">{ds_html}</div>
</section>
'''

# ── Raw files link ─────────────────────────────────────────────────────

raw_links = []
for fname, label in [("analysis.json", "Analysis JSON"), ("analysis.md", "Analysis Markdown"), ("metrics-timeseries.json", "Metrics Timeseries")]:
    if os.path.exists(os.path.join(target_dir, fname)):
        raw_links.append(f'<li><a href="{esc(fname)}" style="color:#1f4068;font-weight:500">{esc(label)}</a> <span style="color:#999;font-size:.8rem">({esc(fname)})</span></li>')

raw_data_html = ""
if raw_links:
    links_str = "\n".join(raw_links)
    raw_data_html = f'''<section>
  <details style="border:none;margin:0">
    <summary class="section-header" style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:.5rem;padding:1rem 1.5rem;border-bottom:1px solid #eee;font-size:1rem;font-weight:600;background:#fafbfc;color:#0a1628;border-left:4px solid #1f4068">
      <span class="icon">📁</span> Raw Data
      <span style="font-size:.7rem;color:#999;margin-left:auto">▶ expand</span>
    </summary>
    <div class="section-body" style="padding:1rem 1.5rem">
      <ul style="list-style:none;display:flex;flex-direction:column;gap:.6rem;font-size:.9rem">
        {links_str}
      </ul>
    </div>
  </details>
</section>
'''

report_html += raw_data_html

report_html += '</div>\n<a href="#" class="back-to-top">↑ Top</a>\n</body>\n</html>\n'

# ── Write ──────────────────────────────────────────────────────────────

out_path = os.path.join(target_dir, "report.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(report_html)

print(f"  Generated {out_path}")
PYREPORT
}

generate_report_html "$TARGET_DIR"
echo "==> Generated report.html"

# ── Purge old runs ───────────────────────────────────────────────────

purge_old_runs() {
  local reports_dir="$1"
  local keep="$2"

  [ ! -d "$reports_dir" ] && return 0

  local -a dirs=()
  local -a times=()

  for hash_dir in "$reports_dir"/*/; do
    [ ! -d "$hash_dir" ] && continue
    local hash_name
    hash_name="$(basename "$hash_dir")"

    local newest=""
    for ts_dir in "$hash_dir"*/; do
      [ ! -d "$ts_dir" ] && continue
      local ts_name
      ts_name="$(basename "$ts_dir")"
      if [ -z "$newest" ] || [ "$ts_name" \> "$newest" ]; then
        newest="$ts_name"
      fi
    done

    if [ -n "$newest" ]; then
      dirs+=("$hash_name")
      times+=("$newest")
    fi
  done

  local count=${#dirs[@]}
  if [ "$count" -le "$keep" ]; then
    return 0
  fi

  local to_delete=$((count - keep))
  local sort_file
  sort_file="$(mktemp)"

  local i=0
  for ((i = 0; i < ${#dirs[@]}; i++)); do
    echo "${times[$i]} ${dirs[$i]}" >> "$sort_file"
  done

  local deleted=0
  while IFS=' ' read -r _ hash_name; do
    if [ "$deleted" -ge "$to_delete" ]; then
      break
    fi
    echo "==> Purging old report: $hash_name"
    rm -rf "$reports_dir/$hash_name"
    deleted=$((deleted + 1))
  done < <(sort "$sort_file")

  rm -f "$sort_file"
}

purge_old_runs "$WORK/gh-pages/reports" "$KEEP"

# ── Format timestamp for display ─────────────────────────────────────

format_timestamp() {
  local ts="$1"
  if command -v python3 &>/dev/null; then
    python3 -c "
import sys
from datetime import datetime
ts = '${ts}'
for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H-%M-%S', '%Y-%m-%dT%H-%M-%SZ', '%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
    try:
        dt = datetime.strptime(ts, fmt)
        months = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        print('%s %d, %d %02d:%02d UTC' % (months[dt.month], dt.day, dt.year, dt.hour, dt.minute))
        sys.exit(0)
    except ValueError:
        continue
print(ts)
" 2>/dev/null && return 0
  fi
  echo "$ts" | sed 's/T/ /;s/Z/ UTC/'
}

# ── Generate dashboard index.html ────────────────────────────────────

echo "==> Generating dashboard..."

generate_dashboard() {
  local reports_dir="$WORK/gh-pages/reports"
  local dash_file="$WORK/gh-pages/index.html"

  local runs_file
  runs_file="$(mktemp)"

  [ -d "$reports_dir" ] || true

  for hash_dir in "$reports_dir"/*/; do
    [ ! -d "$hash_dir" ] && continue
    local hash_name
    hash_name="$(basename "$hash_dir")"

    for ts_dir in "$hash_dir"*/; do
      [ ! -d "$ts_dir" ] && continue
      local ts_name
      ts_name="$(basename "$ts_dir")"
      local meta_json="$ts_dir/metadata.json"

      [ ! -f "$meta_json" ] && continue

      local r_scenario r_timestamp r_duration r_verdict r_assert_total r_assert_passed
      local r_fips_commit r_fips_branch r_microfips_commit r_microfips_mode

      r_scenario="$(json_string "$meta_json" scenario || true)"
      r_timestamp="$(json_string "$meta_json" timestamp || true)"
      r_duration="$(json_number "$meta_json" duration_secs || true)"

      r_scenario="${r_scenario:-unknown}"
      r_duration="${r_duration:-0}"

      r_fips_commit="$(json_nested_string "$meta_json" fips_git commit || true)"
      r_fips_commit="${r_fips_commit:-$(json_string "$meta_json" commit || true)}"
      r_fips_branch="$(json_nested_string "$meta_json" fips_git branch || true)"

      r_microfips_commit="$(json_nested_string "$meta_json" microfips_git commit || true)"
      r_microfips_mode="$(json_nested_string "$meta_json" microfips_git mode || true)"

      r_verdict="N/A"
      r_assert_total="0"
      r_assert_passed="0"

      local analysis_json="$ts_dir/analysis.json"
      if [ -f "$analysis_json" ]; then
        r_verdict="$(json_string "$analysis_json" verdict || true)"
        r_verdict="${r_verdict:-N/A}"
        if grep -q '"passed"' "$analysis_json" 2>/dev/null; then
          r_assert_passed="$(grep -c '"passed"[[:space:]]*:[[:space:]]*true' "$analysis_json" || true)"
          local at="$(grep -c '"passed"[[:space:]]*:' "$analysis_json" || true)"
          r_assert_total="${at:-0}"
        fi
      fi

      local sort_ts="${r_timestamp:-$ts_name}"
      sort_ts="$(echo "$sort_ts" | tr -d ':Z' | sed 's/T/ /')"

      echo "${sort_ts}|${hash_name}|${ts_name}|${r_scenario}|${r_timestamp}|${r_duration}|${r_verdict}|${r_assert_total}|${r_assert_passed}|${r_fips_commit}|${r_fips_branch}|${r_microfips_commit:-}|${r_microfips_mode:-}" >> "$runs_file"
    done
  done

  # ── Compute summary stats ──────────────────────────────────────────

  local total_runs=0 total_commits=0 total_pass=0 last_updated=""

  if [ -f "$runs_file" ] && [ -s "$runs_file" ]; then
    total_runs="$(wc -l < "$runs_file" | tr -d ' ')"
    total_commits="$(cut -d'|' -f2 "$runs_file" | sort -u | wc -l | tr -d ' ')"
    last_updated="$(sort -r "$runs_file" | head -1 | cut -d'|' -f5)"
    # Count PASS verdicts
    total_pass="$(grep -c '|PASS|' "$runs_file" || true)"
  fi

  total_runs="${total_runs:-0}"
  total_commits="${total_commits:-0}"
  total_pass="${total_pass:-0}"
  last_updated="${last_updated:-N/A}"

  local last_updated_display
  if [ "$last_updated" != "N/A" ] && [ -n "$last_updated" ]; then
    last_updated_display="$(format_timestamp "$last_updated")"
  else
    last_updated_display="N/A"
  fi

  local pass_rate="N/A"
  if [ "$total_runs" -gt 0 ]; then
    pass_rate="$(python3 -c "print(f'{${total_pass}/${total_runs}*100:.0f}%')" 2>/dev/null || echo "${total_pass}/${total_runs}")"
  fi

  # Latest verdict for header badge
  local latest_verdict="N/A"
  local latest_verdict_class="verdict-na"
  if [ -f "$runs_file" ] && [ -s "$runs_file" ]; then
    latest_verdict="$(sort -r "$runs_file" | head -1 | cut -d'|' -f7)"
    case "$latest_verdict" in
      PASS)              latest_verdict_class="verdict-pass" ;;
      FAIL)              latest_verdict_class="verdict-fail" ;;
      DEGRADED)          latest_verdict_class="verdict-degraded" ;;
      INSUFFICIENT_DATA) latest_verdict_class="verdict-insufficient_data" ;;
      *)                 latest_verdict_class="verdict-na" ;;
    esac
  fi
  latest_verdict="${latest_verdict:-N/A}"

  # ── Begin HTML output ──────────────────────────────────────────────

  cat > "$dash_file" <<'DASHHEAD'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIPS Lab Test Reports</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='15' fill='%231f4068'/%3E%3Ctext x='16' y='22' text-anchor='middle' font-family='sans-serif' font-weight='bold' font-size='18' fill='white'%3EF%3C/text%3E%3C/svg%3E">
<meta property="og:title" content="FIPS Lab Test Reports">
<meta property="og:description" content="Dashboard for FIPS Lab physical-device test results and analysis">
<meta property="og:type" content="website">
<meta name="description" content="FIPS Lab test report dashboard showing BLE connectivity, MMP metrics, and rekey analysis">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:#f0f2f5;color:#1a1a2e;line-height:1.5}
.header{background:linear-gradient(135deg,#0a1628,#162447,#1f4068);color:#fff;padding:1.5rem 2rem;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 8px rgba(0,0,0,.2)}
.header h1{font-size:1.5rem;font-weight:600;letter-spacing:.5px}
.header .updated{font-size:.85rem;opacity:.8}
.container{max-width:1100px;margin:2rem auto;padding:0 1rem}
.summary{display:flex;gap:1rem;margin-bottom:2rem;flex-wrap:wrap}
.summary-card{background:#fff;border-radius:8px;padding:1rem 1.5rem;flex:1;min-width:160px;box-shadow:0 1px 3px rgba(0,0,0,.08);transition:transform .15s ease,box-shadow .15s ease;cursor:default}
.summary-card:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.12)}
.summary-card .label{font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;color:#666;margin-bottom:.25rem}
.summary-card .value{font-size:1.75rem;font-weight:700;color:#1a1a2e}
.commit-group{background:#fff;border-radius:8px;margin-bottom:1.5rem;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}
.commit-header{padding:1rem 1.5rem;border-bottom:1px solid #eee;display:flex;align-items:center;gap:1rem;flex-wrap:wrap}
.commit-header .hash{font-family:"SF Mono",SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace;font-size:1rem;font-weight:600}
.commit-header .hash a{color:#1f4068;text-decoration:none}
.commit-header .hash a:hover{text-decoration:underline}
.branch-badge{display:inline-block;font-size:.7rem;font-weight:600;padding:3px 10px;border-radius:12px;background:#e8f0fe;color:#1f4068;letter-spacing:.3px;vertical-align:middle}
.runs{padding:.5rem 0}
.run-row{display:grid;grid-template-columns:170px 150px 70px 80px 120px 1fr;align-items:center;padding:.65rem 1.5rem;border-bottom:1px solid #f5f5f5;font-size:.875rem;gap:.5rem}
.run-row:last-child{border-bottom:none}
.run-row:hover{background:#fafbfc}
.chart-row{display:flex;gap:1rem;padding:.5rem 1.5rem 1rem;background:#fafbfc;border-bottom:1px solid #eee;flex-wrap:wrap}
.chart-row.grid-2x2{display:grid;grid-template-columns:1fr 1fr;max-width:860px}
.chart-item{flex:1;min-width:260px;max-width:420px;position:relative}
.grid-2x2 .chart-item{max-width:none}
.chart-item img{width:100%;height:auto;display:block;border-radius:4px}
.chart-link{display:block;position:relative;text-decoration:none;color:inherit}
.chart-link .chart-hover-label{position:absolute;bottom:0;left:0;right:0;padding:8px 0;background:rgba(15,25,50,.75);color:#fff;font-size:.75rem;text-align:center;letter-spacing:.3px;opacity:0;transition:opacity .2s ease;border-radius:0 0 4px 4px}
.chart-link:hover .chart-hover-label{opacity:1}
.chart-link:hover img{box-shadow:0 2px 8px rgba(0,0,0,.15)}
.run-time{color:#555;font-variant-numeric:tabular-nums}
.run-scenario{font-family:"SF Mono",SFMono-Regular,Consolas,monospace;font-size:.8rem;color:#444}
.verdict{display:inline-block;font-size:.75rem;font-weight:600;padding:3px 10px;border-radius:12px;text-transform:uppercase;letter-spacing:.3px;text-align:center}
.verdict-pass{background:#e6f4ea;color:#137333}
.verdict-fail{background:#fce8e6;color:#d93025}
.verdict-degraded{background:#fef7e0;color:#b06000}
.verdict-insufficient_data,.verdict-na{background:#f1f3f4;color:#80868b}
.assertions{font-size:.8rem;color:#555}
.assertions-bar{display:inline-block;height:6px;border-radius:3px;vertical-align:middle;margin-right:6px}
.assertions-bar-pass{background:#137333}
.assertions-bar-fail{background:#d93025}
.run-link a{color:#1f4068;text-decoration:none;font-weight:500;font-size:.8rem}
.run-link a:hover{text-decoration:underline}
.run-header{display:grid;grid-template-columns:170px 150px 70px 80px 120px 1fr;padding:.5rem 1.5rem;background:#fafbfc;font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:#666;border-bottom:1px solid #eee;gap:.5rem}
.empty{padding:3rem;text-align:center;color:#888;font-size:1rem}
.footer{max-width:1100px;margin:0 auto 2rem;padding:1rem;text-align:center;font-size:.8rem;color:#888;border-top:1px solid #e0e0e0}
.footer a{color:#1f4068;text-decoration:none}
.footer a:hover{text-decoration:underline}
.scenario-badge{display:inline-block;font-size:.7rem;font-weight:500;padding:3px 10px;border-radius:12px;background:#f0f1f3;color:#555;letter-spacing:.3px;vertical-align:middle}
.microfips-tag{display:inline-block;font-size:.7rem;font-weight:600;padding:3px 10px;border-radius:12px;background:#fce4ec;color:#880e4f;letter-spacing:.3px;vertical-align:middle;white-space:nowrap}
.view-report{padding:.3rem 1.5rem .8rem;font-size:.8rem}
.view-report a{color:#1f4068;text-decoration:none;font-weight:500}
.view-report a:hover{text-decoration:underline}
@media(max-width:768px){.run-row{display:flex;flex-wrap:wrap;gap:.3rem;padding:.5rem 1rem}.run-row span{flex:1 1 45%;min-width:100px}.run-header{display:none}.header{flex-direction:column;gap:.5rem;text-align:center}.summary-card{min-width:100%}.chart-row{padding:.5rem 1rem}.chart-item{min-width:100%;max-width:100%}}
@media(prefers-color-scheme:dark){body{background:#0d1117;color:#c9d1d9}.summary-card{background:#161b22;box-shadow:0 1px 3px rgba(0,0,0,.3)}.summary-card:hover{box-shadow:0 4px 12px rgba(0,0,0,.5)}.summary-card .label{color:#8b949e}.summary-card .value{color:#f0f6fc}.commit-group{background:#161b22;box-shadow:0 1px 3px rgba(0,0,0,.3)}.commit-header{border-bottom-color:#30363d}.commit-header .hash a{color:#58a6ff}.branch-badge{background:#1f2937;color:#58a6ff}.run-row{border-bottom-color:#21262d}.run-row:hover{background:#1c2128}.chart-row{background:#0d1117;border-bottom-color:#30363d}.run-header{background:#161b22;border-bottom-color:#30363d;color:#8b949e}.run-time,.run-scenario,.assertions{color:#8b949e}.run-link a{color:#58a6ff}.verdict-pass{background:#0d2818;color:#3fb950}.verdict-fail{background:#3d1214;color:#f85149}.verdict-degraded{background:#3d2e00;color:#d29922}.verdict-insufficient_data,.verdict-na{background:#21262d;color:#8b949e}.footer{color:#484f58;border-top-color:#30363d}.footer a{color:#58a6ff}.chart-link:hover img{box-shadow:0 2px 8px rgba(0,0,0,.4)}.scenario-badge{background:#21262d;color:#8b949e}.view-report a{color:#58a6ff}.microfips-tag{background:#3d1520;color:#f06292}}
.matrix-section{background:#fff;border-radius:8px;margin-bottom:2rem;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}
.matrix-header{padding:1rem 1.5rem;border-bottom:1px solid #eee;font-size:1rem;font-weight:600;background:#fafbfc;color:#0a1628;border-left:4px solid #1f4068}
.matrix-table{width:auto;margin:0 auto;border-collapse:collapse}
.matrix-table th{padding:.5rem 1.2rem;font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:#666;background:#fafbfc;border-bottom:1px solid #eee;text-align:center}
.matrix-table td{padding:.6rem 1rem;text-align:center;border-bottom:1px solid #f5f5f5;font-size:.85rem}
.matrix-row-label{font-weight:600;text-align:left!important;padding-left:1.5rem!important;color:#444}
.matrix-cell{min-width:90px}
.matrix-cell a{display:block;text-decoration:none;color:inherit;font-weight:600;padding:.3rem .6rem;border-radius:4px;transition:opacity .15s ease}
.matrix-cell a:hover{opacity:.75}
.matrix-pass{background:#e6f4ea;color:#137333}
.matrix-fail{background:#fce8e6;color:#d93025}
.matrix-degraded{background:#fef7e0;color:#b06000}
.matrix-empty{background:#f1f3f4;color:#80868b}
@media(prefers-color-scheme:dark){.matrix-section{background:#161b22;box-shadow:0 1px 3px rgba(0,0,0,.3)}.matrix-header{background:#161b22;color:#c9d1d9;border-bottom-color:#30363d}.matrix-table th{background:#21262d;color:#8b949e;border-bottom-color:#30363d}.matrix-table td{border-bottom-color:#21262d}.matrix-row-label{color:#c9d1d9}.matrix-pass{background:#0d2818;color:#3fb950}.matrix-fail{background:#3d1214;color:#f85149}.matrix-degraded{background:#3d2e00;color:#d29922}.matrix-empty{background:#21262d;color:#8b949e}}
</style>
</head>
<body>
<div class="header">
<h1>FIPS Lab Test Reports</h1>
<div class="updated">Last updated: PLACEHOLDER_LAST_UPDATED <span class="verdict PLACEHOLDER_LATEST_VERDICT_CLASS" style="vertical-align:middle;margin-left:8px">Latest: PLACEHOLDER_LATEST_VERDICT</span></div>
</div>
<div class="container">
<div class="summary">
<div class="summary-card"><div class="label">Total Runs</div><div class="value">PLACEHOLDER_TOTAL_RUNS</div></div>
<div class="summary-card"><div class="label">Commits Tested</div><div class="value">PLACEHOLDER_TOTAL_COMMITS</div></div>
<div class="summary-card"><div class="label">Pass Rate</div><div class="value">PLACEHOLDER_PASS_RATE</div></div>
</div>
DASHHEAD

  # ── Device Compatibility Matrix ────────────────────────────────────
  # (macOS bash 3.x has no declare -A, so we use a temp file as a kv store)

  mx_normkey() {
    local a="$1" b="$2"
    if [[ "$a" < "$b" ]]; then echo "${b}:${a}"; else echo "${a}:${b}"; fi
  }

  local mx_tmpfile
  mx_tmpfile="$(mktemp)"
  if [ -f "$runs_file" ] && [ -s "$runs_file" ]; then
    while IFS='|' read -r _ mx_hash mx_ts mx_scenario _ _ mx_verdict _ _ _ _; do
      local mx_pairs_raw=""
      case "$mx_scenario" in
        lab-2node-ble)       mx_pairs_raw="linux mac" ;;
        lab-3node-isolated)  mx_pairs_raw="linux mac esp32 linux" ;;
        lab-3node-m5pico)    mx_pairs_raw="linux mac linux m5pico" ;;
        microfips-smoke)     mx_pairs_raw="esp32 linux" ;;
        *)                   continue ;;
      esac

      local mx_a="" mx_b=""
      for mx_w in $mx_pairs_raw; do
        if [ -z "$mx_a" ]; then mx_a="$mx_w"; continue; fi
        mx_b="$mx_w"
        local mx_nk
        mx_nk="$(mx_normkey "$mx_a" "$mx_b")"
        if ! grep -q "^${mx_nk}|" "$mx_tmpfile" 2>/dev/null; then
          local mx_rp="reports/${mx_hash}/${mx_ts}/"
          [ -f "$reports_dir/${mx_hash}/${mx_ts}/report.html" ] && mx_rp="reports/${mx_hash}/${mx_ts}/report.html"
          echo "${mx_nk}|${mx_verdict}|${mx_rp}" >> "$mx_tmpfile"
        fi
        mx_a=""
      done
    done < <(sort -t'|' -k1 -r "$runs_file")
  fi

  {
    echo '<div class="matrix-section">'
    echo '<div class="matrix-header">🔗 Device Compatibility Matrix</div>'
    echo '<table class="matrix-table">'
    echo '<thead><tr><th></th><th>linux</th><th>esp32</th><th>m5pico</th></tr></thead>'
    echo '<tbody>'

    for mx_row in "mac" "linux"; do
      mx_row_html="<tr><td class=\"matrix-row-label\">${mx_row}</td>"
      for mx_col in "linux" "esp32" "m5pico"; do
        mx_ckey="$(mx_normkey "$mx_row" "$mx_col")"

        mx_line="$(grep "^${mx_ckey}|" "$mx_tmpfile" 2>/dev/null | head -1 || true)"
        if [ -n "$mx_line" ]; then
          mx_cv="$(echo "$mx_line" | cut -d'|' -f2)"
          mx_cp="$(echo "$mx_line" | cut -d'|' -f3)"
          case "$mx_cv" in
            PASS)     mx_cc="matrix-pass" ;;
            FAIL)     mx_cc="matrix-fail" ;;
            DEGRADED) mx_cc="matrix-degraded" ;;
            *)        mx_cc="matrix-empty" ;;
          esac
          mx_row_html="${mx_row_html}<td class=\"matrix-cell ${mx_cc}\"><a href=\"${mx_cp}\">${mx_cv}</a></td>"
        else
          mx_row_html="${mx_row_html}<td class=\"matrix-cell matrix-empty\">—</td>"
        fi
      done
      echo "${mx_row_html}</tr>"
    done

    echo '</tbody></table></div>'
  } >> "$dash_file"
  rm -f "$mx_tmpfile"

  # ── Emit commit groups ─────────────────────────────────────────────

  if [ -f "$runs_file" ] && [ -s "$runs_file" ]; then
    local commits_file
    commits_file="$(mktemp)"
    cut -d'|' -f1,2 "$runs_file" | awk -F'|' '{print $2 "|" $1}' | sort -t'|' -k2 -r | awk -F'|' '{if(!seen[$1]++){print $1}}' > "$commits_file"

    while IFS= read -r hash_name; do
      [ -z "$hash_name" ] && continue
      local short_hash="${hash_name:0:12}"
      local commit_url="https://github.com/Amperstrand/fips/tree/rebuild/macos-ble-upstream/${hash_name}"

      local first_line
      first_line="$(grep "|${hash_name}|" "$runs_file" | sort -t'|' -k1 -r | head -1)"
      local group_branch
      group_branch="$(echo "$first_line" | cut -d'|' -f11)"
      local group_scenario
      group_scenario="$(echo "$first_line" | cut -d'|' -f4)"

      echo '<div class="commit-group">' >> "$dash_file"
      echo '<div class="commit-header">' >> "$dash_file"
      echo "<span class=\"hash\"><a href=\"${commit_url}\">${hash_name}</a></span>" >> "$dash_file"
      if [ -n "$group_branch" ]; then
        echo "<span class=\"branch-badge\">${group_branch}</span>" >> "$dash_file"
      fi
      if [ -n "$group_scenario" ] && [ "$group_scenario" != "unknown" ]; then
        echo "<span class=\"scenario-badge\">${group_scenario}</span>" >> "$dash_file"
      fi
      echo '</div>' >> "$dash_file"

      # Table header for runs
      echo '<div class="run-header"><span>Timestamp</span><span>Scenario</span><span>Duration</span><span>Verdict</span><span>Assertions</span><span>Details</span></div>' >> "$dash_file"
      echo '<div class="runs">' >> "$dash_file"

      grep "|${hash_name}|" "$runs_file" | sort -t'|' -k1 -r | while IFS='|' read -r _ _ ts_name r_scenario r_timestamp r_duration r_verdict r_assert_total r_assert_passed r_fips_commit r_fips_branch r_microfips_commit r_microfips_mode; do
        local display_time
        if [ -n "$r_timestamp" ]; then
          display_time="$(format_timestamp "$r_timestamp")"
        else
          display_time="$ts_name"
        fi

        # Format duration
        local duration_display
        if [ -n "$r_duration" ] && [ "$r_duration" != "0" ]; then
          local mins=$((r_duration / 60))
          local secs=$((r_duration % 60))
          duration_display="${mins}m${secs}s"
        else
          duration_display="N/A"
        fi

        # Verdict CSS class
        local verdict_class
        case "$r_verdict" in
          PASS)            verdict_class="verdict-pass" ;;
          FAIL)            verdict_class="verdict-fail" ;;
          DEGRADED)        verdict_class="verdict-degraded" ;;
          INSUFFICIENT_DATA) verdict_class="verdict-insufficient_data" ;;
          *)               verdict_class="verdict-na" ;;
        esac

        local verdict_display="${r_verdict:-N/A}"

        # Assertions display
        local assertions_display
        if [ -n "$r_assert_total" ] && [ "$r_assert_total" != "0" ]; then
          assertions_display="${r_assert_passed}/${r_assert_total}"
        else
          assertions_display="N/A"
        fi

        local report_path="reports/${hash_name}/${ts_name}/"
        local report_label="Browse"
        if [ -f "$reports_dir/${hash_name}/${ts_name}/report.html" ]; then
          report_path="reports/${hash_name}/${ts_name}/report.html"
          report_label="Report"
        elif [ -f "$reports_dir/${hash_name}/${ts_name}/analysis.md" ]; then
          report_path="reports/${hash_name}/${ts_name}/analysis.md"
          report_label="Analysis"
        elif [ -f "$reports_dir/${hash_name}/${ts_name}/analysis.txt" ]; then
          report_path="reports/${hash_name}/${ts_name}/analysis.txt"
          report_label="Analysis"
        fi

        echo "<div class=\"run-row\">" >> "$dash_file"
        echo "<span class=\"run-time\">${display_time}</span>" >> "$dash_file"
        local scenario_display="${r_scenario}"
        if [ -n "$r_microfips_commit" ] && [ "$r_microfips_commit" != "" ]; then
          scenario_display="${r_scenario} <span class=\"microfips-tag\">µ ${r_microfips_mode:-ble} ${r_microfips_commit:0:8}</span>"
        fi
        echo "<span class=\"run-scenario\"><strong>${scenario_display}</strong></span>" >> "$dash_file"
        echo "<span>${duration_display}</span>" >> "$dash_file"
        echo "<span class=\"verdict ${verdict_class}\">${verdict_display}</span>" >> "$dash_file"
        echo "<span class=\"assertions\">${assertions_display}</span>" >> "$dash_file"
        echo "<span class=\"run-link\"><a href=\"${report_path}\">${report_label}</a></span>" >> "$dash_file"
        echo "</div>" >> "$dash_file"

        local chart_rtt="$reports_dir/${hash_name}/${ts_name}/chart-rtt.svg"
        local chart_peers="$reports_dir/${hash_name}/${ts_name}/chart-peers.svg"
        local chart_rekeys="$reports_dir/${hash_name}/${ts_name}/chart-rekeys.svg"
        local chart_rssi="$reports_dir/${hash_name}/${ts_name}/chart-rssi.svg"
        local has_charts=false
        [ -f "$chart_rtt" ] || [ -f "$chart_peers" ] || [ -f "$chart_rekeys" ] || [ -f "$chart_rssi" ] && has_charts=true

        if $has_charts; then
          local chart_count=0
          [ -f "$chart_rtt" ] && chart_count=$((chart_count + 1))
          [ -f "$chart_peers" ] && chart_count=$((chart_count + 1))
          [ -f "$chart_rekeys" ] && chart_count=$((chart_count + 1))
          [ -f "$chart_rssi" ] && chart_count=$((chart_count + 1))
          if [ "$chart_count" -ge 4 ]; then
            echo '<div class="chart-row grid-2x2">' >> "$dash_file"
          else
            echo '<div class="chart-row">' >> "$dash_file"
          fi
          if [ -f "$chart_rtt" ]; then
            echo "<div class=\"chart-item\"><a class=\"chart-link\" href=\"reports/${hash_name}/${ts_name}/chart-rtt.svg\" target=\"_blank\" rel=\"noopener\"><img src=\"reports/${hash_name}/${ts_name}/chart-rtt.svg\" alt=\"RTT Chart\" loading=\"lazy\"/><span class=\"chart-hover-label\">View full size</span></a></div>" >> "$dash_file"
          fi
          if [ -f "$chart_peers" ]; then
            echo "<div class=\"chart-item\"><a class=\"chart-link\" href=\"reports/${hash_name}/${ts_name}/chart-peers.svg\" target=\"_blank\" rel=\"noopener\"><img src=\"reports/${hash_name}/${ts_name}/chart-peers.svg\" alt=\"Peer Count Chart\" loading=\"lazy\"/><span class=\"chart-hover-label\">View full size</span></a></div>" >> "$dash_file"
          fi
          if [ -f "$chart_rekeys" ]; then
            echo "<div class=\"chart-item\"><a class=\"chart-link\" href=\"reports/${hash_name}/${ts_name}/chart-rekeys.svg\" target=\"_blank\" rel=\"noopener\"><img src=\"reports/${hash_name}/${ts_name}/chart-rekeys.svg\" alt=\"Rekey Events Chart\" loading=\"lazy\"/><span class=\"chart-hover-label\">View full size</span></a></div>" >> "$dash_file"
          fi
          if [ -f "$chart_rssi" ]; then
            echo "<div class=\"chart-item\"><a class=\"chart-link\" href=\"reports/${hash_name}/${ts_name}/chart-rssi.svg\" target=\"_blank\" rel=\"noopener\"><img src=\"reports/${hash_name}/${ts_name}/chart-rssi.svg\" alt=\"RSSI Chart\" loading=\"lazy\"/><span class=\"chart-hover-label\">View full size</span></a></div>" >> "$dash_file"
          fi
           echo '</div>' >> "$dash_file"
          echo "<div class=\"view-report\"><a href=\"${report_path}\" target=\"_blank\" rel=\"noopener\">View full report →</a></div>" >> "$dash_file"
        fi
      done

      echo '</div>' >> "$dash_file"
      echo '</div>' >> "$dash_file"
    done < "$commits_file"

    rm -f "$commits_file"
  else
    echo '<div class="empty">No test reports found.</div>' >> "$dash_file"
  fi

  # ── Close HTML ─────────────────────────────────────────────────────

  cat >> "$dash_file" <<'DASHFOOT'
</div>
<div class="footer">Powered by <a href="https://github.com/Amperstrand/fips-lab">fips-lab</a></div>
</body>
</html>
DASHFOOT

  # ── Replace placeholders ───────────────────────────────────────────

  sed -i.bak "s|PLACEHOLDER_LAST_UPDATED|${last_updated_display}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_TOTAL_RUNS|${total_runs}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_TOTAL_COMMITS|${total_commits}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_PASS_RATE|${pass_rate}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_LATEST_VERDICT|${latest_verdict}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_LATEST_VERDICT_CLASS|${latest_verdict_class}|g" "$dash_file"
  rm -f "${dash_file}.bak"

  rm -f "$runs_file"
}

generate_dashboard

# ── Commit and push ──────────────────────────────────────────────────

git add -A
git commit -m "report: fips-${SHORT} ${DIR_TIMESTAMP}" || true

echo "==> Pushing to gh-pages..."
git push -f origin gh-pages 2>&1

# ── Print URLs ───────────────────────────────────────────────────────

HTTPS_URL="${REMOTE_URL/git@github.com:/https://github.com/}"
HTTPS_URL="${HTTPS_URL%.git}"
REPO_NAME="$(basename "$HTTPS_URL")"
ORG_NAME="$(dirname "$HTTPS_URL" | xargs basename)"
GITHUB_PAGES_BASE="https://${ORG_NAME}.github.io/${REPO_NAME}"

echo ""
echo "==> Report published to gh-pages"
echo "==> Run:     ${GITHUB_PAGES_BASE}/${TARGET_DIR}/"
echo "==> Dashboard: ${GITHUB_PAGES_BASE}/"
