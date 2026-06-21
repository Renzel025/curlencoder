#!/bin/bash
# Wrapper for cron: sources the env file, then runs the encoder monitor.
# Cron has a bare environment, so the env MUST be sourced here (not relied on
# from an interactive shell). Works from any cwd — cd's to this script's dir.
#
# Crontab (weekly, Mon 07:00):
#   0 7 * * 1 /opt/curlencoder/run_monitor.sh >> /opt/curlencoder/monitor.log 2>&1
#
# Make executable once:  chmod +x /opt/curlencoder/run_monitor.sh

cd "$(dirname "$0")" || exit 1
set -a; . ./encoder_monitor.env; set +a
exec python3 encoder_monitor.py
