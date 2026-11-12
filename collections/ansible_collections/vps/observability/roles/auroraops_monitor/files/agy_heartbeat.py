#!/usr/bin/env python3
"""Keep the local Magpie agy account active without exposing credentials.

Makes a single /usage round-trip through the agy binary inside the magpie
container. A successful reply refreshes the OAuth access-token window while the
refresh token is still valid. Exits non-zero on any failure so systemd can log
it; prints no secrets and no arbitrary provider output.
"""
import json
import subprocess
import sys


def main():
    try:
        result = subprocess.run(
            ["docker", "exec", "magpie", "agy", "-p", "/usage", "--output-format", "json", "--print-timeout", "35s"],
            capture_output=True, text=True, timeout=50, check=False,
        )
        if result.returncode:
            print("agy heartbeat failed: command returned nonzero", file=sys.stderr)
            return 1
        data = json.loads(result.stdout)
        if data.get("status") != "SUCCESS" or not data.get("response", "").strip():
            print("agy heartbeat failed: no successful usage response", file=sys.stderr)
            return 1
        return 0
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
        print("agy heartbeat failed: unavailable or invalid response", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
