#!/usr/bin/env python3
"""AuroraOps Host Metrics & Geo Cursor Sampler.

Guarantees:
- Pure read-only resource sampling from /proc (zero self-healing, zero restart side-effects).
- Incremental geo-audit log cursor reading (/var/log/geo_audit/geo_audit.log).
- Rotation-safe: detects inode changes or file truncation, resets cursor cleanly.
- Fail-safe cursor commit: cursor updates ONLY AFTER spool.append_record succeeds.
- Log privacy: redacts full IPv4 / IPv6 addresses to avoid leaking sensitive IPs.
- Fault-tolerant: missing geo-audit log or empty lines will never crash or abort metrics collection.
- 15-minute roll check: rolls/seals active file if it is older than 15 minutes (900s).
- Spool integrity: writes to BoundedSpool(active_name="metrics.jsonl") under flock.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from spool_manager import BoundedSpool

DEFAULT_BASE_DIR = Path("/var/lib/auroraops-monitor")
DEFAULT_GEO_LOG = Path("/var/log/geo_audit/geo_audit.log")
DEFAULT_CURSOR_FILE = "geo_audit_cursor.json"
DEFAULT_ROLL_INTERVAL_SECONDS = 900.0  # 15 minutes

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b")
GEO_LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")


def redact_ips(text: str) -> str:
    """Mask IPv4 and IPv6 addresses for privacy."""
    def mask_ipv4(m: re.Match) -> str:
        parts = m.group(0).split(".")
        return f"{parts[0]}.{parts[1]}.***.***"

    text = IP_RE.sub(mask_ipv4, text)
    text = IPV6_RE.sub("[REDACTED_IPV6]", text)
    return text


def collect_proc_metrics() -> dict:
    """Read host performance metrics directly from /proc."""
    metrics: dict = {
        "loadavg": {},
        "memory": {},
        "network": {},
        "sockets": {},
        "cpu_stat": {},
    }

    # /proc/loadavg
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            parts = f.read().split()
            if len(parts) >= 5:
                procs = parts[3].split("/")
                metrics["loadavg"] = {
                    "load1": float(parts[0]),
                    "load5": float(parts[1]),
                    "load15": float(parts[2]),
                    "procs_running": int(procs[0]) if len(procs) == 2 else 0,
                    "procs_total": int(procs[1]) if len(procs) == 2 else 0,
                }
    except Exception as exc:
        metrics["loadavg"] = {"error": str(exc)}

    # /proc/meminfo
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    val_parts = v.strip().split()
                    if val_parts and val_parts[0].isdigit():
                        mem[k.strip()] = int(val_parts[0])
        total = mem.get("MemTotal", 0)
        free = mem.get("MemFree", 0)
        avail = mem.get("MemAvailable", free)
        used = total - avail if total >= avail else 0
        metrics["memory"] = {
            "total_kb": total,
            "free_kb": free,
            "available_kb": avail,
            "used_kb": used,
            "used_pct": round((used / total) * 100, 2) if total > 0 else 0.0,
            "buffers_kb": mem.get("Buffers", 0),
            "cached_kb": mem.get("Cached", 0),
            "swap_total_kb": mem.get("SwapTotal", 0),
            "swap_free_kb": mem.get("SwapFree", 0),
        }
    except Exception as exc:
        metrics["memory"] = {"error": str(exc)}

    # /proc/net/dev
    try:
        net: dict[str, dict] = {}
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines[2:]:
                if ":" in line:
                    iface, stats = line.split(":", 1)
                    cols = stats.split()
                    if len(cols) >= 16:
                        net[iface.strip()] = {
                            "rx_bytes": int(cols[0]),
                            "rx_packets": int(cols[1]),
                            "rx_errors": int(cols[2]),
                            "rx_drops": int(cols[3]),
                            "tx_bytes": int(cols[8]),
                            "tx_packets": int(cols[9]),
                            "tx_errors": int(cols[10]),
                            "tx_drops": int(cols[11]),
                        }
        metrics["network"] = net
    except Exception as exc:
        metrics["network"] = {"error": str(exc)}

    # /proc/net/sockstat
    try:
        sock: dict[str, int] = {}
        with open("/proc/net/sockstat", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if not parts:
                    continue
                proto = parts[0].rstrip(":")
                for i in range(1, len(parts), 2):
                    if i + 1 < len(parts) and parts[i+1].isdigit():
                        sock[f"{proto}_{parts[i]}"] = int(parts[i+1])
        metrics["sockets"] = sock
    except Exception as exc:
        metrics["sockets"] = {"error": str(exc)}

    # /proc/stat
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()[1:]
                    metrics["cpu_stat"] = {
                        "user": int(parts[0]),
                        "nice": int(parts[1]),
                        "system": int(parts[2]),
                        "idle": int(parts[3]),
                        "iowait": int(parts[4]) if len(parts) > 4 else 0,
                    }
                    break
    except Exception as exc:
        metrics["cpu_stat"] = {"error": str(exc)}

    return metrics


class GeoAuditCursorReader:
    def __init__(self, log_path: Path, cursor_file: Path) -> None:
        self.log_path = log_path
        self.cursor_file = cursor_file

    def _read_cursor(self) -> dict:
        if self.cursor_file.exists():
            try:
                with open(self.cursor_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"inode": 0, "offset": 0, "last_updated": ""}

    def _read_complete_lines(self, file_path: Path, start_offset: int) -> tuple[list[dict], int]:
        parsed_events: list[dict] = []
        if not file_path.exists():
            return parsed_events, start_offset
        curr_size = file_path.stat().st_size
        if start_offset > curr_size:
            start_offset = 0
        last_valid_offset = start_offset
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(start_offset)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        # Incomplete half-line in-flight: do not consume and do not advance offset
                        break
                    last_valid_offset = f.tell()
                    line_str = line.strip()
                    if not line_str:
                        continue
                    m = GEO_LINE_RE.match(line_str)
                    if m:
                        ts_str = m.group(1)
                        raw_payload = m.group(2)
                        clean_payload = redact_ips(raw_payload)
                        try:
                            parsed_payload = json.loads(clean_payload)
                        except Exception:
                            parsed_payload = {"raw": clean_payload}
                        parsed_events.append({
                            "log_timestamp": ts_str,
                            "data": parsed_payload,
                        })
                    else:
                        clean_line = redact_ips(line_str)
                        parsed_events.append({
                            "log_timestamp": datetime.now(timezone.utc).isoformat(),
                            "raw": clean_line,
                        })
        except Exception:
            pass
        return parsed_events, last_valid_offset

    def read_incremental_events(self) -> tuple[list[dict], dict | None]:
        """Read new events since last cursor.

        Guarantees:
        - Drains unread tail of previous inode file upon log rotation before reading new file.
        - Incomplete / half lines are never consumed and do not advance cursor.
        - Candidate cursor state MUST only be committed after successful spool write!
        """
        if not self.log_path.exists():
            return [], None

        st = self.log_path.stat()
        curr_inode = st.st_ino
        curr_size = st.st_size

        saved = self._read_cursor()
        saved_inode = saved.get("inode", 0)
        saved_offset = saved.get("offset", 0)

        events: list[dict] = []

        if curr_inode != saved_inode and saved_inode > 0:
            # Drain tail of previous rotated file if still present on disk
            old_file = None
            if self.log_path.parent.exists():
                for f in self.log_path.parent.iterdir():
                    try:
                        if f.is_file() and f.stat().st_ino == saved_inode:
                            old_file = f
                            break
                    except Exception:
                        pass
            if old_file is not None:
                old_events, _ = self._read_complete_lines(old_file, saved_offset)
                events.extend(old_events)
            # Switch to new file starting from offset 0
            new_events, new_offset = self._read_complete_lines(self.log_path, 0)
            events.extend(new_events)
        else:
            # Same inode or fresh start
            new_events, new_offset = self._read_complete_lines(self.log_path, saved_offset)
            events.extend(new_events)

        candidate = {
            "inode": curr_inode,
            "offset": new_offset,
            "file_size": curr_size,
            "events_read": len(events),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        return events, candidate

    def commit_cursor(self, candidate: dict) -> None:
        """Atomically persist new cursor state with directory fsync."""
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(candidate, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.cursor_file)
        try:
            dir_fd = os.open(str(self.cursor_file.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass


def sample_and_record(
    base_dir: Path | None = None,
    geo_log: Path | None = None,
    host_alias: str | None = None,
    roll_interval_seconds: float = DEFAULT_ROLL_INTERVAL_SECONDS,
) -> dict:
    base = base_dir or Path(os.environ.get("AURORAOPS_MONITOR_DIR", DEFAULT_BASE_DIR))
    log_path = geo_log or Path(os.environ.get("AURORAOPS_GEO_AUDIT_LOG", DEFAULT_GEO_LOG))
    host = host_alias or os.environ.get("AURORAOPS_HOST_ALIAS", socket.gethostname())

    cursor_path = base / DEFAULT_CURSOR_FILE
    spool = BoundedSpool(base_dir=base, active_name="metrics.jsonl")

    # 1. 15-minute roll check before writing new record
    seal_res = spool.seal_active(min_age_seconds=roll_interval_seconds)

    # 2. Read incremental geo events
    geo_reader = GeoAuditCursorReader(log_path=log_path, cursor_file=cursor_path)
    geo_events, candidate_cursor = geo_reader.read_incremental_events()

    # 3. Collect /proc performance metrics
    proc_metrics = collect_proc_metrics()

    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "collector": "auroraops_host_sampler",
        "type": "host_metrics",
        "proc_metrics": proc_metrics,
        "geo_events": geo_events,
        "status": "ok",
    }

    # 4. Append to Bounded Spool
    append_res = spool.append_record(record)

    # 5. Commit cursor ONLY after successful append
    if candidate_cursor is not None:
        geo_reader.commit_cursor(candidate_cursor)

    return {
        "status": "ok",
        "host": host,
        "seal_check": seal_res.get("status"),
        "append": append_res,
        "geo_events_count": len(geo_events),
        "cursor_committed": candidate_cursor is not None,
        "record": record,
    }


def main():
    parser = argparse.ArgumentParser(description="AuroraOps Host Metrics & Geo Cursor Sampler")
    parser.add_argument("--base-dir", type=Path, default=None)
    parser.add_argument("--geo-log", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--roll-interval", type=float, default=DEFAULT_ROLL_INTERVAL_SECONDS)
    args = parser.parse_args()

    res = sample_and_record(
        base_dir=args.base_dir,
        geo_log=args.geo_log,
        host_alias=args.host,
        roll_interval_seconds=args.roll_interval,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
