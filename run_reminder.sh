#!/bin/bash
# Wrapper the LaunchAgent (com.mccia.ramp-login-reminder.plist) calls every
# Monday at 10:50 — ten minutes before com.mccia.ramp-weekly-report.plist
# opens the RAMP login window.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p output
source venv/bin/activate
python login_reminder.py >> output/login_reminder_output.log 2>&1
