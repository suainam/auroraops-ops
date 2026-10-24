#!/usr/bin/env python3
"""External 4-Hour Scheduler Tick for InstaBox Outer Probe & Bounded Spool.

Guarantees:
- Monotonic 180s total deadline across all phases.
- Cold wake reachability with bounded retry (up to 3 tries).
- Drain-before-probe to handle capacity backpressure.
- Durable controller reception: temp write -> SHA256 verify -> file fsync -> atomic rename -> dir fsync.
- Safe ACK: binds filename and SHA256 digest, checks returncode and response payload.
- Zero shell injection / interpolation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SEALED_FILENAME_RE = re.compile(r"^spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl$")
DEFAULT_CONTROLLER_ARCHIVE = Path.home() / ".auroraops" / "controller_spool" / "instabox"
MAX_LIFECYCLE_SECONDS = 180.0


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


class SchedulerTick:
    def __init__(
        self,
        ssh_target: str = "instabox",
        remote_dir: str | None = None,
        archive_dir: Path | None = None,
        max_duration: float = MAX_LIFECYCLE_SECONDS,
    ) -> None:
        self.ssh_target = ssh_target
        self.remote_dir = remote_dir or ("/data/home/auroraops-monitor" if self.ssh_target == "instabox" else "/var/lib/auroraops-monitor")
        self.archive_dir = archive_dir or (Path.home() / ".auroraops" / "controller_spool" / self.ssh_target)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.max_duration = max_duration
        self.start_mono = time.monotonic()
        self.deadline = self.start_mono + self.max_duration
        self.timing: dict[str, float] = {}

    def _remaining_timeout(self, cap: float = 30.0) -> float:
        rem = self.deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"Global scheduler deadline of {self.max_duration}s exceeded.")
        return min(rem, cap)

    def _run_ssh(self, cmd_args: list[str], timeout_cap: float = 30.0) -> tuple[int, str, str]:
        timeout = self._remaining_timeout(timeout_cap)
        full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(min(timeout, 15)))}", self.ssh_target] + cmd_args
        t0 = time.monotonic()
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - t0
        return proc.returncode, proc.stdout, proc.stderr

    def cold_wake(self) -> None:
        t0 = time.monotonic()
        attempts = 3
        last_err = ""
        for i in range(1, attempts + 1):
            try:
                rc, stdout, stderr = self._run_ssh(["true"], timeout_cap=15.0)
                if rc == 0:
                    self.timing["wake_seconds"] = round(time.monotonic() - t0, 3)
                    return
                last_err = stderr or f"rc={rc}"
            except Exception as exc:
                last_err = str(exc)
            time.sleep(1.0)
        raise RuntimeError(f"Cold wake failed after {attempts} attempts: {last_err}")

    def drain_unacked(self, seal_first: bool = True) -> list[dict]:
        t0 = time.monotonic()
        if seal_first:
            # 1. Ask remote to seal active spool if it exists
            rc_seal, out_seal, err_seal = self._run_ssh(
                ["python3", f"{self.remote_dir}/spool_manager.py", "seal"], timeout_cap=15.0
            )
            if rc_seal != 0:
                raise RuntimeError(f"Failed to seal remote spool: {err_seal or out_seal}")
        # 2. List sealed un-ACKed files
        rc, stdout, stderr = self._run_ssh(
            ["python3", f"{self.remote_dir}/spool_manager.py", "list"], timeout_cap=15.0
        )
        if rc != 0:
            raise RuntimeError(f"Failed to list un-ACKed files: {stderr}")
        try:
            files_meta = json.loads(stdout)
        except Exception as exc:
            raise RuntimeError(f"Invalid JSON from remote spool_manager list: {stdout}") from exc

        pulled = []
        for meta in files_meta:
            fname = meta.get("filename", "")
            remote_sha = meta.get("sha256", "")
            remote_size = meta.get("size_bytes", 0)

            # Strict filename validation
            if not SEALED_FILENAME_RE.match(fname):
                raise ValueError(f"Security: rejected non-conforming remote filename: '{fname}'")

            # Fixed remote path, zero traversal
            remote_path = f"{self.remote_dir}/spool/{fname}"
            # Temp local destination
            tmp_local = self.archive_dir / f".tmp_{fname}.{uuid.uuid4().hex[:8]}"
            final_local = self.archive_dir / fname

            # SCP transfer
            timeout = self._remaining_timeout(30.0)
            scp_cmd = [
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={max(1, int(min(timeout, 15)))}",
                f"{self.ssh_target}:{remote_path}",
                str(tmp_local),
            ]
            scp_rc = subprocess.run(scp_cmd, timeout=timeout).returncode
            if scp_rc != 0:
                if tmp_local.exists():
                    tmp_local.unlink()
                raise RuntimeError(f"SCP transfer failed for {fname}")

            # Fsync file
            with open(tmp_local, "rb") as f:
                os.fsync(f.fileno())

            # Validate size and checksum
            local_size = tmp_local.stat().st_size
            local_sha = compute_sha256(tmp_local)

            if local_size != remote_size or local_sha.lower() != remote_sha.lower():
                tmp_local.unlink()
                raise ValueError(
                    f"Integrity check failed for {fname}: size({local_size} vs {remote_size}), sha({local_sha} vs {remote_sha})"
                )

            # Atomic rename into permanent archive
            os.replace(tmp_local, final_local)
            _fsync_dir(self.archive_dir)

            # Send safe ACK with SHA256 binding
            ack_rc, ack_out, ack_err = self._run_ssh(
                ["python3", f"{self.remote_dir}/spool_manager.py", "ack", fname, "--sha256", local_sha],
                timeout_cap=15.0,
            )
            if ack_rc != 0:
                raise RuntimeError(f"ACK failed on remote for {fname}: {ack_err or ack_out}")

            ack_data = json.loads(ack_out)
            if ack_data.get("status") != "acked_and_removed":
                raise RuntimeError(f"Unexpected ACK response for {fname}: {ack_out}")

            pulled.append({
                "filename": fname,
                "size_bytes": local_size,
                "sha256": local_sha,
                "local_path": str(final_local),
            })

        self.timing["drain_seconds"] = round(time.monotonic() - t0, 3)
        return pulled

    def execute_probe(self) -> dict:
        t0 = time.monotonic()
        rc, stdout, stderr = self._run_ssh(
            ["python3", f"{self.remote_dir}/auroraops_probe.py"], timeout_cap=35.0
        )
        if rc != 0:
            raise RuntimeError(f"Probe execution failed: {stderr or stdout}")

        try:
            res = json.loads(stdout)
        except Exception as exc:
            raise RuntimeError(f"Invalid JSON from remote probe: {stdout}") from exc

        self.timing["probe_seconds"] = round(time.monotonic() - t0, 3)
        return res

    def run(self) -> dict:
        start_utc = datetime.now(timezone.utc).isoformat()

        # Step 1: Cold Wake
        self.cold_wake()

        # Step 2: Pre-probe drain of un-ACKed sealed files (protects against full-spool backpressure)
        pre_drained = self.drain_unacked()

        # Step 3: Run real exit probe & append to bounded spool
        probe_res = self.execute_probe()

        # Step 4: Seal newly recorded active sample and pull to durable controller archive
        post_drained = self.drain_unacked()

        total_elapsed = round(time.monotonic() - self.start_mono, 3)
        end_utc = datetime.now(timezone.utc).isoformat()

        sla_pass = (total_elapsed <= self.max_duration)
        all_pulled = pre_drained + post_drained

        summary = {
            "tick_id": f"tick-{int(time.time())}",
            "start_time_utc": start_utc,
            "end_time_utc": end_utc,
            "total_duration_seconds": total_elapsed,
            "sla_max_seconds": self.max_duration,
            "sla_pass": sla_pass,
            "timing_breakdown": self.timing,
            "pulled_records_count": len(all_pulled),
            "pulled_records": all_pulled,
            "latest_probe_record": probe_res.get("record"),
            "status": "ok" if sla_pass else "sla_breached",
        }
        return summary


def main():
    parser = argparse.ArgumentParser(description="External InstaBox / Node Scheduler Tick")
    parser.add_argument("--ssh-target", default=os.environ.get("INSTABOX_SSH_TARGET", "instabox"))
    parser.add_argument("--remote-dir", default=None, help="Remote monitor base directory")
    parser.add_argument("--archive-dir", type=Path, default=None)
    parser.add_argument("--max-duration", type=float, default=MAX_LIFECYCLE_SECONDS)
    parser.add_argument("--pull-only", action="store_true", help="Only pull and ACK un-ACKed sealed files without probe")
    parser.add_argument("--seal", action="store_true", help="Seal active spool before pulling (for pull-only mode)")
    args = parser.parse_args()

    tick = SchedulerTick(
        ssh_target=args.ssh_target,
        remote_dir=args.remote_dir,
        archive_dir=args.archive_dir,
        max_duration=args.max_duration,
    )
    if args.pull_only:
        try:
            pulled = tick.drain_unacked(seal_first=args.seal)
            summary = {
                "ssh_target": tick.ssh_target,
                "archive_dir": str(tick.archive_dir),
                "pulled_count": len(pulled),
                "pulled": pulled,
                "status": "ok",
            }
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            sys.exit(0)
        except Exception as exc:
            err_res = {
                "status": "failed",
                "error": str(exc),
                "elapsed_seconds": round(time.monotonic() - tick.start_mono, 3),
            }
            print(json.dumps(err_res, indent=2), file=sys.stderr)
            sys.exit(2)

    try:
        res = tick.run()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        if not res["sla_pass"]:
            sys.exit(1)
    except Exception as exc:
        err_res = {
            "status": "failed",
            "error": str(exc),
            "elapsed_seconds": round(time.monotonic() - tick.start_mono, 3),
        }
        print(json.dumps(err_res, indent=2), file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
