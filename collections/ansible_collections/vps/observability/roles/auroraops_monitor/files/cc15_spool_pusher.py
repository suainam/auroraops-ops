#!/usr/bin/env python3
"""Controller to cc15 Spool Pusher with Atomic Confirmation & Zero Double-Counting.

Syncs verified sealed spool archives from:
    ~/.auroraops/controller_spool/{instabox,qqg1299}/*.jsonl
to:
    cc15:/var/lib/auroraops-monitor/spool_inbound/{source}/

Protocol guarantees:
1. Two-way verification: checks both local ledger AND remote file existence + sha256 digest before skipping.
2. Atomic remote delivery with remote file & directory fsync; zero masking with `|| true`.
3. Local durable state persistence with directory fsync.
4. Fail-closed: non-zero exit code if any file fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

FILENAME_RE = re.compile(r"^spool-[0-9]{8}T[0-9]{6}Z(_[0-9]+)?\.jsonl$")


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def fsync_dir(dir_path: Path) -> None:
    try:
        dfd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        pass


class Cc15SpoolPusher:
    def __init__(
        self,
        ssh_target: str = "cc15",
        ssh_user: str = "root",
        remote_inbound_base: str = "/var/lib/auroraops-monitor/spool_inbound",
        local_spool_base: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.ssh_target = ssh_target
        self.ssh_user = ssh_user
        self.effective_target = f"{self.ssh_user}@{self.ssh_target}" if self.ssh_user else self.ssh_target
        self.remote_inbound_base = remote_inbound_base
        self.local_spool_base = local_spool_base or (Path.home() / ".auroraops" / "controller_spool")
        self.state_file = state_file or (self.local_spool_base / "cc15_sync_state.json")
        self.state = self._load_state()

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)
        fsync_dir(self.state_file.parent)

    def run_remote_cmd(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        full_cmd = ["ssh", "-o", "BatchMode=yes", self.effective_target, cmd]
        return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def verify_remote_file(self, remote_path: str, expected_sha: str) -> bool:
        cmd = f"test -f '{remote_path}' && sha256sum '{remote_path}' | awk '{{print $1}}'"
        res = self.run_remote_cmd(cmd, timeout=15)
        if res.returncode == 0 and res.stdout.strip() == expected_sha:
            return True
        return False

    def sync_source(self, source_name: str) -> dict[str, int]:
        source_dir = self.local_spool_base / source_name
        if not source_dir.exists():
            return {"scanned": 0, "pushed": 0, "skipped": 0, "failed": 0}

        remote_source_dir = f"{self.remote_inbound_base}/{source_name}"
        # Ensure remote directory exists
        mkdir_res = self.run_remote_cmd(
            f"mkdir -p '{remote_source_dir}' && chown -R auroraops-monitor:auroraops-monitor '{remote_source_dir}'"
        )
        if mkdir_res.returncode != 0:
            raise RuntimeError(f"Failed to create remote directory {remote_source_dir}: {mkdir_res.stderr}")

        pushed = 0
        skipped = 0
        failed = 0
        files = [p for p in source_dir.iterdir() if p.is_file() and FILENAME_RE.match(p.name)]

        for local_file in sorted(files, key=lambda p: p.name):
            file_key = f"{source_name}/{local_file.name}"
            local_sha256 = compute_sha256(local_file)
            remote_final = f"{remote_source_dir}/{local_file.name}"

            # Check sync state AND check remote file presence
            existing_entry = self.state.get(file_key)
            if existing_entry and existing_entry.get("sha256") == local_sha256:
                if self.verify_remote_file(remote_final, local_sha256):
                    skipped += 1
                    continue
                # Remote file is missing despite local ledger; re-sync required!

            # Upload to temp file on remote
            nonce = uuid.uuid4().hex[:8]
            remote_tmp = f"{remote_source_dir}/.tmp_{local_file.name}.{nonce}"

            scp_cmd = ["scp", "-q", "-o", "BatchMode=yes", str(local_file), f"{self.effective_target}:{remote_tmp}"]
            scp_res = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60, check=False)
            if scp_res.returncode != 0:
                failed += 1
                continue

            # Atomically install on remote with sha256 verification and file/dir fsync
            remote_script = f"""python3 -c "
import os, sys, hashlib, shutil
tmp_path = sys.argv[1]
final_path = sys.argv[2]
expected_sha = sys.argv[3]
user = sys.argv[4]
group = sys.argv[5]

h = hashlib.sha256()
with open(tmp_path, 'rb') as f:
    while chunk := f.read(65536):
        h.update(chunk)
if h.hexdigest() != expected_sha:
    sys.exit(2)

os.replace(tmp_path, final_path)
fd = os.open(final_path, os.O_RDONLY)
os.fsync(fd)
os.close(fd)

dfd = os.open(os.path.dirname(final_path), os.O_RDONLY)
os.fsync(dfd)
os.close(dfd)

shutil.chown(final_path, user=user, group=group)
" '{remote_tmp}' '{remote_final}' '{local_sha256}' 'auroraops-monitor' 'auroraops-monitor'"""

            inst_res = self.run_remote_cmd(remote_script, timeout=30)
            if inst_res.returncode != 0:
                self.run_remote_cmd(f"rm -f '{remote_tmp}'")
                failed += 1
                continue

            # Record in local ledger
            self.state[file_key] = {
                "sha256": local_sha256,
                "synced_at_utc": datetime.now(timezone.utc).isoformat(),
                "size_bytes": local_file.stat().st_size,
            }
            self._save_state()
            pushed += 1

        return {"scanned": len(files), "pushed": pushed, "skipped": skipped, "failed": failed}

    def push_all(self, sources: list[str] | None = None) -> dict[str, dict[str, int]]:
        targets = sources or ["instabox", "qqg1299"]
        results = {}
        for src in targets:
            results[src] = self.sync_source(src)
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Push local controller spools to cc15 inbound directory")
    parser.add_argument("--ssh-target", default="cc15", help="Target SSH host (default: cc15)")
    parser.add_argument("--ssh-user", default="root", help="Target SSH user (default: root)")
    parser.add_argument("--sources", nargs="+", default=["instabox", "qqg1299"], help="Sources to sync")
    args = parser.parse_args()

    pusher = Cc15SpoolPusher(ssh_target=args.ssh_target, ssh_user=args.ssh_user)
    res = pusher.push_all(args.sources)
    print(json.dumps(res, indent=2))

    total_failed = sum(v.get("failed", 0) for v in res.values())
    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
