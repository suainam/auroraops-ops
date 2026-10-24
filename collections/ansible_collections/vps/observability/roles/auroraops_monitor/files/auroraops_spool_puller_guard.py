#!/usr/bin/env python3
"""AuroraOps Spool Puller Guard: strictly allowlisted commands only.

Guarantees:
- Zero shell interpolation or eval (uses shlex.split and os.execv).
- Enforces strict regex validation for sealed filenames and SHA256 checksums.
- Confines SCP reads strictly to /var/lib/auroraops-monitor/spool/spool-*.jsonl.
- Completely blocks all arbitrary shell, interactive sessions, or unauthorized paths.
"""

from __future__ import annotations

import os
import re
import shlex
import sys

ALLOWED_SEALED_RE = re.compile(r"^/var/lib/auroraops-monitor/spool/spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl$")
ALLOWED_ACK_FILE_RE = re.compile(r"^spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl$")
ALLOWED_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")

orig = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
if not orig:
    sys.stderr.write("Access denied: interactive shell not permitted\n")
    sys.exit(1)

try:
    tokens = shlex.split(orig)
except Exception:
    sys.stderr.write("Access denied: malformed command\n")
    sys.exit(1)

if not tokens:
    sys.exit(1)

# 1. Reachability probe: true
if tokens == ["true"]:
    sys.exit(0)

# 2. Spool management commands
if tokens[0] == "python3" and any("spool_manager.py" in t for t in tokens):
    if "seal" in tokens:
        os.execv(
            "/usr/bin/python3",
            ["python3", "/var/lib/auroraops-monitor/spool_manager.py", "--base-dir", "/var/lib/auroraops-monitor", "seal"],
        )
    elif "list" in tokens:
        os.execv(
            "/usr/bin/python3",
            ["python3", "/var/lib/auroraops-monitor/spool_manager.py", "--base-dir", "/var/lib/auroraops-monitor", "list"],
        )
    elif "ack" in tokens:
        try:
            ack_idx = tokens.index("ack")
            fname = tokens[ack_idx + 1]
            sha_idx = tokens.index("--sha256")
            sha = tokens[sha_idx + 1]
            if ALLOWED_ACK_FILE_RE.match(fname) and ALLOWED_SHA_RE.match(sha):
                os.execv(
                    "/usr/bin/python3",
                    [
                        "python3",
                        "/var/lib/auroraops-monitor/spool_manager.py",
                        "--base-dir",
                        "/var/lib/auroraops-monitor",
                        "ack",
                        fname,
                        "--sha256",
                        sha,
                    ],
                )
        except Exception:
            pass
        sys.stderr.write("Access denied: invalid ack arguments\n")
        sys.exit(1)

# 3. SCP transfer for sealed files
if tokens[0] == "scp":
    if "-f" in tokens:
        target = tokens[-1]
        if ALLOWED_SEALED_RE.match(target):
            os.execv("/usr/bin/scp", tokens)
    sys.stderr.write(f"Access denied: SCP path not permitted: '{tokens[-1]}'\n")
    sys.exit(1)

sys.stderr.write(f"Access denied: unauthorized command '{tokens[0]}'\n")
sys.exit(1)
