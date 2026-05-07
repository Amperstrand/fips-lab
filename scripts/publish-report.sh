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
      -e 's|"user": "ubuntu"|"user": "REDACTED"|g' \
      -e 's|"user": "[^"]*"|"user": "REDACTED"|g' \
      "$file" 2>/dev/null
  else
    # macOS sed
    sed -i '' \
      -e 's|/Users/[^"\\ ]*|/Users/REDACTED|g' \
      -e 's|/home/[^"\\ ]*|/home/REDACTED|g' \
      -e 's|/tmp/[^"\\ ]*|/tmp/REDACTED|g' \
      -e 's|/run/[^"\\ ]*|/run/REDACTED|g' \
      -e 's|"user": "ubuntu"|"user": "REDACTED"|g' \
      -e 's|"user": "[^"]*"|"user": "REDACTED"|g' \
      "$file" 2>/dev/null
  fi
}

for json_file in "$TARGET_DIR"/*.json; do
  [ -f "$json_file" ] && redact_file "$json_file"
done
for yaml_file in "$TARGET_DIR"/*.yaml; do
  [ -f "$yaml_file" ] && redact_file "$yaml_file"
done

echo "==> Redacted local paths and usernames from published JSON/YAML"

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
      local r_fips_commit r_fips_branch

      r_scenario="$(json_string "$meta_json" scenario || true)"
      r_timestamp="$(json_string "$meta_json" timestamp || true)"
      r_duration="$(json_number "$meta_json" duration_secs || true)"

      r_scenario="${r_scenario:-unknown}"
      r_duration="${r_duration:-0}"

      r_fips_commit="$(json_nested_string "$meta_json" fips_git commit || true)"
      r_fips_commit="${r_fips_commit:-$(json_string "$meta_json" commit || true)}"
      r_fips_branch="$(json_nested_string "$meta_json" fips_git branch || true)"

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

      echo "${sort_ts}|${hash_name}|${ts_name}|${r_scenario}|${r_timestamp}|${r_duration}|${r_verdict}|${r_assert_total}|${r_assert_passed}|${r_fips_commit}|${r_fips_branch}" >> "$runs_file"
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

  # ── Begin HTML output ──────────────────────────────────────────────

  cat > "$dash_file" <<'DASHHEAD'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FIPS Lab Test Reports</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:#f0f2f5;color:#1a1a2e;line-height:1.5}
.header{background:linear-gradient(135deg,#0a1628,#162447,#1f4068);color:#fff;padding:1.5rem 2rem;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 8px rgba(0,0,0,.2)}
.header h1{font-size:1.5rem;font-weight:600;letter-spacing:.5px}
.header .updated{font-size:.85rem;opacity:.8}
.container{max-width:1100px;margin:2rem auto;padding:0 1rem}
.summary{display:flex;gap:1rem;margin-bottom:2rem;flex-wrap:wrap}
.summary-card{background:#fff;border-radius:8px;padding:1rem 1.5rem;flex:1;min-width:160px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
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
.chart-item{flex:1;min-width:260px;max-width:420px}
.chart-item img{width:100%;height:auto}
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
</style>
</head>
<body>
<div class="header">
<h1>FIPS Lab Test Reports</h1>
<div class="updated">Last updated: PLACEHOLDER_LAST_UPDATED</div>
</div>
<div class="container">
<div class="summary">
<div class="summary-card"><div class="label">Total Runs</div><div class="value">PLACEHOLDER_TOTAL_RUNS</div></div>
<div class="summary-card"><div class="label">Commits Tested</div><div class="value">PLACEHOLDER_TOTAL_COMMITS</div></div>
<div class="summary-card"><div class="label">Pass Rate</div><div class="value">PLACEHOLDER_PASS_RATE</div></div>
</div>
DASHHEAD

  # ── Emit commit groups ─────────────────────────────────────────────

  if [ -f "$runs_file" ] && [ -s "$runs_file" ]; then
    local commits_file
    commits_file="$(mktemp)"
    cut -d'|' -f1,2 "$runs_file" | awk -F'|' '{print $2 "|" $1}' | sort -t'|' -k2 -r | awk -F'|' '{if(!seen[$1]++){print $1}}' > "$commits_file"

    while IFS= read -r hash_name; do
      [ -z "$hash_name" ] && continue
      local short_hash="${hash_name:0:12}"
      local commit_url="https://github.com/Amperstrand/fips/commit/${hash_name}"

      local first_line
      first_line="$(grep "|${hash_name}|" "$runs_file" | sort -t'|' -k1 -r | head -1)"
      local group_branch
      group_branch="$(echo "$first_line" | cut -d'|' -f11)"

      echo '<div class="commit-group">' >> "$dash_file"
      echo '<div class="commit-header">' >> "$dash_file"
      echo "<span class=\"hash\"><a href=\"${commit_url}\">${hash_name}</a></span>" >> "$dash_file"
      if [ -n "$group_branch" ]; then
        echo "<span class=\"branch-badge\">${group_branch}</span>" >> "$dash_file"
      fi
      echo '</div>' >> "$dash_file"

      # Table header for runs
      echo '<div class="run-header"><span>Timestamp</span><span>Scenario</span><span>Duration</span><span>Verdict</span><span>Assertions</span><span>Details</span></div>' >> "$dash_file"
      echo '<div class="runs">' >> "$dash_file"

      grep "|${hash_name}|" "$runs_file" | sort -t'|' -k1 -r | while IFS='|' read -r _ _ ts_name r_scenario r_timestamp r_duration r_verdict r_assert_total r_assert_passed r_fips_commit r_fips_branch; do
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

        # Link to analysis.md if it exists, otherwise to the directory listing
        local report_path="reports/${hash_name}/${ts_name}/"
        local report_label="Browse"
        if [ -f "$reports_dir/${hash_name}/${ts_name}/analysis.md" ]; then
          report_path="reports/${hash_name}/${ts_name}/analysis.md"
          report_label="Analysis"
        elif [ -f "$reports_dir/${hash_name}/${ts_name}/analysis.txt" ]; then
          report_path="reports/${hash_name}/${ts_name}/analysis.txt"
          report_label="Analysis"
        fi

        echo "<div class=\"run-row\">" >> "$dash_file"
        echo "<span class=\"run-time\">${display_time}</span>" >> "$dash_file"
        echo "<span class=\"run-scenario\"><strong>${r_scenario}</strong></span>" >> "$dash_file"
        echo "<span>${duration_display}</span>" >> "$dash_file"
        echo "<span class=\"verdict ${verdict_class}\">${verdict_display}</span>" >> "$dash_file"
        echo "<span class=\"assertions\">${assertions_display}</span>" >> "$dash_file"
        echo "<span class=\"run-link\"><a href=\"${report_path}\">${report_label}</a></span>" >> "$dash_file"
        echo "</div>" >> "$dash_file"

        local chart_rtt="$reports_dir/${hash_name}/${ts_name}/chart-rtt.svg"
        local chart_peers="$reports_dir/${hash_name}/${ts_name}/chart-peers.svg"
        local chart_rekeys="$reports_dir/${hash_name}/${ts_name}/chart-rekeys.svg"
        local has_charts=false
        [ -f "$chart_rtt" ] || [ -f "$chart_peers" ] || [ -f "$chart_rekeys" ] && has_charts=true

        if $has_charts; then
          echo '<div class="chart-row">' >> "$dash_file"
          if [ -f "$chart_rtt" ]; then
            echo "<div class=\"chart-item\"><img src=\"reports/${hash_name}/${ts_name}/chart-rtt.svg\" alt=\"RTT Chart\" loading=\"lazy\"/></div>" >> "$dash_file"
          fi
          if [ -f "$chart_peers" ]; then
            echo "<div class=\"chart-item\"><img src=\"reports/${hash_name}/${ts_name}/chart-peers.svg\" alt=\"Peer Count Chart\" loading=\"lazy\"/></div>" >> "$dash_file"
          fi
          if [ -f "$chart_rekeys" ]; then
            echo "<div class=\"chart-item\"><img src=\"reports/${hash_name}/${ts_name}/chart-rekeys.svg\" alt=\"Rekey Events Chart\" loading=\"lazy\"/></div>" >> "$dash_file"
          fi
          echo '</div>' >> "$dash_file"
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
</body>
</html>
DASHFOOT

  # ── Replace placeholders ───────────────────────────────────────────

  sed -i.bak "s|PLACEHOLDER_LAST_UPDATED|${last_updated_display}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_TOTAL_RUNS|${total_runs}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_TOTAL_COMMITS|${total_commits}|g" "$dash_file"
  sed -i.bak "s|PLACEHOLDER_PASS_RATE|${pass_rate}|g" "$dash_file"
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
