#!/usr/bin/env python3
"""Recover GitHub Actions workflows disabled by repository inactivity."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request


class WatchdogError(RuntimeError):
    """Expected watchdog failure safe to show without credentials."""


@dataclass(frozen=True)
class Target:
    repository: str
    workflow: str
    ref: str


class GitHubClient:
    def __init__(self, api_url: str, token: str, timeout: int = 30) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "AuroraOps-GitHub-Actions-Watchdog",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        req = request.Request(
            f"{self.api_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            raise WatchdogError(f"GitHub API {method} {path} returned HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise WatchdogError(f"GitHub API {method} {path} failed: {exc.reason}") from exc
        return json.loads(raw) if raw else None

    @staticmethod
    def workflow_path(target: Target) -> str:
        owner, repository = target.repository.split("/", 1)
        return (
            f"/repos/{parse.quote(owner, safe='')}/{parse.quote(repository, safe='')}"
            f"/actions/workflows/{parse.quote(target.workflow, safe='')}"
        )

    def state(self, target: Target) -> str:
        result = self.call("GET", self.workflow_path(target))
        state = result.get("state") if isinstance(result, dict) else None
        if not isinstance(state, str) or not state:
            raise WatchdogError(f"{target.repository}/{target.workflow}: missing workflow state")
        return state

    def enable(self, target: Target) -> None:
        self.call("PUT", f"{self.workflow_path(target)}/enable")

    def dispatch(self, target: Target) -> None:
        self.call("POST", f"{self.workflow_path(target)}/dispatches", {"ref": target.ref})


def load_targets(raw: str) -> list[Target]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WatchdogError("GITHUB_ACTIONS_WATCHDOG_TARGETS is not valid JSON") from exc
    if not isinstance(values, list) or not values:
        raise WatchdogError("GITHUB_ACTIONS_WATCHDOG_TARGETS must be a non-empty list")
    targets: list[Target] = []
    for value in values:
        if not isinstance(value, dict):
            raise WatchdogError("each watchdog target must be an object")
        try:
            target = Target(
                repository=value["repository"], workflow=value["workflow"], ref=value["ref"]
            )
        except (KeyError, TypeError) as exc:
            raise WatchdogError("each watchdog target requires repository, workflow, and ref") from exc
        if target.repository.count("/") != 1 or not target.workflow or not target.ref:
            raise WatchdogError(f"invalid watchdog target: {target.repository}")
        targets.append(target)
    return targets


def load_configured_targets() -> list[Target]:
    raw = os.environ.get("GITHUB_ACTIONS_WATCHDOG_TARGETS", "")
    targets_file = os.environ.get("GITHUB_ACTIONS_WATCHDOG_TARGETS_FILE", "")
    if raw:
        return load_targets(raw)
    if not targets_file:
        raise WatchdogError("GITHUB_ACTIONS_WATCHDOG_TARGETS_FILE is required")
    try:
        return load_targets(Path(targets_file).read_text(encoding="utf-8"))
    except OSError as exc:
        raise WatchdogError(f"cannot read watchdog targets file: {targets_file}") from exc


def check_target(
    client: GitHubClient,
    target: Target,
    dry_run: bool,
    recover_states: set[str] | None = None,
) -> dict[str, str]:
    initial_state = client.state(target)
    result = {
        "repository": target.repository,
        "workflow": target.workflow,
        "state": initial_state,
        "action": "none",
    }
    states = recover_states or {"disabled_inactivity"}
    if initial_state not in states or dry_run:
        return result

    client.enable(target)
    enabled_state = client.state(target)
    if enabled_state != "active":
        raise WatchdogError(
            f"{target.repository}/{target.workflow}: state after enable is {enabled_state}, expected active"
        )
    client.dispatch(target)
    result["state"] = enabled_state
    result["action"] = "enabled_and_dispatched"
    return result


def notify_failure(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS_WATCHDOG_NOTIFY_FAILURE", "true").lower() != "true":
        return
    notifier = os.environ.get("GITHUB_ACTIONS_WATCHDOG_NOTIFIER", "/usr/local/bin/notifier.py")
    if not os.path.isfile(notifier):
        return
    payload = [{
        "host": os.environ.get("GITHUB_ACTIONS_WATCHDOG_HOSTNAME", "unknown"),
        "service": "github_actions_watchdog",
        "status": "error",
        "message": f"GitHub Actions watchdog failed: {message}",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }]
    try:
        subprocess.run(
            [os.environ.get("GITHUB_ACTIONS_WATCHDOG_NOTIFIER_PYTHON", sys.executable), notifier],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="query states without mutation")
    parser.add_argument(
        "--recover-state",
        action="append",
        choices=["disabled_inactivity", "disabled_manually"],
        default=None,
        help="state eligible for recovery; repeatable; defaults to disabled_inactivity",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        token = os.environ.get("GITHUB_ACTIONS_WATCHDOG_TOKEN", "")
        if not token:
            raise WatchdogError("GITHUB_ACTIONS_WATCHDOG_TOKEN is required")
        targets = load_configured_targets()
        client = GitHubClient(
            os.environ.get("GITHUB_ACTIONS_WATCHDOG_API_URL", "https://api.github.com"),
            token,
        )
        recover_states = set(args.recover_state or ["disabled_inactivity"])
        results = [
            check_target(client, target, args.dry_run, recover_states)
            for target in targets
        ]
        print(json.dumps({"status": "ok", "dry_run": args.dry_run, "targets": results}, sort_keys=True))
        return 0
    except WatchdogError as exc:
        notify_failure(str(exc))
        print(f"github-actions-watchdog: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
