#!/bin/bash
# Wrapper the LaunchAgent (com.mccia.ramp-login-reminder.plist) calls every
# Nightly at 22:50 IST — ten minutes before com.mccia.ramp-daily-report.plist
# opens the RAMP login window.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p output
source venv/bin/activate
python login_reminder.py >> output/login_reminder_output.log 2>&1
