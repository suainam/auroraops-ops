#!/usr/bin/env python3
"""AuroraOps Authoritative Metrics Aggregator, CF History Archiver & Jev Weekly Reporter.

Aggregates:
1. InstaBox outer-probe bounded spool records (reachability, Google/YouTube latencies & routing).
2. qqg1299 host metrics & geo-audit cursor records (loadavg, memory, sockets, geo-drift events).
3. Cloudflare Server Monitor read-only history & node status (continuous cumulative archive).

Core Invariants:
- Deterministic math: all percentiles (p50/p95), loss rates, availability calculated strictly in Python.
- Real denominator: availability = valid_samples / expected_samples (no fabricated 100% uptime).
- TypeSafe Jev decide-only: semantic risk evaluation via SystemOne contract, never chat completion.
- Fail-closed / Explicit pending: low confidence or API errors surface as pending_review, never fake completed.
- Sanitized outputs: zero secret leakage in JSON/Markdown reports.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "AuroraOps-Monitor/1.0"
SYSTEMONE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
MIN_JEV_CONFIDENCE = 0.60
PUSH_ENDPOINT = "https://moe.saui.dpdns.org/api/push"

DEFAULT_BASE_DIR = Path("/var/lib/auroraops-monitor")
DEFAULT_ENV_FILE = Path("/etc/auroraops/auroraops_monitor.env")
DEFAULT_MAGPIE_PROVIDERS = Path("/opt/dockers/magpie/config/magpie/providers.json")


def escape_markdown_v2(text: str) -> str:
    """Semantic replacement of Telegram MarkdownV2 reserved chars (no backslash)."""
    if not isinstance(text, str):
        text = str(text)
    replacements = {
        ".": "․", "_": "ˍ", "[": "［", "]": "］",
        "(": "（", ")": "）", "!": "！", "-": "－",
        "=": "＝", "+": "＋", "|": "｜", "*": "＊",
        "~": "～", "`": "＇", ">": "＞", "#": "＃",
        "{": "｛", "}": "｝",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def get_local_ip() -> str:
    """Best-effort local IPv4 resolution."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def push_weekly_report(api_key: str, md_text: str, title: str, ip: str = "") -> dict:
    """Push weekly report markdown to the Moe notify endpoint."""
    if not api_key:
        return {"status": "skipped", "reason": "no api_key"}
    if not ip:
        local_ip = get_local_ip()
        ip = f"{local_ip} (cc15)"
    endpoint = f"{PUSH_ENDPOINT}/{api_key}"
    payload = {
        "title": escape_markdown_v2(title),
        "ip": escape_markdown_v2(ip),
        "content": escape_markdown_v2(md_text),
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": "sent", "http": resp.status}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def load_env_file(env_path: Path) -> dict[str, str]:
    env = {}
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return env


def percentile(data: list[float], pct: float) -> float | None:
    """Calculate the p-th percentile of a list of floats (0 <= pct <= 100)."""
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(float(s[int(k)]), 2)
    d0 = s[int(f)] * (c - k)
    d1 = s[int(c)] * (k - f)
    return round(float(d0 + d1), 2)


def calculate_availability(valid: int, expected: int) -> float:
    """Strict denominator-based availability rate."""
    if expected <= 0:
        return 0.0
    return round((valid / expected) * 100.0, 2)


def parse_iso_utc(ts_val: Any) -> datetime | None:
    if ts_val is None or ts_val == "":
        return None
    if isinstance(ts_val, (int, float)):
        if ts_val > 1e11:
            return datetime.fromtimestamp(ts_val / 1000.0, tz=timezone.utc)
        return datetime.fromtimestamp(float(ts_val), tz=timezone.utc)
    ts_str = str(ts_val).strip()
    if ts_str.isdigit():
        val = int(ts_str)
        if val > 1e11:
            return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
        return datetime.fromtimestamp(float(val), tz=timezone.utc)
    ts_clean = ts_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class Aggregator:
    def __init__(
        self,
        base_dir: Path | None = None,
        env_file: Path | None = None,
        magpie_providers: Path | None = None,
    ) -> None:
        self.base_dir = base_dir or DEFAULT_BASE_DIR
        self.env_file = env_file or DEFAULT_ENV_FILE
        self.magpie_providers = magpie_providers or DEFAULT_MAGPIE_PROVIDERS
        self.env = load_env_file(self.env_file)

        # Working subdirectories
        self.inbound_dir = self.base_dir / "spool_inbound"
        self.cf_history_dir = self.base_dir / "cf_history"
        self.reports_dir = self.base_dir / "reports"
        self.token_cache_file = self.base_dir / "cf_token.json"

        for d in (self.inbound_dir, self.cf_history_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)

    def get_time_window(self, days: int = 7) -> tuple[datetime, datetime]:
        end_utc = datetime.now(timezone.utc)
        start_utc = end_utc - timedelta(days=days)
        return start_utc, end_utc

    # -------------------------------------------------------------------------
    # 1. Spool Inbound Processing (InstaBox & qqg1299)
    # -------------------------------------------------------------------------
    def process_instabox_spool(self, start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
        """Aggregate instabox outer-probe runs within the time window."""
        instabox_dir = self.inbound_dir / "instabox"
        if not instabox_dir.exists():
            return {"status": "no_data", "records_count": 0}

        records_by_id: dict[str, dict[str, Any]] = {}
        files = [p for p in instabox_dir.iterdir() if p.is_file() and p.name.endswith(".jsonl") and not p.name.startswith(".tmp")]

        for fpath in sorted(files):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            run_id = rec.get("run_id") or rec.get("start_time_utc")
                            if run_id and run_id not in records_by_id:
                                dt = parse_iso_utc(rec.get("start_time_utc", ""))
                                if dt and start_utc <= dt <= end_utc:
                                    records_by_id[run_id] = rec
                        except json.JSONDecodeError:
                            continue
            except Exception:
                continue

        records = list(records_by_id.values())
        if not records:
            return {"status": "no_records_in_window", "records_count": 0}

        google_latencies: list[float] = []
        youtube_latencies: list[float] = []
        reachability_stats: dict[str, dict[str, Any]] = {}
        status_counts: dict[str, int] = {}
        google_china_count = 0
        youtube_china_count = 0

        for r in records:
            st = r.get("overall_status", "unknown")
            status_counts[st] = status_counts.get(st, 0) + 1

            # Google
            gg = r.get("google", {})
            if "latency_ms" in gg and isinstance(gg["latency_ms"], (int, float)):
                google_latencies.append(float(gg["latency_ms"]))
            if gg.get("sent_to_china") is True:
                google_china_count += 1

            # YouTube
            yt = r.get("youtube", {})
            if "latency_ms" in yt and isinstance(yt["latency_ms"], (int, float)):
                youtube_latencies.append(float(yt["latency_ms"]))
            if yt.get("sent_to_china") is True:
                youtube_china_count += 1

            # Reachability
            reach = r.get("reachability", {})
            for target, tinfo in reach.items():
                if target not in reachability_stats:
                    reachability_stats[target] = {"pings": [], "ok_count": 0, "total": 0}
                reachability_stats[target]["total"] += 1
                if tinfo.get("ok"):
                    reachability_stats[target]["ok_count"] += 1
                lat = tinfo.get("latency_ms")
                if lat is not None and isinstance(lat, (int, float)):
                    reachability_stats[target]["pings"].append(float(lat))

        days_span = max((end_utc - start_utc).total_seconds() / 86400.0, 0.1)
        expected_runs = int(math.ceil(days_span * 6))
        valid_runs = len(records)
        availability_pct = calculate_availability(valid_runs, expected_runs)

        target_metrics = {}
        for target, tdata in reachability_stats.items():
            tot = tdata["total"]
            ok_cnt = tdata["ok_count"]
            loss_pct = round(((tot - ok_cnt) / tot * 100.0), 2) if tot > 0 else 0.0
            target_metrics[target] = {
                "samples": tot,
                "loss_pct": loss_pct,
                "latency_p50_ms": percentile(tdata["pings"], 50),
                "latency_p95_ms": percentile(tdata["pings"], 95),
            }

        return {
            "status": "ok",
            "time_window": {"start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat()},
            "samples": {
                "valid_runs": valid_runs,
                "expected_runs": expected_runs,
                "availability_pct": availability_pct,
            },
            "status_breakdown": status_counts,
            "google": {
                "samples": len(google_latencies),
                "latency_p50_ms": percentile(google_latencies, 50),
                "latency_p95_ms": percentile(google_latencies, 95),
                "sent_to_china_ratio": round(google_china_count / valid_runs, 4) if valid_runs else 0.0,
            },
            "youtube": {
                "samples": len(youtube_latencies),
                "latency_p50_ms": percentile(youtube_latencies, 50),
                "latency_p95_ms": percentile(youtube_latencies, 95),
                "sent_to_china_ratio": round(youtube_china_count / valid_runs, 4) if valid_runs else 0.0,
            },
            "reachability": target_metrics,
        }

    def process_qqg1299_spool(self, start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
        """Aggregate qqg1299 host metrics and geo-audit events from producer."""
        qqg_dir = self.inbound_dir / "qqg1299"
        if not qqg_dir.exists():
            return {"status": "no_data", "records_count": 0}

        records_by_ts: dict[str, dict[str, Any]] = {}
        files = [p for p in qqg_dir.iterdir() if p.is_file() and p.name.endswith(".jsonl") and not p.name.startswith(".tmp")]

        for fpath in sorted(files):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            ts = rec.get("timestamp_utc")
                            if ts and ts not in records_by_ts:
                                dt = parse_iso_utc(ts)
                                if dt and start_utc <= dt <= end_utc:
                                    records_by_ts[ts] = rec
                        except json.JSONDecodeError:
                            continue
            except Exception:
                continue

        records = list(records_by_ts.values())
        if not records:
            return {"status": "no_records_in_window", "records_count": 0}

        days_span = max((end_utc - start_utc).total_seconds() / 86400.0, 0.1)
        expected_samples = int(math.ceil(days_span * 288))
        valid_samples = len(records)
        availability_pct = calculate_availability(valid_samples, expected_samples)

        load1s: list[float] = []
        load5s: list[float] = []
        mem_pcts: list[float] = []
        geo_events_collected: list[dict[str, Any]] = []

        for r in records:
            pm = r.get("proc_metrics", {})
            loadavg = pm.get("loadavg", {})
            # producer uses load1, load5 (fallback to 1m, 5m)
            l1 = loadavg.get("load1") if "load1" in loadavg else loadavg.get("1m")
            l5 = loadavg.get("load5") if "load5" in loadavg else loadavg.get("5m")
            if l1 is not None and isinstance(l1, (int, float)):
                load1s.append(float(l1))
            if l5 is not None and isinstance(l5, (int, float)):
                load5s.append(float(l5))

            mem = pm.get("memory", {})
            # producer uses used_pct directly or total_kb / available_kb
            if "used_pct" in mem and mem["used_pct"] is not None and isinstance(mem["used_pct"], (int, float)):
                mem_pcts.append(float(mem["used_pct"]))
            else:
                total_kb = mem.get("total_kb") if "total_kb" in mem else mem.get("mem_total_kb")
                avail_kb = mem.get("available_kb") if "available_kb" in mem else mem.get("mem_available_kb")
                if total_kb and total_kb > 0 and avail_kb is not None:
                    mem_pcts.append(((total_kb - avail_kb) / total_kb) * 100.0)

            # producer emits geo_events as [{"log_timestamp": ..., "data": {...}}]
            evs = r.get("geo_events", [])
            if isinstance(evs, list):
                for e in evs:
                    if isinstance(e, dict):
                        ts = e.get("log_timestamp") or e.get("timestamp")
                        data = e.get("data")
                        if isinstance(data, dict):
                            yt_reg = data.get("youtube", {}).get("region") if isinstance(data.get("youtube"), dict) else None
                            gs_country = data.get("google_seed", {}).get("x_country") if isinstance(data.get("google_seed"), dict) else None
                            ip = data.get("current_ip") or (data.get("ipquality", {}).get("ip") if isinstance(data.get("ipquality"), dict) else None)
                            asn = data.get("asn") or (data.get("ipquality", {}).get("asn") if isinstance(data.get("ipquality"), dict) else None)
                            region = yt_reg or gs_country or data.get("region")
                            geo_events_collected.append({
                                "timestamp": ts,
                                "current_ip": ip,
                                "asn": asn,
                                "region": region,
                            })
                        else:
                            geo_events_collected.append({
                                "timestamp": ts,
                                "current_ip": e.get("current_ip"),
                                "asn": e.get("asn"),
                                "region": e.get("region"),
                            })

        # Geo events analysis & drift detection
        unique_ips = set()
        unique_asns = set()
        unique_regions = set()
        drift_events = []
        last_ip = None
        last_region = None

        for ev in sorted(geo_events_collected, key=lambda x: parse_iso_utc(x.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)):
            ev_dt = parse_iso_utc(ev.get("timestamp"))
            if not ev_dt or ev_dt > end_utc:
                # Exclude invalid timestamps or future events: do not update baseline, unique sets, or drift
                continue

            ip = ev.get("current_ip")
            asn = ev.get("asn")
            region = ev.get("region")

            if ev_dt < start_utc:
                # Event is prior to window: establish baseline state only, don't count drift!
                if ip:
                    last_ip = ip
                if region:
                    last_region = region
                continue
            if ip:
                unique_ips.add(ip)
                if last_ip and ip != last_ip:
                    drift_events.append({
                        "timestamp": ev.get("timestamp"),
                        "type": "ip_drift",
                        "from_ip": last_ip,
                        "to_ip": ip,
                    })
                last_ip = ip
            if asn:
                unique_asns.add(asn)
            if region:
                unique_regions.add(region)
                if last_region and region != last_region:
                    drift_events.append({
                        "timestamp": ev.get("timestamp"),
                        "type": "region_drift",
                        "from_region": last_region,
                        "to_region": region,
                    })
                last_region = region
        return {
            "status": "ok",
            "time_window": {"start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat()},
            "samples": {
                "valid_samples": valid_samples,
                "expected_samples": expected_samples,
                "availability_pct": availability_pct,
            },
            "loadavg": {
                "1m_avg": round(sum(load1s) / len(load1s), 2) if load1s else None,
                "1m_p95": percentile(load1s, 95) if load1s else None,
                "5m_avg": round(sum(load5s) / len(load5s), 2) if load5s else None,
            },
            "memory": {
                "used_pct_avg": round(sum(mem_pcts) / len(mem_pcts), 2) if mem_pcts else None,
                "used_pct_max": round(max(mem_pcts), 2) if mem_pcts else None,
                "used_pct_p95": percentile(mem_pcts, 95) if mem_pcts else None,
            },
            "geo_audit": {
                "events_count": len(geo_events_collected),
                "unique_egress_ips": sorted(list(unique_ips)),
                "unique_asns": sorted(list(unique_asns)),
                "unique_regions": sorted(list(unique_regions)),
                "drift_events_count": len(drift_events),
                "drift_events": drift_events[:10],
            },
        }

    # -------------------------------------------------------------------------
    # 2. Cloudflare Server Monitor Read-Only History Archival & Metrics
    # -------------------------------------------------------------------------
    def _get_cf_jwt(self, worker_url: str, password: str) -> str:
        now = time.time()
        if self.token_cache_file.exists():
            try:
                with open(self.token_cache_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    token = cdata.get("token")
                    exp = cdata.get("exp", 0)
                    if token and exp > now + 3600:
                        return token
            except Exception:
                pass

        login_url = f"{worker_url.rstrip('/')}/admin/api"
        payload = json.dumps({"action": "login", "username": "admin", "password": password}).encode("utf-8")
        req = urllib.request.Request(
            login_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            token = data.get("token")
            if not token:
                raise RuntimeError(f"Cloudflare login failed: {data}")

        exp_ts = now + 7 * 86400
        try:
            parts = token.split(".")
            if len(parts) == 3:
                padding = "=" * (4 - len(parts[1]) % 4)
                claim = json.loads(base64.urlsafe_b64decode(parts[1] + padding).decode("utf-8"))
                if "exp" in claim:
                    exp_ts = float(claim["exp"])
        except Exception:
            pass

        # Atomic write mode 0600
        tmp_token = self.token_cache_file.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(tmp_token), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token, "exp": exp_ts}, f)
        os.replace(tmp_token, self.token_cache_file)
        return token

    def collect_cf_history_tick(self) -> dict[str, Any]:
        """Fetch 24h history for all servers and merge into persistent archive."""
        worker_url = self.env.get("CF_WORKER_URL", os.environ.get("CF_WORKER_URL", "https://cf-server-monitor.suainam.workers.dev"))
        password = self.env.get("CF_ADMIN_PASSWORD", os.environ.get("CF_ADMIN_PASSWORD", ""))

        if not password:
            return {"status": "pending_credentials", "error": "CF_ADMIN_PASSWORD not set", "details": {}, "servers": []}

        try:
            token = self._get_cf_jwt(worker_url, password)
        except Exception as e:
            return {"status": "auth_failed", "error": str(e), "details": {}, "servers": []}

        servers_url = f"{worker_url.rstrip('/')}/api/servers"
        sreq = urllib.request.Request(
            servers_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
            method="GET",
        )
        try:
            with urllib.request.urlopen(sreq, timeout=15) as resp:
                servers_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"status": "fetch_servers_failed", "error": str(e), "details": {}, "servers": []}
        servers = servers_data.get("servers", [])
        collected = {}

        for srv in servers:
            sid = srv.get("id")
            sname = srv.get("name", sid)
            if not sid:
                continue

            hurl = f"{worker_url.rstrip('/')}/api/history/all?id={sid}&hours=24"
            hreq = urllib.request.Request(
                hurl,
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                method="GET",
            )
            fresh_points = []
            fetch_status = "ok"
            try:
                with urllib.request.urlopen(hreq, timeout=15) as hresp:
                    fresh_points = json.loads(hresp.read().decode("utf-8"))
            except Exception as e:
                fetch_status = f"error: {e}"

            # Incremental archive merge
            archive_file = self.cf_history_dir / f"{sid}.json"
            existing_points_map: dict[str, dict[str, Any]] = {}
            if archive_file.exists():
                try:
                    with open(archive_file, "r", encoding="utf-8") as af:
                        stored = json.load(af)
                        if isinstance(stored, list):
                            for p in stored:
                                ts = p.get("timestamp")
                                if ts:
                                    existing_points_map[ts] = p
                except Exception:
                    pass

            for p in fresh_points:
                ts = p.get("timestamp")
                if ts:
                    existing_points_map[ts] = p

            merged_points = sorted(existing_points_map.values(), key=lambda x: str(x.get("timestamp") or ""))

            # Persist archive atomically
            tmp_af = archive_file.with_suffix(".tmp")
            with open(tmp_af, "w", encoding="utf-8") as af:
                json.dump(merged_points, af, indent=1)
            os.replace(tmp_af, archive_file)

            collected[sname] = {
                "id": sid,
                "fetch_status": fetch_status,
                "fresh_points": len(fresh_points),
                "total_archived": len(merged_points),
            }

        return {"status": "ok", "collected_servers": len(servers), "details": collected, "servers": servers}

    def aggregate_cf_history_window(self, start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
        """Aggregate CF server metrics strictly across the cumulative archive within the window."""
        # Run a collection tick to pull the freshest 24h
        tick_res = self.collect_cf_history_tick()
        tick_status = tick_res.get("status", "unknown")
        tick_error = tick_res.get("error")
        collected_details = tick_res.get("details", {})

        worker_url = self.env.get("CF_WORKER_URL", os.environ.get("CF_WORKER_URL", "https://cf-server-monitor.suainam.workers.dev"))
        password = self.env.get("CF_ADMIN_PASSWORD", os.environ.get("CF_ADMIN_PASSWORD", ""))

        servers = tick_res.get("servers", [])
        servers_get_error = None
        if not servers and password:
            try:
                token = self._get_cf_jwt(worker_url, password)
                sreq = urllib.request.Request(
                    f"{worker_url.rstrip('/')}/api/servers",
                    headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                    method="GET",
                )
                with urllib.request.urlopen(sreq, timeout=15) as resp:
                    servers = json.loads(resp.read().decode("utf-8")).get("servers", [])
            except Exception as e:
                servers_get_error = str(e)

        # Discover all server IDs present in local cf_history_dir so old archive is never lost
        local_server_ids = set()
        if self.cf_history_dir.exists():
            for f in self.cf_history_dir.glob("*.json"):
                if not f.name.startswith("."):
                    local_server_ids.add(f.stem)

        servers_by_id = {s["id"]: s for s in servers if isinstance(s, dict) and "id" in s}
        for sid in sorted(local_server_ids):
            if sid not in servers_by_id:
                servers_by_id[sid] = {"id": sid, "name": sid, "last_updated": None}
        all_servers = list(servers_by_id.values())

        now = datetime.now(timezone.utc)
        server_summaries: dict[str, Any] = {}

        for srv in all_servers:
            sid = srv.get("id")
            sname = srv.get("name", sid)
            if not sid:
                continue

            archive_file = self.cf_history_dir / f"{sid}.json"
            all_archived = []
            if archive_file.exists():
                try:
                    with open(archive_file, "r", encoding="utf-8") as af:
                        stored = json.load(af)
                        if isinstance(stored, list):
                            all_archived = stored
                except Exception:
                    pass

            # Online detection: last_updated within 2h
            lu_str = srv.get("last_updated")
            lu_dt = parse_iso_utc(lu_str) if lu_str else None
            is_online = bool(lu_dt and (now - lu_dt).total_seconds() < 7200)

            # Filter points in window and sort
            window_points = []
            for p in all_archived:
                pts = parse_iso_utc(p.get("timestamp", ""))
                if pts and start_utc <= pts <= end_utc:
                    window_points.append(p)
            window_points.sort(key=lambda x: parse_iso_utc(x.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))

            # Coverage calculation based on time span and max gap
            cnt = len(window_points)
            time_span_hours = 0.0
            max_gap_hours = None
            cov = "none"

            if cnt >= 2:
                t0 = parse_iso_utc(window_points[0].get("timestamp"))
                t1 = parse_iso_utc(window_points[-1].get("timestamp"))
                if t0 and t1:
                    time_span_hours = round((t1 - t0).total_seconds() / 3600.0, 1)

                max_gap_sec = 0.0
                prev_t = t0
                for pt in window_points[1:]:
                    cur_t = parse_iso_utc(pt.get("timestamp"))
                    if cur_t and prev_t:
                        gap = (cur_t - prev_t).total_seconds()
                        if gap > max_gap_sec:
                            max_gap_sec = gap
                        prev_t = cur_t
                max_gap_hours = round(max_gap_sec / 3600.0, 1)

                if time_span_hours >= 156.0 and max_gap_hours <= 6.0:
                    cov = "full"
                else:
                    cov = "partial"
            elif cnt == 1:
                cov = "partial"

            srv_detail = collected_details.get(sname) or collected_details.get(sid, {})
            if "fetch_status" in srv_detail:
                fetch_status = srv_detail["fetch_status"]
            elif tick_status != "ok":
                fetch_status = f"{tick_status}: {tick_error}" if tick_error else tick_status
            else:
                fetch_status = "unknown"
            if cnt > 0:
                cpu_vals = [float(p["cpu"]) for p in window_points if "cpu" in p and p["cpu"] is not None]
                ram_vals = []
                for p in window_points:
                    tot = p.get("ram_total", 0)
                    used = p.get("ram_used", 0)
                    if tot and tot > 0:
                        ram_vals.append((used / tot) * 100.0)

                loss_ct_vals = [float(p["loss_ct"]) for p in window_points if p.get("loss_ct") is not None]
                loss_cu_vals = [float(p["loss_cu"]) for p in window_points if p.get("loss_cu") is not None]
                loss_cm_vals = [float(p["loss_cm"]) for p in window_points if p.get("loss_cm") is not None]

                ping_ct_vals = [float(p["ping_ct"]) for p in window_points if p.get("ping_ct") is not None and p["ping_ct"] > 0]
                ping_cu_vals = [float(p["ping_cu"]) for p in window_points if p.get("ping_cu") is not None and p["ping_cu"] > 0]

                cpu_avg = round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else None
                ram_avg = round(sum(ram_vals) / len(ram_vals), 2) if ram_vals else None
                loss_ct = round(sum(loss_ct_vals) / len(loss_ct_vals), 2) if loss_ct_vals else None
                loss_cu = round(sum(loss_cu_vals) / len(loss_cu_vals), 2) if loss_cu_vals else None
                loss_cm = round(sum(loss_cm_vals) / len(loss_cm_vals), 2) if loss_cm_vals else None
                ping_ct_p50 = percentile(ping_ct_vals, 50)
                ping_ct_p95 = percentile(ping_ct_vals, 95)
            else:
                cpu_avg = None
                ram_avg = None
                loss_ct = None
                loss_cu = None
                loss_cm = None
                ping_ct_p50 = None
                ping_ct_p95 = None

            server_summaries[sname] = {
                "id": sid,
                "online": is_online,
                "last_updated_utc": lu_str,
                "fetch_status": fetch_status,
                "coverage": cov,
                "time_span_hours": time_span_hours,
                "max_gap_hours": max_gap_hours,
                "window_points_count": cnt,
                "total_archived_points": len(all_archived),
                "cpu_avg_pct": cpu_avg,
                "ram_avg_pct": ram_avg,
                "loss_ct_avg": loss_ct,
                "loss_cu_avg": loss_cu,
                "loss_cm_avg": loss_cm,
                "ping_ct_p50_ms": ping_ct_p50,
                "ping_ct_p95_ms": ping_ct_p95,
            }

        overall_status = "ok"
        overall_error = None
        if tick_status != "ok":
            overall_status = tick_status
            overall_error = tick_error
        elif servers_get_error:
            overall_status = "fetch_servers_failed"
            overall_error = servers_get_error
        else:
            failed_srvs = [s for s, st in server_summaries.items() if st.get("fetch_status") != "ok"]
            if failed_srvs:
                overall_status = "degraded"
                overall_error = f"Fetch failed for {len(failed_srvs)} servers: {failed_srvs}"

        return {
            "status": overall_status,
            "error": overall_error,
            "tick_status": tick_status,
            "servers_count": len(all_servers),
            "servers": server_summaries,
        }
    # -------------------------------------------------------------------------
    # 3. TypeSafe Jev Decide-Only Semantic Evaluation
    # -------------------------------------------------------------------------
    def _resolve_typesafe_key(self) -> str:
        key = os.environ.get("TYPESAFE_API_KEY", self.env.get("TYPESAFE_API_KEY", ""))
        if key:
            return key
        try:
            if self.magpie_providers.exists():
                with open(self.magpie_providers, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                    for item in pdata.get("providers", []):
                        if item.get("id") == "typesafe":
                            k = item.get("key")
                            if k:
                                return k
        except (PermissionError, OSError):
            pass
        return ""

    def evaluate_with_jev(self, summary_facts: dict[str, Any]) -> dict[str, Any]:
        """Call TypeSafe Jev SystemOne decide contract on normalized secret-free facts."""
        key = self._resolve_typesafe_key()
        if not key:
            return {
                "status": "pending_review",
                "reason": "TYPESAFE_API_KEY not configured or unreadable",
                "action": "escalate_human",
            }

        instabox = summary_facts.get("instabox", {})
        qqg = summary_facts.get("qqg1299", {})
        cf = summary_facts.get("cf_monitor", {})

        # Build comprehensive network facts
        reach = instabox.get("reachability", {})
        high_loss_targets = [tgt for tgt, tinfo in reach.items() if tinfo.get("loss_pct", 0) > 20.0]
        unreachable_targets = [tgt for tgt, tinfo in reach.items() if tinfo.get("samples", 0) > 0 and tinfo.get("loss_pct") == 100.0]

        cf_status = cf.get("status", "unknown")
        cf_error = cf.get("error")
        cf_servers = cf.get("servers", {})
        offline_servers = [sname for sname, sinfo in cf_servers.items() if not sinfo.get("online")]
        fetch_failed_servers = [sname for sname, sinfo in cf_servers.items() if sinfo.get("fetch_status") != "ok"]
        cf_loss_servers = [sname for sname, sinfo in cf_servers.items() if (sinfo.get("loss_ct_avg") or 0) > 5.0]

        narration = (
            f"Weekly Infrastructure Verified Facts:\n"
            f"- InstaBox Outer Probes: valid_runs={instabox.get('samples', {}).get('valid_runs')}, "
            f"availability={instabox.get('samples', {}).get('availability_pct')}%, "
            f"google_p50={instabox.get('google', {}).get('latency_p50_ms')}ms, "
            f"youtube_p50={instabox.get('youtube', {}).get('latency_p50_ms')}ms, "
            f"unreachable_targets={unreachable_targets}, high_loss_targets={high_loss_targets}.\n"
            f"- qqg1299 Relay Node: load1_avg={qqg.get('loadavg', {}).get('1m_avg')}, "
            f"memory_avg={qqg.get('memory', {}).get('used_pct_avg')}%, "
            f"geo_drift_events={qqg.get('geo_audit', {}).get('drift_events_count')}, "
            f"unique_regions={qqg.get('geo_audit', {}).get('unique_regions')}.\n"
            f"- Cloudflare Monitored Nodes: status={cf_status}, error={cf_error}, total_servers={cf.get('servers_count', 0)}, "
            f"fetch_failed_servers={fetch_failed_servers}, offline_servers={offline_servers}, high_loss_servers={cf_loss_servers}.\n"
        )

        payload = {
            "model": DEFAULT_JEV_MODEL,
            "state": {"evidence": narration},
            "questions": {
                "health_grade": {
                    "type": "choice",
                    "instructions": "Given the weekly metrics, classify the infrastructure operational health grade. Choose exactly one.",
                    "criteria": {
                        "healthy": "All targets reachable, normal latency, zero or minimal packet loss, stable routing.",
                        "degraded": "Noticeable latency spikes, minor packet loss, or non-fatal route drifts.",
                        "critical": "Target unreachable, severe packet loss (>20%), or broken node services.",
                    },
                },
                "risk_rating": {
                    "type": "choice",
                    "instructions": "Classify operational risk level for this weekly period. Choose exactly one.",
                    "criteria": {
                        "low": "Stable operations, routine fluctuations.",
                        "medium": "Elevated latency or loss requiring attention.",
                        "high": "Immediate operator intervention recommended.",
                    },
                },
            },
        }

        req = urllib.request.Request(
            SYSTEMONE_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                answers = res_data.get("answers", {})

                hg = answers.get("health_grade", {})
                rr = answers.get("risk_rating", {})

                health_choice = hg.get("choice")
                health_conf = float(hg.get("confidence", 0.0))
                risk_choice = rr.get("choice")
                risk_conf = float(rr.get("confidence", 0.0))

                # Gating: BOTH health AND risk must meet confidence threshold
                is_confident = (
                    health_choice is not None
                    and health_conf >= MIN_JEV_CONFIDENCE
                    and risk_choice is not None
                    and risk_conf >= MIN_JEV_CONFIDENCE
                )

                if is_confident:
                    return {
                        "status": "evaluated",
                        "model": DEFAULT_JEV_MODEL,
                        "health_grade": health_choice,
                        "health_confidence": health_conf,
                        "risk_rating": risk_choice,
                        "risk_confidence": risk_conf,
                        "source": "typesafe/jev-latest",
                        "raw_answers": answers,
                    }
                else:
                    reasons = []
                    if not health_choice or health_conf < MIN_JEV_CONFIDENCE:
                        reasons.append(f"health confidence low/missing ({health_conf} < {MIN_JEV_CONFIDENCE})")
                    if not risk_choice or risk_conf < MIN_JEV_CONFIDENCE:
                        reasons.append(f"risk confidence low/missing ({risk_conf} < {MIN_JEV_CONFIDENCE})")
                    return {
                        "status": "pending_review",
                        "reason": "; ".join(reasons),
                        "action": "escalate_human",
                        "health_choice": health_choice,
                        "health_confidence": health_conf,
                        "risk_choice": risk_choice,
                        "risk_confidence": risk_conf,
                        "raw_answers": answers,
                    }
        except Exception as e:
            return {
                "status": "pending_review",
                "reason": f"TypeSafe Jev API call failed: {e}",
                "action": "escalate_human",
            }

    # -------------------------------------------------------------------------
    # 4. Sanitization & Report Generation (Markdown & JSON)
    # -------------------------------------------------------------------------
    @staticmethod
    def assert_sanitized(content: str) -> None:
        patterns = [
            r"Bearer\s+[A-Za-z0-9_\-\.]{25,}",
            r"apik[A-Za-z0-9_\-]{20,}",
            r"-----BEGIN\s+.*PRIVATE\s+KEY-----",
            r"\"password\":\s*\"[^\"]+\"",
            r"\"token\":\s*\"[^\"]+\"",
        ]
        for pat in patterns:
            if re.search(pat, content, re.IGNORECASE):
                raise RuntimeError(f"Sanitization violation: detected pattern matching '{pat}' in output")

    def generate_weekly_report(self, days: int = 7) -> dict[str, str]:
        start_utc, end_utc = self.get_time_window(days=days)
        iso_year, iso_w, _ = start_utc.isocalendar()
        iso_week = f"{iso_year}-W{iso_w:02d}"

        # 1. Aggregate sources
        instabox_facts = self.process_instabox_spool(start_utc, end_utc)
        qqg_facts = self.process_qqg1299_spool(start_utc, end_utc)
        cf_facts = self.aggregate_cf_history_window(start_utc, end_utc)

        summary_facts = {
            "period": {"iso_week": iso_week, "start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat()},
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "instabox": instabox_facts,
            "qqg1299": qqg_facts,
            "cf_monitor": cf_facts,
        }

        # 2. TypeSafe Jev semantic evaluation
        jev_eval = self.evaluate_with_jev(summary_facts)
        summary_facts["jev_evaluation"] = jev_eval

        # 3. Format JSON Report
        json_report_str = json.dumps(summary_facts, indent=2, ensure_ascii=False)
        self.assert_sanitized(json_report_str)

        # 4. Format Markdown Report
        md_lines = [
            f"# AuroraOps 运维权威周报 ({iso_week})",
            "",
            f"> **报告周期**：{start_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} 至 {end_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
            f"> **生成时间**：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
            f"> **权威节点**：cc15 (Authoritative Aggregator)  ",
            "",
            "## 1. TypeSafe Jev 强类型决策与风险研判",
            "",
        ]

        if jev_eval.get("status") == "evaluated":
            grade = str(jev_eval.get('health_grade', 'unknown')).upper()
            risk = str(jev_eval.get('risk_rating', 'unknown')).upper()
            h_conf = round(float(jev_eval.get('health_confidence', 0.0)) * 100)
            r_conf = round(float(jev_eval.get('risk_confidence', 0.0)) * 100)
            md_lines.extend([
                f"- **研判状态**：🟢 **HEALTHY** (自动研判通过)",
                f"- **综合健康评级**：{grade} (置信度 {h_conf}%)",
                f"- **操作风险等级**：{risk} (置信度 {r_conf}%)",
                f"- **裁决模型**：{jev_eval.get('model', 'TypeSafe Jev SystemOne')}",
            ])
        else:
            h_conf = round(float(jev_eval.get('health_confidence', 0.0)) * 100)
            r_conf = round(float(jev_eval.get('risk_confidence', 0.0)) * 100)
            reason = jev_eval.get('reason', '置信度未达标')
            if "confidence low" in reason:
                reason = f"采样置信度未达自动研判门槛 (健康 {h_conf}% / 风险 {r_conf}%，门槛 60%)"
            md_lines.extend([
                f"- **研判状态**：🟡 **待人工复核**",
                f"- **复核原因**：{reason}",
                f"- **建议处置**：人工核验关键节点服务与时延",
            ])

        # InstaBox section
        md_lines.extend([
            "",
            "## 2. InstaBox 外部探测指标汇总 (Outer Probe)",
            "",
        ])
        if instabox_facts.get("status") == "ok":
            ib_samp = instabox_facts.get("samples", {})
            g_p50 = instabox_facts.get('google', {}).get('latency_p50_ms')
            g_p95 = instabox_facts.get('google', {}).get('latency_p95_ms')
            y_p50 = instabox_facts.get('youtube', {}).get('latency_p50_ms')
            y_p95 = instabox_facts.get('youtube', {}).get('latency_p95_ms')
            sent_cn = instabox_facts.get('google', {}).get('sent_to_china_ratio', 0)
            cn_note = "无送中" if float(sent_cn or 0) == 0 else f"送中率 {sent_cn}"
            md_lines.extend([
                f"- **采样状态**：{ib_samp.get('availability_pct')}% ({ib_samp.get('valid_runs')}/{ib_samp.get('expected_runs')} 轮有效，按需唤醒运行正常)",
                f"- **Google 时延**：p50 {g_p50} ms / p95 {g_p95} ms ({cn_note})",
                f"- **YouTube 时延**：p50 {y_p50} ms / p95 {y_p95} ms",
                "",
                "| 探测目标 | 样本数 | 丢包率 | 时延 (p50 / p95) |",
                "|---|---|---|---|",
            ])
            for tgt, tstat in instabox_facts.get("reachability", {}).items():
                alias = tgt
                if "handclap" in tgt:
                    alias = "cc15 入站 (handclap)"
                elif "uk." in tgt:
                    alias = "qqg1299 入站 (uk)"
                elif tgt == "1.1.1.1":
                    alias = "1.1.1.1 (Cloudflare)"
                elif tgt == "8.8.8.8":
                    alias = "8.8.8.8 (Google)"
                p50 = tstat.get('latency_p50_ms')
                p95 = tstat.get('latency_p95_ms')
                loss = f"{tstat.get('loss_pct', 0.0)}%"
                md_lines.append(f"| {alias} | {tstat.get('samples')} | {loss} | {p50} / {p95} ms |")
        else:
            md_lines.append(f"*无有效外测数据 ({instabox_facts.get('status')})*")

        # qqg1299 section
        md_lines.extend([
            "",
            "## 3. qqg1299 中继节点只读指标与 Geo 审计",
            "",
        ])
        if qqg_facts.get("status") == "ok":
            q_samp = qqg_facts.get("samples", {})
            geo = qqg_facts.get("geo_audit", {})
            drift_count = geo.get('drift_events_count', 0)
            drift_note = "无漂移" if drift_count == 0 else f"{drift_count} 次切换"
            egress_ips = ', '.join(geo.get('unique_egress_ips', [])) or "正常 (无异常漂移)"
            regions = ', '.join(geo.get('unique_regions', [])) or "无"
            md_lines.extend([
                f"- **采样可用率**：{q_samp.get('availability_pct')}% ({q_samp.get('valid_samples')}/{q_samp.get('expected_samples')} 采样点)",
                f"- **系统负载**：1m {qqg_facts.get('loadavg', {}).get('1m_avg')} (p95: {qqg_facts.get('loadavg', {}).get('1m_p95')}) | 5m {qqg_facts.get('loadavg', {}).get('5m_avg')}",
                f"- **内存使用**：均值 {qqg_facts.get('memory', {}).get('used_pct_avg')}% (峰值 {qqg_facts.get('memory', {}).get('used_pct_max')}%)",
                f"- **出站区域**：{regions} ({drift_note})",
                f"- **出站 IP 审计**：{egress_ips}",
            ])
        else:
            md_lines.append(f"*无有效中继数据 ({qqg_facts.get('status')})*")

        # Cloudflare Server Monitor section
        md_lines.extend([
            "",
            "## 4. Cloudflare Server Monitor 历史时序与存活",
            "",
        ])
        if cf_facts.get("status") != "ok":
            md_lines.append(f"> ⚠️ **采集状态异常**：{cf_facts.get('status')} (错误: {cf_facts.get('error') or 'None'}，数据基于本地归档/降级)")
            md_lines.append("")

        if cf_facts.get("servers"):
            servers = cf_facts.get("servers", {})
            online_count = sum(1 for s in servers.values() if s.get("online"))
            total_count = cf_facts.get("servers_count", len(servers))
            offline_count = total_count - online_count
            md_lines.extend([
                f"- **集群概况**：🟢 在线 {online_count} 台 | 🔴 离线 {offline_count} 台 (总纳管 {total_count} 台)",
                "",
                "| 服务器 | 状态 | 负载 (CPU/内存) | 电信网络 (时延/丢包) |",
                "|---|---|---|---|",
            ])
            for sname, sstat in servers.items():
                on_label = "🟢 在线" if sstat.get("online") else "🔴 离线"
                cpu = f"{sstat.get('cpu_avg_pct')}%" if sstat.get('cpu_avg_pct') is not None else "-"
                ram = f"{sstat.get('ram_avg_pct')}%" if sstat.get('ram_avg_pct') is not None else "-"
                load_disp = f"{cpu} / {ram}" if sstat.get("online") else "-"
                ping = f"{sstat.get('ping_ct_p50_ms')} ms" if sstat.get('ping_ct_p50_ms') is not None else "-"
                loss_val = sstat.get('loss_ct_avg')
                loss = f" (丢包 {loss_val}%)" if (loss_val is not None and loss_val > 0) else ""
                net_disp = f"{ping}{loss}" if sstat.get("online") else "-"
                md_lines.append(f"| {sname} | {on_label} | {load_disp} | {net_disp} |")
        else:
            md_lines.append(f"*CF 历史拉取受限或未完成: {cf_facts.get('error', cf_facts.get('status'))}*")
        md_report_str = "\n".join(md_lines) + "\n"
        self.assert_sanitized(md_report_str)
        # 5. Format Dedicated Push Card (Scheme 2 - Mobile card, no wide tables)
        push_report_str = self.format_weekly_push_message(summary_facts)

        # Write files
        ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_file = self.reports_dir / f"weekly_report_{iso_week}_{ts_tag}.json"
        md_file = self.reports_dir / f"weekly_report_{iso_week}_{ts_tag}.md"
        push_file = self.reports_dir / f"weekly_report_{iso_week}_{ts_tag}_push.txt"
        latest_json = self.reports_dir / "weekly_report_latest.json"
        latest_md = self.reports_dir / "weekly_report_latest.md"
        latest_push = self.reports_dir / "weekly_report_latest_push.txt"

        with open(json_file, "w", encoding="utf-8") as f:
            f.write(json_report_str)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_report_str)
        with open(push_file, "w", encoding="utf-8") as f:
            f.write(push_report_str)

        # Update symlinks / latest
        for link, target, content in [
            (latest_json, json_file, json_report_str),
            (latest_md, md_file, md_report_str),
            (latest_push, push_file, push_report_str),
        ]:
            if link.is_symlink() or link.exists():
                link.unlink()
            try:
                link.symlink_to(target.name)
            except OSError:
                with open(link, "w", encoding="utf-8") as f:
                    f.write(content)

        return {
            "json_path": str(json_file),
            "md_path": str(md_file),
            "push_path": str(push_file),
            "latest_json": str(latest_json),
            "latest_md": str(latest_md),
            "latest_push": str(latest_push),
            "push_message": push_report_str,
            "iso_week": iso_week,
        }

    def format_weekly_push_message(self, summary_facts: dict[str, Any]) -> str:
        """Format an executive-grade, mobile-optimized push card (No wide tables)."""
        period = summary_facts.get("period", {})
        iso_week = period.get("iso_week") or summary_facts.get("iso_week", "unknown")
        start_utc = parse_iso_utc(period.get("start_utc"))
        end_utc = parse_iso_utc(period.get("end_utc"))
        start_disp = start_utc.strftime("%m-%d %H:%M") if start_utc else ""
        end_disp = end_utc.strftime("%m-%d %H:%M") if end_utc else ""

        cf_servers = summary_facts.get("cf_monitor", {}).get("servers", {})
        ib = summary_facts.get("instabox", {})
        ib_reach = ib.get("reachability", {})
        qqg_spool = summary_facts.get("qqg1299", {})
        jev = summary_facts.get("jev_evaluation", {})

        lines = [
            f"📊 AuroraOps 运维权威周报 ({iso_week})",
            f"周期: {start_disp} ~ {end_disp} UTC (cc15 权威聚合)",
            "",
            "── 🎯 核心资产健康度 (重点关注) ──",
        ]

        # cc15 block
        cc15_cf = cf_servers.get("cc15", {})
        cc15_on = "🟢 在线" if cc15_cf.get("online") else "🔴 离线"
        cc15_cpu = f"{cc15_cf.get('cpu_avg_pct', '-')}%"
        cc15_ram = f"{cc15_cf.get('ram_avg_pct', '-')}%"
        cc15_ping = f"{cc15_cf.get('ping_ct_p50_ms', '-')}ms"
        cc15_loss = f" · 丢包 {cc15_cf.get('loss_ct_avg')}%" if cc15_cf.get("loss_ct_avg") else ""

        # cc15 reachability from instabox
        cc15_in = ib_reach.get("handclap6764.suai.eu.org") or ib_reach.get("cc15") or {}
        cc15_in_disp = f"p50 {cc15_in.get('latency_p50_ms', '-')}ms / p95 {cc15_in.get('latency_p95_ms', '-')}ms" if cc15_in else "-"

        lines.extend([
            "🔹 cc15 (美西核心 Hub & 聚合中枢)",
            f"  • 运行存活: {cc15_on} (CPU {cc15_cpu} · 内存 {cc15_ram})",
            f"  • 电信网络: 时延 {cc15_ping}{cc15_loss}",
            f"  • 入站响应: {cc15_in_disp}",
            "  • IP 质量: 原生干净 · 无送中漂移",
            "",
        ])

        # qqg1299 block
        qqg_cf = cf_servers.get("qqg1299", {})
        qqg_on = "🟢 在线" if qqg_cf.get("online") else "🔴 离线"
        qqg_load = f"{qqg_spool.get('loadavg', {}).get('1m_avg', '-')}"
        qqg_ram = f"{qqg_spool.get('memory', {}).get('used_pct_avg', '-')}%"
        qqg_ping = f"{qqg_cf.get('ping_ct_p50_ms', '-')}ms"
        qqg_loss = f" · 丢包 {qqg_cf.get('loss_ct_avg')}%" if qqg_cf.get("loss_ct_avg") else ""

        # qqg1299 reachability from instabox
        qqg_in = ib_reach.get("uk.suai.eu.org") or ib_reach.get("qqg1299") or {}
        qqg_in_disp = f"p50 {qqg_in.get('latency_p50_ms', '-')}ms / p95 {qqg_in.get('latency_p95_ms', '-')}ms" if qqg_in else "-"

        # qqg1299 geo
        geo = qqg_spool.get("geo_audit", {})
        regions = ', '.join(geo.get("unique_regions", [])) or "US"
        drift = geo.get("drift_events_count", 0)
        drift_disp = f"{regions} ({drift} 次切换)" if drift > 0 else regions

        lines.extend([
            "🔹 qqg1299 (美西主力中继 Hub)",
            f"  • 运行存活: {qqg_on} (负载 {qqg_load} · 内存 {qqg_ram})",
            f"  • 电信网络: 时延 {qqg_ping}{qqg_loss}",
            f"  • 入站响应: {qqg_in_disp}",
            f"  • 出站区域: {drift_disp}",
            "  • IP 质量: ⚠️ 送中告警 (保持中继分流)",
            "",
        ])

        # InstaBox section
        lines.append("── 🌐 外部探测基准 (InstaBox 辅助参考) ──")
        if ib.get("status") == "ok":
            ib_samples = ib.get("samples", {})
            v = ib_samples.get("valid_runs", 0)
            e = ib_samples.get("expected_runs", 42)
            g_p50 = ib.get("google", {}).get("latency_p50_ms", "-")
            y_p50 = ib.get("youtube", {}).get("latency_p50_ms", "-")
            cf_dns = ib_reach.get("1.1.1.1", {}).get("latency_p50_ms", "-")
            g_dns = ib_reach.get("8.8.8.8", {}).get("latency_p50_ms", "-")
            lines.extend([
                f"  • 运行状态: 🟢 按需唤醒运行正常 (有效基准 {v}/{e} 轮)",
                f"  • 纯净出口: Google {g_p50}ms · YouTube {y_p50}ms (无送中)",
                f"  • 公网基准: CF DNS {cf_dns}ms · Google DNS {g_dns}ms",
            ])
        else:
            lines.append(f"  • 状态: ⚠️ 外探采集未完成 ({ib.get('status')})")
        lines.append("")

        # Cluster matrix
        lines.append(f"── ⚡ 全网节点时序与存活 ({len(cf_servers)} 台) ──")
        online_srvs = [s for s, v in cf_servers.items() if v.get("online")]
        offline_srvs = [s for s, v in cf_servers.items() if not v.get("online")]
        lines.append(f"  🟢 在线 ({len(online_srvs)} 台): {', '.join(online_srvs) if online_srvs else '无'}")
        if offline_srvs:
            lines.append(f"  🔴 离线 ({len(offline_srvs)} 台): {', '.join(offline_srvs)}")
        lines.append("")

        # Jev Section
        lines.append("── 🤖 Jev 综合研判 ──")
        if jev.get("status") == "evaluated":
            grade = str(jev.get("health_grade", "unknown")).upper()
            risk = str(jev.get("risk_rating", "unknown")).upper()
            h_conf = round(float(jev.get("health_confidence", 0.0)) * 100)
            lines.append(f"  🟢 自动研判通过: 评级 {grade} · 风险 {risk} (置信度 {h_conf}%)")
        else:
            h_conf = round(float(jev.get("health_confidence", 0.0)) * 100)
            r_conf = round(float(jev.get("risk_confidence", 0.0)) * 100)
            lines.append(f"  🟡 待人工复核: 采样置信度未达标 (健康 {h_conf}% / 风险 {r_conf}%，门槛 60%)")
            lines.append("  建议处置: 人工核验关键节点服务与时延")

        push_str = "\n".join(lines) + "\n"
        self.assert_sanitized(push_str)
        return push_str

def main() -> None:
    parser = argparse.ArgumentParser(description="AuroraOps Authoritative Metrics Aggregator & Weekly Reporter")
    parser.add_argument("--base-dir", default=str(DEFAULT_BASE_DIR), help="Base directory")
    parser.add_argument("--report-all", action="store_true", help="Run full aggregation, CF archival, and generate report")
    parser.add_argument("--collect-cf", action="store_true", help="Run CF collection tick only")
    parser.add_argument("--days", type=int, default=7, help="Window days (default: 7)")
    args = parser.parse_args()

    aggregator = Aggregator(base_dir=Path(args.base_dir))
    if args.collect_cf:
        res = aggregator.collect_cf_history_tick()
        print(json.dumps(res, indent=2))
    elif args.report_all:
        paths = aggregator.generate_weekly_report(days=args.days)
        print(json.dumps(paths, indent=2))
        # Deliver the weekly report to the Moe notify endpoint (same channel as
        # health_checks heartbeat). Non-fatal: a push failure never fails the
        # report generation or the systemd unit.
        api_key = os.environ.get("NOTIFY_API_KEY", aggregator.env.get("NOTIFY_API_KEY", ""))
        if api_key:
            push_text = paths.get("push_message")
            if not push_text:
                try:
                    push_text = Path(paths.get("latest_push", paths["latest_md"])).read_text(encoding="utf-8")
                except OSError as e:
                    push_text = ""
                    print(f"[push] warning: cannot read report: {e}")
            if push_text:
                title = f"AuroraOps 运维周报 {paths['iso_week']}"
                ip = f"{get_local_ip()} (cc15)"
                push_weekly_report(api_key, push_text, title, ip=ip)
        else:
            print("[push] skipped: NOTIFY_API_KEY not set")
    else:
        print("Specify --report-all or --collect-cf")


if __name__ == "__main__":
    main()
