#!/usr/bin/env python3
"""Stage-1 runner: execute the official xykt/ipquality script for one exit.

Kept deliberately thin. The scoring itself belongs to the upstream tool; this
wrapper only:

  * strips ambient proxy variables so an ignored `-x` cannot misreport the
    control plane's address as the measured exit;
  * runs the script in privacy mode so the report is not uploaded off-box;
  * hands the raw output to the analyzer, which owns all interpretation.

Exits emit one JSON object on stdout. Credentials arrive through the
environment, so they never appear in argv or `ps`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# Upstream markers the analyzer also knows about; kept here only so the runner
# can explain *why* a run failed without opening the analyzer.
MARKER_HINT = "www.google.cn"


def clean_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in PROXY_KEYS}
    env.update(extra)
    return env


def has_json_document(raw: str) -> bool:
    """True only when the output actually contains a JSON report.

    The upstream script always prints cursor-control padding first, so a
    non-empty stdout proves nothing. On a host missing `jq` it prints padding
    and then dies, which previously looked like a successful observation.
    """
    if not raw:
        return False
    start = raw.find("{")
    if start < 0:
        return False
    try:
        json.loads(strip_ansi(raw[start:]))
    except ValueError:
        return False
    return True


def build_socks_url(env: dict[str, str]) -> str:
    server = env.get("IPQUALITY_SOCKS5_SERVER", "").strip()
    port = env.get("IPQUALITY_SOCKS5_PORT", "").strip()
    if not server or not port:
        return ""
    user = env.get("IPQUALITY_SOCKS5_USER", "")
    password = env.get("IPQUALITY_SOCKS5_PASS", "")
    auth = f"{user}:{password}@" if user else ""
    # socks5h keeps DNS resolution on the exit side, which is what makes the
    # measurement reflect the exit rather than the runner.
    return f"socks5h://{auth}{server}:{port}"


def strip_ansi(raw: str) -> str:
    """Local mirror of the analyzer's cleaner, to avoid an import cycle."""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
    text = re.sub(r"\x1b\][0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"\x1b[0-9;]*[A-Za-z]", "", text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x1b]", "", text)


def run_upstream(script: str, args: list[str], socks_url: str,
                 offline_payload: str) -> tuple[str, str]:
    """Return (raw_output, error). Offline mode replays a stored payload."""
    if offline_payload:
        # Recorded upstream output carries its own colour codes, so validate it
        # with the same tolerant parser the analyzer uses rather than a strict
        # json.loads that would reject a perfectly good capture.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ipquality_analyzer import parse_upstream_json
        if parse_upstream_json(offline_payload) is None:
            return "", "offline payload is not parseable as an ipquality report"
        return offline_payload, ""

    # In offline mode the caller passes an empty path (no temp dir was created),
    # which arrives here as a bare "/ipquality.sh" from the Ansible path join.
    # That is "no script", not "a script at the filesystem root".
    if not script.strip() or script.strip() == "/ipquality.sh":
        return "", "no upstream script configured and no offline payload supplied"
    if not Path(script).is_file():
        return "", f"upstream script not present: {script}"

    # jq is a hard dependency of the upstream script: it parses every database
    # response through `jq`. Without it the script dies partway and emits only
    # cursor-control padding, which would otherwise be accepted below as a
    # successful -- if empty -- observation. Checked up front so the report says
    # exactly why, instead of recording a vacuous pass.
    if not any(Path(directory).is_dir() and (Path(directory) / "jq").is_file()
               for directory in os.environ.get("PATH", "").split(os.pathsep) if directory):
        return "", ("upstream ipquality requires `jq`, which is not installed; "
                    "the scoring stage cannot run on this host")

    argv = ["bash", script, *args]
    if socks_url:
        # The upstream script builds `-x` from a single argument, so the URL is
        # passed as one element and never re-split by a shell.
        argv += ["-x", socks_url]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=1800,
            env=clean_env({}), cwd=tempfile.gettempdir(),
        )
    except subprocess.TimeoutExpired:
        return "", "upstream script timed out"
    except OSError as error:
        return "", f"upstream script could not be started: {error}"

    # A non-zero exit, or output that carries no JSON document at all, is a
    # failed observation. ANSI padding alone must never be reported as success.
    if not has_json_document(completed.stdout):
        detail = (completed.stderr or completed.stdout).strip()
        # Keep the tail: it names the missing dependency or the failing step.
        return "", (f"upstream produced no JSON report (rc={completed.returncode}): "
                    f"{detail[-200:]}")
    return completed.stdout, ""


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "exit"
    # The observation is written to a file rather than stdout: the upstream
    # report is large and carries raw escape sequences, and relaying that
    # through Ansible's stdout is both fragile and needlessly exposed in logs.
    output_path = sys.argv[2] if len(sys.argv) > 2 else ""

    env = dict(os.environ)
    socks_url = build_socks_url(env)
    args = env.get("IPQUALITY_SCRIPT_ARGS", "-j -p -n -4 -E").split()
    offline_payload = env.get("IPQUALITY_OFFLINE_PAYLOAD", "")
    script = env.get("IPQUALITY_SCRIPT", "")

    raw, error = run_upstream(script, args, socks_url, offline_payload)
    observation = {"exit_name": name, "ok": not bool(error), "via_socks5": bool(socks_url),
                   "raw": raw, "error": error}
    payload = json.dumps(observation, ensure_ascii=False)

    if output_path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    # A failed observation is recorded data, not a task failure; the pipeline
    # decides whether a missing exit is a gate violation.
    return 0


if __name__ == "__main__":
    sys.exit(main())
