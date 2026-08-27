#!/bin/bash
# Wrapper the LaunchAgent (com.mccia.ramp-weekly-report.plist) calls every
# Monday morning. Kept as a plain shell script rather than pointing launchd
# straight at the venv's python so the working directory, venv activation,
# and log redirection are all in one obvious place.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p output
source venv/bin/activate
python weekly_report.py >> output/weekly_report_output.log 2>&1
