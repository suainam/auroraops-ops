#!/usr/bin/env python3
"""Synchronize and validate passive Vaultwarden recovery snapshots."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORBIDDEN_NAMES = {"db.sqlite3-wal", "db.sqlite3-shm", "vaultwarden.log"}
FORBIDDEN_PARTS = {"icon_cache", "tmp", ".rsync-partial"}


class StandbyError(RuntimeError):
    """Raised when synchronization or validation cannot complete safely."""


@dataclass(frozen=True)
class Config:
    root: Path
    source: str
    source_port: int
    ssh_key: Path
    known_hosts: Path
    retention: int
    container: str
    mode: str
    snapshot_only: bool = False
    @property
    def incoming(self) -> Path:
        return self.root / "incoming"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def latest(self) -> Path:
        return self.root / "latest.json"

    @property
    def sync_status(self) -> Path:
        return self.state / "sync-status.json"

    @classmethod
    def from_env(cls) -> "Config":
        retention = int(os.environ.get("VAULTWARDEN_STANDBY_RETENTION", "12"))
        if retention < 1:
            raise StandbyError("VAULTWARDEN_STANDBY_RETENTION must be at least 1")
        root = Path(
            os.environ.get(
                "VAULTWARDEN_STANDBY_ROOT",
                "/opt/vaultwarden-standby",
            )
        )
        snapshot_only_env = os.environ.get("VAULTWARDEN_STANDBY_SNAPSHOT_ONLY")
        mode_env = os.environ.get("VAULTWARDEN_STANDBY_MODE")
        if snapshot_only_env is not None:
            snapshot_only = snapshot_only_env.lower() in ("true", "1", "yes")
        elif mode_env is not None:
            snapshot_only = mode_env == "snapshot-only"
        else:
            snapshot_only = False
            latest_file = root / "latest.json"
            if latest_file.is_file():
                try:
                    latest_data = json.loads(latest_file.read_text(encoding="utf-8"))
                    if (
                        latest_data.get("snapshot_only") is True
                        or latest_data.get("mode") == "snapshot-only"
                    ):
                        snapshot_only = True
                except Exception:
                    pass
        if not mode_env:
            mode = "snapshot-only" if snapshot_only else "passive"
        else:
            mode = "snapshot-only" if snapshot_only else mode_env
        return cls(
            root=root,
            source=os.environ.get(
                "VAULTWARDEN_STANDBY_SOURCE",
                "vaultwarden-sync@10.147.20.129::vaultwarden-snapshots/",
            ),
            source_port=int(os.environ.get("VAULTWARDEN_STANDBY_SOURCE_PORT", "6868")),
            ssh_key=Path(
                os.environ.get(
                    "VAULTWARDEN_STANDBY_SSH_KEY",
                    "/etc/vaultwarden-standby/id_ed25519",
                )
            ),
            known_hosts=Path(
                os.environ.get(
                    "VAULTWARDEN_STANDBY_KNOWN_HOSTS",
                    "/etc/vaultwarden-standby/known_hosts",
                )
            ),
            retention=retention,
            container=os.environ.get(
                "VAULTWARDEN_STANDBY_CONTAINER",
                "vaultwarden",
            ),
            mode=mode,
            snapshot_only=snapshot_only,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise StandbyError(f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StandbyError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StandbyError(f"{label} must be a JSON object")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_quick_check(database: Path) -> None:
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise StandbyError(f"SQLite quick_check failed: {exc}") from exc
    if not row or row[0] != "ok":
        raise StandbyError(f"SQLite quick_check returned {row!r}")


def safe_reference(reference: str) -> str:
    if not reference or reference.startswith(".") or "/" in reference or "\\" in reference:
        raise StandbyError(f"invalid snapshot reference: {reference!r}")
    return reference


def forbidden(relative: Path) -> bool:
    return relative.name in FORBIDDEN_NAMES or bool(FORBIDDEN_PARTS.intersection(relative.parts))


def validate_snapshot(
    snapshot: Path,
    expected_manifest_sha256: str | None = None,
    expected_snapshot_id: str | None = None,
) -> dict[str, Any]:
    manifest_path = snapshot / "manifest.json"
    if manifest_path.is_symlink():
        raise StandbyError("snapshot manifest must not be a symlink")
    manifest = load_json(manifest_path, "manifest")
    if manifest.get("complete") is not True:
        raise StandbyError("snapshot is not marked complete")
    expected_id = expected_snapshot_id or snapshot.name
    if manifest.get("snapshot_id") != expected_id:
        raise StandbyError("manifest snapshot_id does not match expected snapshot")
    if expected_manifest_sha256 and sha256_file(manifest_path) != expected_manifest_sha256:
        raise StandbyError("manifest checksum mismatch")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise StandbyError("manifest contains no files")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise StandbyError("manifest file entry must be an object")
        relative_text = item.get("path")
        if not isinstance(relative_text, str):
            raise StandbyError("manifest file entry has no path")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or forbidden(relative):
            raise StandbyError(f"unsafe snapshot path: {relative_text}")
        if relative_text in seen:
            raise StandbyError(f"duplicate snapshot path: {relative_text}")
        seen.add(relative_text)
        path = snapshot / relative
        if path.is_symlink() or not path.is_file():
            raise StandbyError(f"snapshot file is missing or unsafe: {relative_text}")
        if path.stat().st_size != item.get("size"):
            raise StandbyError(f"snapshot file size mismatch: {relative_text}")
        if sha256_file(path) != item.get("sha256"):
            raise StandbyError(f"snapshot file checksum mismatch: {relative_text}")

    actual_files: set[str] = set()
    for path in snapshot.rglob("*"):
        relative = path.relative_to(snapshot)
        if path.is_symlink():
            raise StandbyError(f"snapshot contains symlink: {relative.as_posix()}")
        if path.is_file() and relative.as_posix() != "manifest.json":
            actual_files.add(relative.as_posix())
        elif not path.is_file() and not path.is_dir():
            raise StandbyError(f"snapshot contains unsupported entry: {relative.as_posix()}")

    unlisted = sorted(actual_files - seen)
    if unlisted:
        raise StandbyError(
            "snapshot contains files not covered by manifest: " + ", ".join(unlisted)
        )
    if "db.sqlite3" not in seen:
        raise StandbyError("snapshot manifest does not contain db.sqlite3")
    sqlite_quick_check(snapshot / "db.sqlite3")
    return manifest


def run_rsync(config: Config) -> None:
    if not config.ssh_key.is_file():
        raise StandbyError(f"SSH private key does not exist: {config.ssh_key}")
    if not config.known_hosts.is_file():
        raise StandbyError(f"SSH known_hosts does not exist: {config.known_hosts}")
    config.incoming.mkdir(parents=True, exist_ok=True)
    ssh_command = (
        f"ssh -p {config.source_port} -i {config.ssh_key} "
        f"-o UserKnownHostsFile={config.known_hosts} "
        "-o StrictHostKeyChecking=yes "
        "-o BatchMode=yes "
        "-o ConnectTimeout=20 "
        "-o ServerAliveInterval=10 "
        "-o ServerAliveCountMax=3"
    )
    command = [
        "rsync",
        "-a",
        "--delete-delay",
        "--partial-dir=.rsync-partial",
        "--exclude=.snapshot.lock",
        "--exclude=.staging-*",
        "-e",
        ssh_command,
        config.source,
        f"{config.incoming}/",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise StandbyError(f"rsync failed ({completed.returncode}): {detail}")


def incoming_latest(config: Config) -> tuple[str, dict[str, Any]]:
    latest = load_json(config.incoming / "latest.json", "incoming latest.json")
    snapshot_id = safe_reference(str(latest.get("snapshot_id", "")))
    manifest_sha256 = latest.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
        raise StandbyError("incoming latest.json has no valid manifest_sha256")
    snapshot = config.incoming / snapshot_id
    if not snapshot.is_dir():
        raise StandbyError(f"incoming snapshot does not exist: {snapshot_id}")
    validate_snapshot(snapshot, manifest_sha256)
    return snapshot_id, latest


def local_snapshot(config: Config, reference: str) -> Path:
    if reference == "latest":
        latest = load_json(config.latest, "local latest.json")
        reference = safe_reference(str(latest.get("snapshot_id", "")))
    else:
        reference = safe_reference(reference)
    snapshot = config.snapshots / reference
    if not snapshot.is_dir():
        raise StandbyError(f"local snapshot does not exist: {reference}")
    return snapshot


def promote(config: Config, snapshot_id: str, remote_latest: dict[str, Any]) -> None:
    config.snapshots.mkdir(parents=True, exist_ok=True)
    source = config.incoming / snapshot_id
    destination = config.snapshots / snapshot_id
    if destination.exists():
        validate_snapshot(destination, remote_latest["manifest_sha256"])
    else:
        staging = config.snapshots / f".staging-{snapshot_id}-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            shutil.copytree(source, staging, symlinks=False)
            validate_snapshot(
                staging,
                remote_latest["manifest_sha256"],
                expected_snapshot_id=snapshot_id,
            )
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    atomic_json(
        config.latest,
        {
            "snapshot_id": snapshot_id,
            "created_at": remote_latest.get("created_at"),
            "manifest_sha256": remote_latest["manifest_sha256"],
            "promoted_at": utc_text(),
            "source": config.source,
            "mode": config.mode,
            "snapshot_only": config.snapshot_only,
        },
    )
    prune(config)


def published_snapshots(config: Config) -> list[Path]:
    if not config.snapshots.is_dir():
        return []
    return sorted(
        path
        for path in config.snapshots.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "manifest.json").is_file()
    )


def prune(config: Config) -> None:
    snapshots = published_snapshots(config)
    for snapshot in snapshots[:-config.retention]:
        shutil.rmtree(snapshot)


def write_sync_status(
    config: Config,
    *,
    result: str,
    snapshot_id: str | None = None,
    error: str = "",
) -> None:
    atomic_json(
        config.sync_status,
        {
            "result": result,
            "checked_at": utc_text(),
            "snapshot_id": snapshot_id,
            "source": config.source,
            "error": error,
            "mode": config.mode,
            "snapshot_only": config.snapshot_only,
        },
    )

def sync(config: Config) -> dict[str, Any]:
    config.root.mkdir(parents=True, exist_ok=True)
    config.state.mkdir(parents=True, exist_ok=True)
    lock_path = config.root / ".sync.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StandbyError("another standby synchronization is running") from exc
        try:
            run_rsync(config)
            snapshot_id, remote_latest = incoming_latest(config)
            promote(config, snapshot_id, remote_latest)
            write_sync_status(config, result="ok", snapshot_id=snapshot_id)
            return {
                "status": "ok",
                "snapshot_id": snapshot_id,
                "source": config.source,
            }
        except Exception as exc:
            write_sync_status(config, result="error", error=str(exc))
            raise


def verify(config: Config, reference: str) -> dict[str, Any]:
    snapshot = local_snapshot(config, reference)
    manifest = validate_snapshot(snapshot)
    return {
        "status": "ok",
        "snapshot_id": snapshot.name,
        "vaultwarden_version": manifest.get("vaultwarden_version"),
        "sqlite_quick_check": "ok",
        "file_count": len(manifest["files"]),
    }


def container_status(config: Config) -> tuple[str, str]:
    if config.snapshot_only:
        return "not_applicable", "not_applicable"
    try:
        completed = subprocess.run(
            ["docker", "inspect", config.container],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return "missing", "unknown"
    if completed.returncode != 0:
        return "missing", "unknown"
    try:
        payload = json.loads(completed.stdout)
        item = payload[0]
        return (
            str(item["State"]["Status"]),
            str(item["HostConfig"]["RestartPolicy"]["Name"] or "no"),
        )
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return "unknown", "unknown"

def snapshot_age(config: Config, snapshot_id: str | None) -> int | None:
    if not snapshot_id:
        return None
    try:
        manifest = load_json(config.snapshots / snapshot_id / "manifest.json", "manifest")
        created_at = str(manifest["created_at"])
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return max(0, int((utc_now() - created).total_seconds()))
    except (StandbyError, KeyError, TypeError, ValueError):
        return None


def status(config: Config) -> dict[str, Any]:
    latest_id: str | None = None
    if config.latest.is_file():
        try:
            latest_id = safe_reference(str(load_json(config.latest, "local latest.json").get("snapshot_id", "")))
        except StandbyError:
            latest_id = None
    sync_payload: dict[str, Any] = {}
    if config.sync_status.is_file():
        try:
            sync_payload = load_json(config.sync_status, "sync status")
        except StandbyError:
            sync_payload = {"result": "invalid", "error": "sync status is invalid"}
    docker_status, restart_policy = container_status(config)
    return {
        "mode": config.mode,
        "container_name": config.container,
        "container_status": docker_status,
        "container_restart_policy": restart_policy,
        "latest_valid_snapshot": latest_id,
        "snapshot_age_seconds": snapshot_age(config, latest_id),
        "last_sync_result": sync_payload.get("result", "never"),
        "last_sync_at": sync_payload.get("checked_at"),
        "last_sync_error": sync_payload.get("error", ""),
        "sync_source": config.source,
        "snapshot_only": config.snapshot_only,
    }

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="pull and promote the latest valid snapshot")
    verify_parser = subparsers.add_parser("verify", help="verify a local valid snapshot")
    verify_parser.add_argument("reference", nargs="?", default="latest")
    subparsers.add_parser("status", help="show passive standby status as JSON")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = Config.from_env()
        if args.command == "sync":
            payload = sync(config)
        elif args.command == "verify":
            payload = verify(config, args.reference)
        elif args.command == "status":
            payload = status(config)
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (StandbyError, OSError, ValueError) as exc:
        print(f"vaultwarden_standby: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
