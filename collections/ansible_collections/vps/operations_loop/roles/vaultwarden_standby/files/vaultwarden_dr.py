#!/usr/bin/env python3
"""Operate the manual Vaultwarden disaster-recovery state machine on the Pi."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import vaultwarden_standby


class DisasterRecoveryError(RuntimeError):
    """Raised when a DR transition cannot complete safely."""


@dataclass(frozen=True)
class Config:
    root: Path
    standby_root: Path
    data_dir: Path
    container: str
    sync_timer: str
    cloudflared_ready_url: str
    primary_health_url: str
    primary_ssh_host: str
    primary_ssh_port: int
    local_health_url: str
    public_health_url: str
    max_snapshot_age_seconds: int
    health_failure_threshold: int
    health_timeout_seconds: int
    notifier_path: Path
    notifier_python: str
    hostname: str
    snapshot_only: bool = False
    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def mode_file(self) -> Path:
        return self.state_dir / "mode.json"

    @property
    def health_state_file(self) -> Path:
        return self.state_dir / "health.json"

    @property
    def operation_file(self) -> Path:
        return self.state_dir / "operation.json"

    @property
    def lock_file(self) -> Path:
        return self.root / ".promote.lock"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging-data"

    @property
    def rollback_dir(self) -> Path:
        return self.root / "rollback-data"

    @classmethod
    def from_env(cls) -> "Config":
        max_age = int(os.environ.get("VAULTWARDEN_DR_MAX_SNAPSHOT_AGE_SECONDS", "900"))
        threshold = int(os.environ.get("VAULTWARDEN_DR_HEALTH_FAILURE_THRESHOLD", "5"))
        timeout = int(os.environ.get("VAULTWARDEN_DR_HEALTH_TIMEOUT_SECONDS", "10"))
        if max_age < 1 or threshold < 1 or timeout < 1:
            raise DisasterRecoveryError("DR age, threshold, and timeout values must be positive")
        root = Path(os.environ.get("VAULTWARDEN_DR_ROOT", "/opt/vaultwarden-dr"))
        standby_root = Path(
            os.environ.get("VAULTWARDEN_STANDBY_ROOT", "/opt/vaultwarden-standby")
        )
        snapshot_only_env = os.environ.get("VAULTWARDEN_STANDBY_SNAPSHOT_ONLY", "")
        mode_env = os.environ.get("VAULTWARDEN_STANDBY_MODE", "")
        snapshot_only = (
            snapshot_only_env.lower() in ("true", "1", "yes")
            or mode_env == "snapshot-only"
        )
        if not snapshot_only:
            mode_file = root / "state" / "mode.json"
            if mode_file.is_file():
                try:
                    payload = json.loads(mode_file.read_text(encoding="utf-8"))
                    if payload.get("snapshot_only") is True or payload.get("mode") == "snapshot-only":
                        snapshot_only = True
                except Exception:
                    pass
            if not snapshot_only and (standby_root / "latest.json").is_file():
                try:
                    latest = json.loads((standby_root / "latest.json").read_text(encoding="utf-8"))
                    if latest.get("snapshot_only") is True or latest.get("mode") == "snapshot-only":
                        snapshot_only = True
                except Exception:
                    pass
        return cls(
            root=root,
            standby_root=standby_root,
            data_dir=Path(
                os.environ.get(
                    "VAULTWARDEN_DR_DATA_DIR",
                    "/opt/dockers/vaultwarden/data",
                )
            ),
            container=os.environ.get("VAULTWARDEN_STANDBY_CONTAINER", "vaultwarden"),
            sync_timer=os.environ.get(
                "VAULTWARDEN_DR_SYNC_TIMER",
                "vaultwarden-standby-sync.timer",
            ),
            cloudflared_ready_url=os.environ.get(
                "VAULTWARDEN_DR_TUNNEL_READY_URL",
                "http://127.0.0.1:20241/ready",
            ),
            primary_health_url=os.environ.get(
                "VAULTWARDEN_DR_PRIMARY_HEALTH_URL",
                "https://bws.suai.eu.org/alive",
            ),
            primary_ssh_host=os.environ.get(
                "VAULTWARDEN_DR_PRIMARY_SSH_HOST",
                "10.147.20.129",
            ),
            primary_ssh_port=int(
                os.environ.get("VAULTWARDEN_DR_PRIMARY_SSH_PORT", "6868")
            ),
            local_health_url=os.environ.get(
                "VAULTWARDEN_DR_LOCAL_HEALTH_URL",
                "http://127.0.0.1:30007/alive",
            ),
            public_health_url=os.environ.get(
                "VAULTWARDEN_DR_PUBLIC_HEALTH_URL",
                "https://bws.msuai.top/alive",
            ),
            max_snapshot_age_seconds=max_age,
            health_failure_threshold=threshold,
            health_timeout_seconds=timeout,
            notifier_path=Path(
                os.environ.get(
                    "VAULTWARDEN_DR_NOTIFIER_PATH",
                    "/usr/local/bin/notifier.py",
                )
            ),
            notifier_python=os.environ.get(
                "VAULTWARDEN_DR_NOTIFIER_PYTHON",
                "/opt/aurora_venv/bin/python3",
            ),
            hostname=os.environ.get("VAULTWARDEN_DR_HOSTNAME", socket.gethostname()),
            snapshot_only=snapshot_only,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    vaultwarden_standby.atomic_json(path, payload)


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        return vaultwarden_standby.load_json(path, label)
    except vaultwarden_standby.StandbyError as exc:
        raise DisasterRecoveryError(str(exc)) from exc


def http_status(url: str, timeout: int) -> int | None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1024)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def tcp_reachable(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise DisasterRecoveryError(f"command failed ({completed.returncode}): {' '.join(command)}: {detail}")
    return completed


def current_mode(config: Config) -> dict[str, Any]:
    if config.snapshot_only:
        return {
            "mode": "snapshot-only",
            "snapshot_only": True,
            "promotion_supported": False,
            "message": "Snapshot-only receiver mode; local container promotion is not supported",
        }
    if not config.mode_file.is_file():
        return {"mode": "passive"}
    try:
        payload = load_json(config.mode_file, "DR mode")
    except DisasterRecoveryError:
        return {"mode": "invalid"}
    if payload.get("mode") not in {"passive", "preparing", "active", "failed", "snapshot-only"}:
        return {"mode": "invalid"}
    return payload


def standby_config(config: Config) -> vaultwarden_standby.Config:
    env_cfg = vaultwarden_standby.Config.from_env()
    if config.snapshot_only != env_cfg.snapshot_only or config.standby_root != env_cfg.root:
        return vaultwarden_standby.Config(
            root=config.standby_root,
            source=env_cfg.source,
            source_port=env_cfg.source_port,
            ssh_key=env_cfg.ssh_key,
            known_hosts=env_cfg.known_hosts,
            retention=env_cfg.retention,
            container=config.container,
            mode="snapshot-only" if config.snapshot_only else env_cfg.mode,
            snapshot_only=config.snapshot_only,
        )
    return env_cfg

def latest_snapshot(config: Config) -> tuple[str | None, int | None, str]:
    standby = standby_config(config)
    if not standby.latest.is_file():
        return None, None, "missing"
    try:
        latest = load_json(standby.latest, "standby latest.json")
        snapshot_id = vaultwarden_standby.safe_reference(str(latest.get("snapshot_id", "")))
        snapshot = standby.snapshots / snapshot_id
        vaultwarden_standby.validate_snapshot(snapshot, latest.get("manifest_sha256"))
        age = vaultwarden_standby.snapshot_age(standby, snapshot_id)
        return snapshot_id, age, "valid"
    except (DisasterRecoveryError, vaultwarden_standby.StandbyError, OSError) as exc:
        return None, None, f"invalid: {exc}"


def docker_state(config: Config) -> tuple[str, str]:
    return vaultwarden_standby.container_status(standby_config(config))


def timer_state(config: Config) -> str:
    completed = run(["systemctl", "is-active", config.sync_timer], check=False)
    value = completed.stdout.strip()
    return value or "unknown"


def status(config: Config) -> dict[str, Any]:
    mode = current_mode(config)
    snapshot_id, age, snapshot_status = latest_snapshot(config)
    container_status, restart_policy = docker_state(config)
    return {
        "mode": "snapshot-only" if config.snapshot_only else mode.get("mode", "invalid"),
        "mode_details": mode,
        "primary_health_url": config.primary_health_url,
        "primary_health_status": http_status(
            config.primary_health_url,
            config.health_timeout_seconds,
        ),
        "primary_ssh_reachable": tcp_reachable(
            config.primary_ssh_host,
            config.primary_ssh_port,
            config.health_timeout_seconds,
        ),
        "container_name": config.container,
        "container_status": container_status,
        "container_restart_policy": restart_policy,
        "tunnel_ready_status": http_status(
            config.cloudflared_ready_url,
            config.health_timeout_seconds,
        ),
        "sync_timer_status": timer_state(config),
        "latest_snapshot_id": snapshot_id,
        "latest_snapshot_status": snapshot_status,
        "snapshot_age_seconds": age,
        "max_snapshot_age_seconds": config.max_snapshot_age_seconds,
        "local_health_url": config.local_health_url,
        "public_health_url": config.public_health_url,
        "snapshot_only": config.snapshot_only,
    }

def notification_payload(title: str, content: str, *, level: str = "error") -> dict[str, Any]:
    return {
        "title": title,
        "description": "Vaultwarden disaster recovery",
        "content": content,
        "status": level,
        "source": "vaultwarden-dr",
    }


def send_notification(config: Config, message: str, *, status: str = "error") -> bool:
    if not config.notifier_path.is_file():
        return False
    payload = [
        {
            "host": config.hostname,
            "service": "vaultwarden_dr",
            "status": status,
            "message": message,
            "timestamp": utc_text(),
        }
    ]
    try:
        completed = subprocess.run(
            [config.notifier_python, str(config.notifier_path)],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def health_check(config: Config) -> dict[str, Any]:
    if config.snapshot_only:
        raise DisasterRecoveryError(
            "DR health-check is disabled in snapshot-only mode; local promotion is not supported"
        )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    status_code = http_status(config.primary_health_url, config.health_timeout_seconds)
    previous: dict[str, Any] = {}
    if config.health_state_file.is_file():
        try:
            previous = load_json(config.health_state_file, "DR health state")
        except DisasterRecoveryError:
            previous = {}
    previous_failures = int(previous.get("consecutive_failures", 0) or 0)
    failures = 0 if status_code == 200 else previous_failures + 1
    alerted = bool(previous.get("alerted", False))
    should_alert = failures >= config.health_failure_threshold and not alerted
    payload = {
        "checked_at": utc_text(),
        "health_url": config.primary_health_url,
        "status_code": status_code,
        "consecutive_failures": failures,
        "threshold": config.health_failure_threshold,
        "alerted": alerted or should_alert,
        "automatic_action_taken": False,
    }
    atomic_json(config.health_state_file, payload)
    result: dict[str, Any] = {"status": "ok" if status_code == 200 else "failed", **payload}
    if should_alert:
        message = (
            f"{config.primary_health_url} 已连续失败 {failures} 次；"
            "系统未自动启动 Pi、恢复数据或修改 DNS。"
        )
        result["notification"] = notification_payload(
            "Vaultwarden 主节点连续健康检查失败",
            message,
        )
        result["notification_sent"] = send_notification(config, message)
    return result


def require_snapshot(config: Config, *, allow_stale: bool) -> tuple[str, Path, int]:
    snapshot_id, age, snapshot_status = latest_snapshot(config)
    if snapshot_status != "valid" or not snapshot_id or age is None:
        raise DisasterRecoveryError(f"latest snapshot is not valid: {snapshot_status}")
    if age > config.max_snapshot_age_seconds and not allow_stale:
        raise DisasterRecoveryError(
            f"latest snapshot is stale: {age}s exceeds {config.max_snapshot_age_seconds}s"
        )
    snapshot = standby_config(config).snapshots / snapshot_id
    return snapshot_id, snapshot, age


def ensure_passive_container(config: Config) -> None:
    container_status, restart_policy = docker_state(config)
    if container_status == "running":
        raise DisasterRecoveryError("Pi Vaultwarden is already running")
    if container_status == "missing":
        raise DisasterRecoveryError("Pi Vaultwarden container does not exist")
    if restart_policy not in {"", "no"}:
        raise DisasterRecoveryError("Pi Vaultwarden restart policy must remain disabled")


def pause_sync(config: Config) -> None:
    run(["systemctl", "stop", config.sync_timer])


def resume_sync(config: Config) -> None:
    run(["systemctl", "start", config.sync_timer])


def copy_snapshot_to_staging(snapshot: Path, staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(snapshot, staging, symlinks=False)
    for name in ("db.sqlite3-wal", "db.sqlite3-shm"):
        (staging / name).unlink(missing_ok=True)
    vaultwarden_standby.sqlite_quick_check(staging / "db.sqlite3")


def replace_data(config: Config) -> None:
    if config.rollback_dir.exists():
        raise DisasterRecoveryError(
            f"rollback directory already exists: {config.rollback_dir}; resolve the previous operation first"
        )
    config.data_dir.parent.mkdir(parents=True, exist_ok=True)
    if config.data_dir.exists():
        os.replace(config.data_dir, config.rollback_dir)
    try:
        os.replace(config.staging_dir, config.data_dir)
    except Exception:
        if config.rollback_dir.exists() and not config.data_dir.exists():
            os.replace(config.rollback_dir, config.data_dir)
        raise


def prepare(
    config: Config,
    *,
    primary_isolated: bool,
    dangerous_allow_split_brain: bool,
    allow_stale_snapshot: bool,
) -> dict[str, Any]:
    if config.snapshot_only:
        raise DisasterRecoveryError(
            "snapshot-only mode does not support local container promotion; external restore is required"
        )
    config.root.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with config.lock_file.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DisasterRecoveryError("another Vaultwarden promotion is running") from exc

        mode = current_mode(config).get("mode")
        if mode == "active":
            raise DisasterRecoveryError("Vaultwarden DR mode is already active")
        primary_status = http_status(config.primary_health_url, config.health_timeout_seconds)
        if primary_status == 200:
            raise DisasterRecoveryError("primary health endpoint is healthy; refusing promotion")
        if not primary_isolated and not dangerous_allow_split_brain:
            raise DisasterRecoveryError(
                "primary isolation is not verified; use --dangerous-allow-split-brain only after explicit operator review"
            )

        ensure_passive_container(config)
        snapshot_id, snapshot, age = require_snapshot(
            config,
            allow_stale=allow_stale_snapshot,
        )
        atomic_json(
            config.mode_file,
            {
                "mode": "preparing",
                "started_at": utc_text(),
                "snapshot_id": snapshot_id,
                "snapshot_age_seconds": age,
                "primary_isolated": primary_isolated,
                "split_brain_risk": not primary_isolated,
            },
        )
        try:
            pause_sync(config)
            copy_snapshot_to_staging(snapshot, config.staging_dir)
            replace_data(config)
            run(["docker", "start", config.container])
            local_status: int | None = None
            for _ in range(12):
                local_status = http_status(
                    config.local_health_url,
                    config.health_timeout_seconds,
                )
                if local_status == 200:
                    break
                time.sleep(5)
            if local_status != 200:
                raise DisasterRecoveryError(
                    f"Pi Vaultwarden local health check failed with status {local_status}"
                )
            operation = {
                "result": "prepared",
                "prepared_at": utc_text(),
                "snapshot_id": snapshot_id,
                "snapshot_age_seconds": age,
                "primary_isolated": primary_isolated,
                "split_brain_risk": not primary_isolated,
                "local_health_status": local_status,
                "dns_changed": False,
            }
            atomic_json(config.operation_file, operation)
            return operation
        except Exception as exc:
            rollback_local(config, reason=str(exc), resume_timer=True)
            raise


def mark_active(config: Config, *, dns_target: str, public_status: int) -> dict[str, Any]:
    if config.snapshot_only:
        raise DisasterRecoveryError(
            "snapshot-only mode does not support local container promotion; external restore is required"
        )
    operation = load_json(config.operation_file, "DR operation")
    if operation.get("result") != "prepared":
        raise DisasterRecoveryError("DR operation is not prepared")
    if public_status != 200:
        raise DisasterRecoveryError("public health verification is not successful")
    payload = {
        **operation,
        "result": "active",
        "activated_at": utc_text(),
        "dns_target": dns_target,
        "public_health_status": public_status,
        "dns_changed": True,
    }
    atomic_json(config.operation_file, payload)
    atomic_json(
        config.mode_file,
        {
            "mode": "active",
            "activated_at": payload["activated_at"],
            "snapshot_id": operation.get("snapshot_id"),
            "dns_target": dns_target,
            "split_brain_risk": operation.get("split_brain_risk", False),
        },
    )
    message = f"Pi 已使用快照 {operation.get('snapshot_id')} 接管，DNS 目标为 {dns_target}。"
    payload["notification"] = notification_payload(
        "Vaultwarden 已人工接管",
        message,
        level="success",
    )
    payload["notification_sent"] = send_notification(
        config,
        message,
        status="success",
    )
    return payload


def rollback_local(config: Config, *, reason: str, resume_timer: bool) -> dict[str, Any]:
    if config.snapshot_only:
        raise DisasterRecoveryError(
            "snapshot-only mode does not support local container rollback"
        )
    run(["docker", "stop", config.container], check=False)
    shutil.rmtree(config.staging_dir, ignore_errors=True)
    if config.rollback_dir.exists():
        if config.data_dir.exists():
            shutil.rmtree(config.data_dir)
        os.replace(config.rollback_dir, config.data_dir)
    if resume_timer:
        resume_sync(config)
    payload = {
        "result": "rolled_back",
        "rolled_back_at": utc_text(),
        "reason": reason,
        "container_stopped": True,
        "sync_resumed": resume_timer,
    }
    atomic_json(config.operation_file, payload)
    atomic_json(config.mode_file, {"mode": "passive", "updated_at": utc_text()})
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show the manual DR status as JSON")
    subparsers.add_parser("health-check", help="check the primary without automatic mutation")

    prepare_parser = subparsers.add_parser(
        "prepare",
        help="restore the latest snapshot and start Pi Vaultwarden without changing DNS",
    )
    prepare_parser.add_argument("--primary-isolated", action="store_true")
    prepare_parser.add_argument("--dangerous-allow-split-brain", action="store_true")
    prepare_parser.add_argument("--allow-stale-snapshot", action="store_true")

    active_parser = subparsers.add_parser(
        "mark-active",
        help="mark a prepared operation active after controller DNS verification",
    )
    active_parser.add_argument("--dns-target", required=True)
    active_parser.add_argument("--public-status", required=True, type=int)

    rollback_parser = subparsers.add_parser("rollback-local", help="stop Pi and restore local passive data")
    rollback_parser.add_argument("--reason", required=True)
    rollback_parser.add_argument("--resume-timer", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = Config.from_env()
        if args.command == "status":
            payload = status(config)
        elif args.command == "health-check":
            payload = health_check(config)
        elif args.command == "prepare":
            payload = prepare(
                config,
                primary_isolated=args.primary_isolated,
                dangerous_allow_split_brain=args.dangerous_allow_split_brain,
                allow_stale_snapshot=args.allow_stale_snapshot,
            )
        elif args.command == "mark-active":
            payload = mark_active(
                config,
                dns_target=args.dns_target,
                public_status=args.public_status,
            )
        elif args.command == "rollback-local":
            payload = rollback_local(
                config,
                reason=args.reason,
                resume_timer=args.resume_timer,
            )
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        DisasterRecoveryError,
        vaultwarden_standby.StandbyError,
        OSError,
        ValueError,
    ) as exc:
        print(f"vaultwarden_dr: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
