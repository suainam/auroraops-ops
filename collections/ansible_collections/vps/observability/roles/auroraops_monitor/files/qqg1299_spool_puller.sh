#!/bin/bash
set -euo pipefail
# AuroraOps qqg1299 Bounded Spool Puller Tick
PULLER_KEY="${HOME}/.auroraops/keys/qqg1299_monitor_puller_ed25519"
ARGS=(
    "--ssh-target" "qqg1299"
    "--remote-dir" "/var/lib/auroraops-monitor"
    "--pull-only"
    "--seal"
)
if [ -f "$PULLER_KEY" ]; then
    ARGS+=("--ssh-identity" "$PULLER_KEY")
fi

exec python3 "${HOME}/.auroraops/bin/instabox_scheduler_tick.py" "${ARGS[@]}"
