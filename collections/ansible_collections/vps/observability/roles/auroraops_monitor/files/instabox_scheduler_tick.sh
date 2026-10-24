#!/usr/bin/env bash
# Thin runner for instabox_scheduler_tick.py
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/instabox_scheduler_tick.py" "$@"
