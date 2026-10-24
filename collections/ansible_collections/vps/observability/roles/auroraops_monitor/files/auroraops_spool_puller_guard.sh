#!/bin/bash
set -euo pipefail
# Forced command guard for AuroraOps Monitor Puller (Restricted Spool Access Only)
SSH_ORIGINAL_COMMAND="${SSH_ORIGINAL_COMMAND:-}"

if [ -z "$SSH_ORIGINAL_COMMAND" ]; then
    echo "Access denied: interactive shell not permitted" >&2
    exit 1
fi

case "$SSH_ORIGINAL_COMMAND" in
    "true")
        exit 0
        ;;
    *"spool_manager.py"*seal*)
        exec python3 /var/lib/auroraops-monitor/spool_manager.py --base-dir /var/lib/auroraops-monitor seal
        ;;
    *"spool_manager.py"*list*)
        exec python3 /var/lib/auroraops-monitor/spool_manager.py --base-dir /var/lib/auroraops-monitor list
        ;;
    *"spool_manager.py"*ack*)
        # Only allow ack with valid filename pattern and sha256
        if [[ "$SSH_ORIGINAL_COMMAND" =~ ^python3\ /var/lib/auroraops-monitor/spool_manager\.py(\ --base-dir\ /var/lib/auroraops-monitor)?\ ack\ (spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl)\ --sha256\ ([0-9a-fA-F]{64})$ ]]; then
            fname="${BASH_REMATCH[2]}"
            sha="${BASH_REMATCH[4]}"
            exec python3 /var/lib/auroraops-monitor/spool_manager.py --base-dir /var/lib/auroraops-monitor ack "$fname" --sha256 "$sha"
        else
            echo "Access denied: invalid ack arguments" >&2
            exit 1
        fi
        ;;
    scp\ *"-f"*)
        eval "cmd_args=($SSH_ORIGINAL_COMMAND)"
        target="${cmd_args[-1]}"
        if [[ "$target" =~ ^/var/lib/auroraops-monitor/spool/spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl$ ]]; then
            exec $SSH_ORIGINAL_COMMAND
        else
            echo "Access denied: SCP path not permitted: '$target'" >&2
            exit 1
        fi
        ;;
    *)
        echo "Access denied: unauthorized command '$SSH_ORIGINAL_COMMAND'" >&2
        exit 1
        ;;
esac
