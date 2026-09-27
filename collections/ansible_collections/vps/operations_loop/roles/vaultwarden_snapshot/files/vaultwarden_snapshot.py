#!/usr/bin/env python3
"""Create and validate immutable Vaultwarden SQLite snapshots.

The script deliberately keeps transport and long-term retention out of scope.
It creates a locally consistent recovery point that Rustic or another transport
can back up as ordinary files.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc


FORBIDDEN_NAMES = {
    "db.sqlite3-wal",
    "db.sqlite3-shm",
    "vaultwarden.log",
}
FORBIDDEN_PARTS = {"icon_cache", "tmp"}
COPY_DIRECTORIES = ("attachments", "sends")
COPY_FILES = ("config.json",)
COPY_GLOBS = ("rsa_key*",)


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be safely created or verified."""


@dataclass(frozen=True)
class Config:
    source_dir: Path
    snapshot_root: Path
    container: str
    health_url: str
    retention: int
    hostname: str
    notifier_path: Path
    notifier_python: str
    notify_failure: bool

    @classmethod
    def from_env(cls) -> "Config":
        retention = int(os.environ.get("VAULTWARDEN_SNAPSHOT_RETENTION", "12"))
        if retention < 1:
            raise SnapshotError("VAULTWARDEN_SNAPSHOT_RETENTION must be at least 1")
        return cls(
            source_dir=Path(
                os.environ.get(
                    "VAULTWARDEN_SNAPSHOT_SOURCE_DIR",
                    "/opt/dockers/vaultwarden/data",
                )
            ),
            snapshot_root=Path(
                os.environ.get(
                    "VAULTWARDEN_SNAPSHOT_ROOT",
                    "/opt/backups/vaultwarden-snapshots",
                )
            ),
            container=os.environ.get("VAULTWARDEN_SNAPSHOT_CONTAINER", "vaultwarden"),
            health_url=os.environ.get(
                "VAULTWARDEN_SNAPSHOT_HEALTH_URL",
                "http://127.0.0.1:30007/alive",
            ),
            retention=retention,
            hostname=os.environ.get("VAULTWARDEN_SNAPSHOT_HOSTNAME", socket.gethostname()),
            notifier_path=Path(
                os.environ.get(
                    "VAULTWARDEN_SNAPSHOT_NOTIFIER",
                    "/usr/local/bin/notifier.py",
                )
            ),
            notifier_python=os.environ.get(
                "VAULTWARDEN_SNAPSHOT_NOTIFIER_PYTHON",
                "/opt/aurora_venv/bin/python3",
            ),
            notify_failure=_env_bool("VAULTWARDEN_SNAPSHOT_NOTIFY_FAILURE", True),
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def snapshot_id() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S.%fZ")


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def run_command(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise SnapshotError(f"command failed ({completed.returncode}): {' '.join(command)}: {detail}")
    return completed.stdout.strip()


def check_health(url: str) -> None:
    if not url:
        return
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read(128)
            if response.status != 200:
                raise SnapshotError(f"Vaultwarden health returned HTTP {response.status}")
            if not body:
                raise SnapshotError("Vaultwarden health returned an empty body")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SnapshotError(f"Vaultwarden health check failed: {exc}") from exc


def sqlite_quick_check(database: Path) -> None:
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise SnapshotError(f"SQLite quick_check failed for {database}: {exc}") from exc
    if not row or row[0] != "ok":
        raise SnapshotError(f"SQLite quick_check returned {row!r} for {database}")


def _safe_copy_file(source: Path, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    except FileNotFoundError:
        # A file may disappear between the two non-destructive attachment passes.
        return


def copy_directory_superset(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for current_root, directories, files in os.walk(source):
        directories.sort()
        files.sort()
        current = Path(current_root)
        relative_root = current.relative_to(source)
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            _safe_copy_file(current / name, target_root / name)


def copy_filesystem_payload(config: Config, staging: Path) -> None:
    for directory_name in COPY_DIRECTORIES:
        copy_directory_superset(
            config.source_dir / directory_name,
            staging / directory_name,
        )


def copy_configuration_payload(config: Config, staging: Path) -> None:
    for file_name in COPY_FILES:
        source = config.source_dir / file_name
        if source.is_file():
            _safe_copy_file(source, staging / file_name)
    for pattern in COPY_GLOBS:
        for source in sorted(config.source_dir.glob(pattern)):
            if source.is_file():
                _safe_copy_file(source, staging / source.name)


def is_forbidden(relative_path: Path) -> bool:
    return relative_path.name in FORBIDDEN_NAMES or bool(FORBIDDEN_PARTS.intersection(relative_path.parts))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_file_manifest(snapshot: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(snapshot)
        if is_forbidden(relative):
            raise SnapshotError(f"forbidden file entered snapshot: {relative}")
        files.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return files


def built_in_backup_signatures(source_dir: Path) -> dict[Path, tuple[int, int]]:
    signatures: dict[Path, tuple[int, int]] = {}
    for path in source_dir.glob("db_*.sqlite3"):
        if path.is_file():
            stat = path.stat()
            signatures[path] = (stat.st_mtime_ns, stat.st_size)
    return signatures


def detect_generated_backup(
    before: dict[Path, tuple[int, int]],
    source_dir: Path,
) -> Path:
    after = built_in_backup_signatures(source_dir)
    changed = sorted(
        (path for path, signature in after.items() if before.get(path) != signature),
        key=lambda path: after[path],
    )
    if len(changed) != 1:
        names = ", ".join(path.name for path in changed) or "none"
        raise SnapshotError(
            "Vaultwarden backup command must create exactly one database file; "
            f"detected: {names}"
        )
    return changed[0]


def vaultwarden_version(config: Config) -> str:
    output = run_command(
        ["docker", "exec", config.container, "/vaultwarden", "--version"]
    )
    for line in output.splitlines():
        if line.lower().startswith("vaultwarden "):
            return line.split(maxsplit=1)[1].strip()
    raise SnapshotError(f"unable to parse Vaultwarden version from: {output!r}")


def resolve_snapshot(config: Config, reference: str) -> Path:
    if reference == "latest":
        latest_path = config.snapshot_root / "latest.json"
        if not latest_path.is_file():
            raise SnapshotError("latest.json does not exist")
        try:
            reference = json.loads(latest_path.read_text(encoding="utf-8"))["snapshot_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SnapshotError(f"invalid latest.json: {exc}") from exc
    if not reference or reference.startswith(".") or "/" in reference or "\\" in reference:
        raise SnapshotError(f"invalid snapshot reference: {reference!r}")
    snapshot = config.snapshot_root / reference
    if not snapshot.is_dir():
        raise SnapshotError(f"snapshot does not exist: {reference}")
    return snapshot


def load_manifest(snapshot: Path) -> dict[str, Any]:
    manifest_path = snapshot / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotError(f"manifest is missing from {snapshot.name}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"manifest is invalid JSON: {exc}") from exc
    if manifest.get("complete") is not True:
        raise SnapshotError(f"snapshot is not complete: {snapshot.name}")
    if manifest.get("snapshot_id") != snapshot.name:
        raise SnapshotError("manifest snapshot_id does not match directory name")
    return manifest


def verify_snapshot(config: Config, reference: str) -> dict[str, Any]:
    snapshot = resolve_snapshot(config, reference)
    manifest = load_manifest(snapshot)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SnapshotError("manifest contains no files")

    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotError("manifest file entry is not an object")
        relative_text = item.get("path")
        if not isinstance(relative_text, str):
            raise SnapshotError("manifest file entry has no path")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or is_forbidden(relative):
            raise SnapshotError(f"unsafe manifest path: {relative_text}")
        if relative_text in seen:
            raise SnapshotError(f"duplicate manifest path: {relative_text}")
        seen.add(relative_text)

        path = snapshot / relative
        if not path.is_file():
            raise SnapshotError(f"snapshot file is missing: {relative_text}")
        if path.stat().st_size != item.get("size"):
            raise SnapshotError(f"snapshot file size mismatch: {relative_text}")
        if sha256_file(path) != item.get("sha256"):
            raise SnapshotError(f"snapshot file checksum mismatch: {relative_text}")

    database = snapshot / "db.sqlite3"
    if "db.sqlite3" not in seen:
        raise SnapshotError("snapshot manifest does not contain db.sqlite3")
    sqlite_quick_check(database)
    return {
        "status": "ok",
        "snapshot_id": snapshot.name,
        "sqlite_quick_check": "ok",
        "file_count": len(files),
    }


def published_snapshots(config: Config) -> list[Path]:
    if not config.snapshot_root.is_dir():
        return []
    return sorted(
        path
        for path in config.snapshot_root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "manifest.json").is_file()
    )


def prune_snapshots(config: Config, keep: int) -> dict[str, Any]:
    if keep < 1:
        raise SnapshotError("keep must be at least 1")
    snapshots = published_snapshots(config)
    removed: list[str] = []
    for snapshot in snapshots[:-keep]:
        shutil.rmtree(snapshot)
        removed.append(snapshot.name)
    return {"status": "ok", "kept": min(keep, len(snapshots)), "removed": removed}


def cleanup_stale_staging(config: Config) -> list[str]:
    removed: list[str] = []
    for path in sorted(config.snapshot_root.glob(".staging-*")):
        if path.is_symlink() or not path.is_dir():
            raise SnapshotError(f"unexpected staging path type: {path}")
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def create_snapshot(config: Config) -> dict[str, Any]:
    if not config.source_dir.is_dir():
        raise SnapshotError(f"Vaultwarden source directory does not exist: {config.source_dir}")
    source_database = config.source_dir / "db.sqlite3"
    if not source_database.is_file():
        raise SnapshotError(f"Vaultwarden database does not exist: {source_database}")

    config.snapshot_root.mkdir(parents=True, exist_ok=True)
    lock_path = config.snapshot_root / ".snapshot.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotError("another Vaultwarden snapshot operation is running") from exc

        cleanup_stale_staging(config)
        check_health(config.health_url)
        current_id = snapshot_id()
        staging = config.snapshot_root / f".staging-{current_id}-{os.getpid()}"
        destination = config.snapshot_root / current_id
        generated_backup: Path | None = None
        if staging.exists() or destination.exists():
            raise SnapshotError(f"snapshot path already exists: {current_id}")
        staging.mkdir(mode=0o700)

        try:
            copy_filesystem_payload(config, staging)
            backup_files_before = built_in_backup_signatures(config.source_dir)
            run_command(
                ["docker", "exec", config.container, "/vaultwarden", "backup"]
            )
            generated_backup = detect_generated_backup(
                backup_files_before,
                config.source_dir,
            )
            _safe_copy_file(generated_backup, staging / "db.sqlite3")
            sqlite_quick_check(staging / "db.sqlite3")

            # A second non-destructive pass captures files created or updated while
            # the online SQLite backup was being generated. Deleted files remain as
            # a safe superset instead of making the snapshot incomplete.
            copy_filesystem_payload(config, staging)
            copy_configuration_payload(config, staging)

            manifest = {
                "snapshot_id": current_id,
                "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                "source_host": config.hostname,
                "vaultwarden_version": vaultwarden_version(config),
                "sqlite_quick_check": "ok",
                "database": "db.sqlite3",
                "complete": True,
                "files": build_file_manifest(staging),
            }
            json_dump_atomic(staging / "manifest.json", manifest)
            check_health(config.health_url)

            generated_backup.unlink()
            generated_backup = None
            os.replace(staging, destination)
            latest_payload = {
                "snapshot_id": current_id,
                "created_at": manifest["created_at"],
                "manifest_sha256": sha256_file(destination / "manifest.json"),
            }
            json_dump_atomic(config.snapshot_root / "latest.json", latest_payload)
            prune_snapshots(config, config.retention)
            return {
                "status": "ok",
                "snapshot_id": current_id,
                "path": str(destination),
                "sqlite_quick_check": "ok",
                "file_count": len(manifest["files"]),
            }
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            if generated_backup is not None:
                generated_backup.unlink(missing_ok=True)


def restore_test(config: Config, reference: str) -> dict[str, Any]:
    snapshot = resolve_snapshot(config, reference)
    verification = verify_snapshot(config, snapshot.name)
    with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-test-") as temporary:
        restored = Path(temporary) / "data"
        shutil.copytree(snapshot, restored)
        for stale_file in ("db.sqlite3-wal", "db.sqlite3-shm"):
            (restored / stale_file).unlink(missing_ok=True)
        sqlite_quick_check(restored / "db.sqlite3")
    return {
        "status": "ok",
        "snapshot_id": verification["snapshot_id"],
        "sqlite_quick_check": "ok",
    }


def notify_failure(config: Config, message: str) -> None:
    if not config.notify_failure or not config.notifier_path.is_file():
        return
    payload = [
        {
            "host": config.hostname,
            "service": "backups_vaultwarden_snapshot",
            "status": "error",
            "message": f"Vaultwarden snapshot failed: {message}",
            "timestamp": utc_now().isoformat().replace("+00:00", "Z"),
        }
    ]
    try:
        subprocess.run(
            [config.notifier_python, str(config.notifier_path)],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # The primary failure must still reach systemd/journald even when alerting
        # is unavailable. Notification is intentionally best effort.
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create", help="create and publish a new consistent snapshot")

    verify_parser = subparsers.add_parser("verify", help="verify a published snapshot")
    verify_parser.add_argument("reference", nargs="?", default="latest")

    restore_parser = subparsers.add_parser(
        "restore-test",
        help="restore a published snapshot into a temporary directory and validate it",
    )
    restore_parser.add_argument("reference", nargs="?", default="latest")

    prune_parser = subparsers.add_parser("prune", help="remove old published snapshots")
    prune_parser.add_argument("--keep", type=int, default=None)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        config = Config.from_env()
        if args.command == "create":
            result = create_snapshot(config)
        elif args.command == "verify":
            result = verify_snapshot(config, args.reference)
        elif args.command == "restore-test":
            result = restore_test(config, args.reference)
        elif args.command == "prune":
            result = prune_snapshots(config, args.keep or config.retention)
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (SnapshotError, OSError, ValueError) as exc:
        try:
            config
        except UnboundLocalError:
            config = None
        if config is not None:
            notify_failure(config, str(exc))
        print(f"vaultwarden_snapshot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
