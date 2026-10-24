#!/usr/bin/env python3
"""Bounded Spool Manager for auroraops-monitor.

Guarantees:
- Single writer lock (fcntl.flock).
- Bounded spool capacity (50MB default).
- Zero active file truncation (rename-based roll with directory fsync).
- Backpressure fail-closed when capacity exceeded with un-ACKed files (never delete un-ACKed records).
- Secure ACK handshake to remove only verified sealed files with SHA256 verification and directory fsync.
- Strict filename validation rejecting path traversal and symlinks.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE_DIR = Path("/data/home/auroraops-monitor")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50MB
SEALED_FILENAME_RE = re.compile(r"^spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl$")


def _fsync_dir(dir_path: Path) -> None:
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class BoundedSpool:
    def __init__(
        self,
        base_dir: Path | None = None,
        max_bytes: int | None = None,
        active_name: str | None = None,
    ) -> None:
        self.base_dir = base_dir or Path(os.environ.get("AURORAOPS_SPOOL_DIR", DEFAULT_BASE_DIR))
        self.spool_dir = self.base_dir / "spool"
        active_file_str = active_name or os.environ.get("AURORAOPS_SPOOL_ACTIVE_NAME")
        if not active_file_str:
            if (self.base_dir / "metrics.jsonl").exists() and not (self.base_dir / "spool.jsonl").exists():
                active_file_str = "metrics.jsonl"
            else:
                active_file_str = "spool.jsonl"
        self.active_file = self.base_dir / active_file_str
        self.lock_file = self.base_dir / "spool.lock"
        self.max_bytes = max_bytes or int(os.environ.get("AURORAOPS_SPOOL_MAX_BYTES", DEFAULT_MAX_BYTES))
    def ensure_dirs(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    def _acquire_lock(self):
        self.ensure_dirs()
        lock_fd = open(self.lock_file, "a+")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        return lock_fd

    def _release_lock(self, lock_fd) -> None:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass

    def get_total_spool_bytes(self) -> int:
        total = 0
        if self.active_file.exists():
            total += self.active_file.stat().st_size
        if self.spool_dir.exists():
            for f in self.spool_dir.iterdir():
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
        return total

    def append_record(self, record: dict | str) -> dict:
        lock_fd = self._acquire_lock()
        try:
            if isinstance(record, dict):
                line = json.dumps(record, ensure_ascii=False) + "\n"
            else:
                line = record.strip() + "\n"
                # Validate JSON format
                json.loads(line)

            line_bytes = line.encode("utf-8")
            line_len = len(line_bytes)

            total_bytes = self.get_total_spool_bytes()
            if total_bytes + line_len > self.max_bytes:
                # Backpressure: fail closed, refuse write, never delete un-ACKed data
                raise RuntimeError(
                    f"Spool capacity exceeded ({total_bytes} + {line_len} > {self.max_bytes} bytes). "
                    "Backpressure activated: write rejected until un-ACKed records are pulled and ACKed."
                )

            active_size = self.active_file.stat().st_size if self.active_file.exists() else 0
            # If single active file exceeds max_bytes / 2 or 10MB, roll it
            roll_threshold = min(self.max_bytes // 2, 10 * 1024 * 1024)
            if active_size > 0 and (active_size + line_len > roll_threshold):
                self._roll_active_locked()

            # Append to active file
            with open(self.active_file, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

            _fsync_dir(self.base_dir)

            return {
                "status": "appended",
                "active_size": self.active_file.stat().st_size,
                "total_bytes": self.get_total_spool_bytes(),
            }
        finally:
            self._release_lock(lock_fd)

    def _roll_active_locked(self) -> Path | None:
        if not self.active_file.exists() or self.active_file.stat().st_size == 0:
            return None
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sealed_name = f"spool-{ts}.jsonl"
        target = self.spool_dir / sealed_name
        counter = 1
        while target.exists():
            target = self.spool_dir / f"spool-{ts}_{counter}.jsonl"
            counter += 1
        self.active_file.rename(target)
        _fsync_dir(self.spool_dir)
        _fsync_dir(self.base_dir)
        return target

    def seal_active(self, min_age_seconds: float = 0.0) -> dict:
        lock_fd = self._acquire_lock()
        try:
            if min_age_seconds > 0.0 and self.active_file.exists():
                file_age = time.time() - self.active_file.stat().st_mtime
                if file_age < min_age_seconds:
                    return {
                        "status": "skipped_too_young",
                        "sealed_path": None,
                        "sealed_filename": None,
                        "size_bytes": self.active_file.stat().st_size,
                        "age_seconds": round(file_age, 1),
                    }
            sealed = self._roll_active_locked()
            sha256 = compute_sha256(sealed) if sealed else None
            return {
                "status": "sealed" if sealed else "empty",
                "sealed_path": str(sealed) if sealed else None,
                "sealed_filename": sealed.name if sealed else None,
                "size_bytes": sealed.stat().st_size if sealed else 0,
                "sha256": sha256,
            }
        finally:
            self._release_lock(lock_fd)

    def list_unacked_files(self) -> list[dict]:
        self.ensure_dirs()
        result = []
        if self.spool_dir.exists():
            for f in sorted(self.spool_dir.iterdir()):
                # Strict validation: regular file, non-symlink, matching pattern
                if f.is_file() and not f.is_symlink() and SEALED_FILENAME_RE.match(f.name):
                    size = f.stat().st_size
                    sha = compute_sha256(f)
                    result.append({
                        "filename": f.name,
                        "size_bytes": size,
                        "sha256": sha,
                        "mtime_utc": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                    })
        return result

    def ack_file(self, filename: str, expected_sha256: str) -> dict:
        clean_name = os.path.basename(filename.strip())
        if not SEALED_FILENAME_RE.match(clean_name):
            raise ValueError(f"Invalid sealed filename format: '{clean_name}'")
        if not expected_sha256 or not re.match(r"^[0-9a-fA-F]{64}$", str(expected_sha256)):
            raise ValueError(f"expected_sha256 is required and must be a 64-character hex string, got: {expected_sha256}")

        target = self.spool_dir / clean_name
        lock_fd = self._acquire_lock()
        try:
            if not target.exists():
                return {"status": "not_found", "filename": clean_name}
            if target.is_symlink() or not target.is_file():
                raise ValueError("Target is not a regular file")

            actual_sha = compute_sha256(target)
            if actual_sha.lower() != expected_sha256.lower():
                raise ValueError(
                    f"Checksum mismatch for '{clean_name}': expected {expected_sha256}, got {actual_sha}. Deletion refused."
                )

            # Delete only after verified checksum and explicit ACK
            target.unlink()
            _fsync_dir(self.spool_dir)
            return {"status": "acked_and_removed", "filename": clean_name, "sha256": actual_sha}
        finally:
            self._release_lock(lock_fd)

    def get_status(self) -> dict:
        self.ensure_dirs()
        active_size = self.active_file.stat().st_size if self.active_file.exists() else 0
        unacked = self.list_unacked_files()
        sealed_size = sum(u["size_bytes"] for u in unacked)
        return {
            "base_dir": str(self.base_dir),
            "active_file_exists": self.active_file.exists(),
            "active_size_bytes": active_size,
            "unacked_sealed_files": unacked,
            "sealed_total_bytes": sealed_size,
            "total_spool_bytes": active_size + sealed_size,
            "max_capacity_bytes": self.max_bytes,
            "capacity_usage_pct": round(((active_size + sealed_size) / self.max_bytes) * 100, 2),
        }


def main():
    parser = argparse.ArgumentParser(description="Bounded Spool Manager")
    parser.add_argument("--base-dir", type=Path, default=None)
    parser.add_argument("--max-bytes", type=int, default=None)
    parser.add_argument("--active-name", type=str, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    append_p = sub.add_parser("append")
    append_p.add_argument("--json", dest="json_str", default=None, help="JSON record string")

    seal_p = sub.add_parser("seal")
    seal_p.add_argument("--older-than", type=float, default=0.0, help="Only seal if active file age >= seconds")
    sub.add_parser("list")
    sub.add_parser("status")

    ack_p = sub.add_parser("ack")
    ack_p.add_argument("filename", help="Sealed spool filename to ACK")
    ack_p.add_argument("--sha256", dest="sha256", required=True, help="Expected SHA256 checksum (required)")

    args = parser.parse_args()
    spool = BoundedSpool(base_dir=args.base_dir, max_bytes=args.max_bytes, active_name=args.active_name)
    if args.command == "append":
        raw = args.json_str
        if raw is None:
            raw = sys.stdin.read()
        res = spool.append_record(raw)
        print(json.dumps(res, indent=2))
    elif args.command == "seal":
        res = spool.seal_active(min_age_seconds=args.older_than)
        print(json.dumps(res, indent=2))
    elif args.command == "list":
        res = spool.list_unacked_files()
        print(json.dumps(res, indent=2))
    elif args.command == "ack":
        res = spool.ack_file(args.filename, expected_sha256=args.sha256)
        print(json.dumps(res, indent=2))
    elif args.command == "status":
        res = spool.get_status()
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
