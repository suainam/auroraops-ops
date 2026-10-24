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

DEFAULT_BASE_DIR = Path("/var/lib/auroraops-monitor")
DEFAULT_ENV_FILE = Path("/etc/auroraops/auroraops_monitor.env")
DEFAULT_MAGPIE_PROVIDERS = Path("/opt/dockers/magpie/config/magpie/providers.json")


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

        for ev in sorted(geo_events_collected, key=lambda x: x.get("timestamp") or ""):
            ip = ev.get("current_ip")
            asn = ev.get("asn")
            region = ev.get("region")
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
            return {"status": "pending_credentials", "error": "CF_ADMIN_PASSWORD not set"}

        try:
            token = self._get_cf_jwt(worker_url, password)
        except Exception as e:
            return {"status": "auth_failed", "error": str(e)}

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
            return {"status": "fetch_servers_failed", "error": str(e)}

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

        return {"status": "ok", "collected_servers": len(servers), "details": collected}

    def aggregate_cf_history_window(self, start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
        """Aggregate CF server metrics strictly across the cumulative archive within the window."""
        # Run a collection tick to pull the freshest 24h
        self.collect_cf_history_tick()

        worker_url = self.env.get("CF_WORKER_URL", os.environ.get("CF_WORKER_URL", "https://cf-server-monitor.suainam.workers.dev"))
        password = self.env.get("CF_ADMIN_PASSWORD", os.environ.get("CF_ADMIN_PASSWORD", ""))

        servers = []
        if password:
            try:
                token = self._get_cf_jwt(worker_url, password)
                sreq = urllib.request.Request(
                    f"{worker_url.rstrip('/')}/api/servers",
                    headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                    method="GET",
                )
                with urllib.request.urlopen(sreq, timeout=15) as resp:
                    servers = json.loads(resp.read().decode("utf-8")).get("servers", [])
            except Exception:
                pass

        now = datetime.now(timezone.utc)
        server_summaries: dict[str, Any] = {}

        for srv in servers:
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

            # Filter points in window
            window_points = []
            for p in all_archived:
                pts = parse_iso_utc(p.get("timestamp", ""))
                if pts and start_utc <= pts <= end_utc:
                    window_points.append(p)

            # Online detection: last_updated within 2h
            lu_str = srv.get("last_updated")
            lu_dt = parse_iso_utc(lu_str) if lu_str else None
            is_online = bool(lu_dt and (now - lu_dt).total_seconds() < 7200)

            # Coverage classification
            cnt = len(window_points)
            if cnt >= 300:
                cov = "full"
            elif cnt > 0:
                cov = "partial"
            else:
                cov = "none"

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
                "coverage": cov,
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

        return {
            "status": "ok",
            "servers_count": len(servers),
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

        cf_servers = cf.get("servers", {})
        offline_servers = [sname for sname, sinfo in cf_servers.items() if not sinfo.get("online")]
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
            f"- Cloudflare Monitored Nodes: total_servers={cf.get('servers_count', 0)}, "
            f"offline_servers={offline_servers}, high_loss_servers={cf_loss_servers}.\n"
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
            f"> **报告周期**：`{start_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}` 至 `{end_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}`  ",
            f"> **生成时间**：`{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`  ",
            f"> **权威节点**：`cc15` (Authoritative Aggregator)  ",
            "",
            "## 1. TypeSafe Jev 强类型决策与风险研判",
            "",
        ]

        if jev_eval.get("status") == "evaluated":
            md_lines.extend([
                f"- **综合健康评级**：`{jev_eval.get('health_grade', 'unknown').upper()}` (置信度: `{jev_eval.get('health_confidence')}`)",
                f"- **操作风险等级**：`{jev_eval.get('risk_rating', 'unknown').upper()}` (置信度: `{jev_eval.get('risk_confidence')}`)",
                f"- **裁决模型**：`{jev_eval.get('model')}` (原生 SystemOne decide contract)",
            ])
        else:
            md_lines.extend([
                f"- **研判状态**：⚠️ **PENDING HUMAN REVIEW** (未伪造通过)",
                f"- **原因**：{jev_eval.get('reason', 'Unknown reason')}",
                f"- **建议处置**：{jev_eval.get('action', 'escalate_human')}",
            ])

        # InstaBox section
        md_lines.extend([
            "",
            "## 2. InstaBox 外部探测指标汇总 (Outer Probe)",
            "",
        ])
        if instabox_facts.get("status") == "ok":
            ib_samp = instabox_facts.get("samples", {})
            md_lines.extend([
                f"- **采样可用率**：`{ib_samp.get('availability_pct')}%` ({ib_samp.get('valid_runs')}/{ib_samp.get('expected_runs')} 有效轮次)",
                f"- **Google 时延**：p50 `{instabox_facts.get('google', {}).get('latency_p50_ms')} ms` / p95 `{instabox_facts.get('google', {}).get('latency_p95_ms')} ms` (送中率: `{instabox_facts.get('google', {}).get('sent_to_china_ratio')}`)",
                f"- **YouTube 时延**：p50 `{instabox_facts.get('youtube', {}).get('latency_p50_ms')} ms` / p95 `{instabox_facts.get('youtube', {}).get('latency_p95_ms')} ms`",
                "",
                "| 探测目标 | 样本数 | 丢包率 | 时延 p50 | 时延 p95 |",
                "|---|---|---|---|---|",
            ])
            for tgt, tstat in instabox_facts.get("reachability", {}).items():
                md_lines.append(f"| `{tgt}` | {tstat.get('samples')} | {tstat.get('loss_pct')}% | {tstat.get('latency_p50_ms')} ms | {tstat.get('latency_p95_ms')} ms |")
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
            md_lines.extend([
                f"- **采样可用率**：`{q_samp.get('availability_pct')}%` ({q_samp.get('valid_samples')}/{q_samp.get('expected_samples')} 有效采样点)",
                f"- **负载均值**：1m `{qqg_facts.get('loadavg', {}).get('1m_avg')}` (p95: `{qqg_facts.get('loadavg', {}).get('1m_p95')}`) / 5m `{qqg_facts.get('loadavg', {}).get('5m_avg')}`",
                f"- **内存使用**：均值 `{qqg_facts.get('memory', {}).get('used_pct_avg')}%` / 峰值 `{qqg_facts.get('memory', {}).get('used_pct_max')}%`",
                f"- **出站 IP 集合**：`{', '.join(geo.get('unique_egress_ips', [])) or 'None'}`",
                f"- **Geo 区域集合**：`{', '.join(geo.get('unique_regions', [])) or 'None'}`",
                f"- **Geo 漂移事件**：`{geo.get('drift_events_count')}` 次",
            ])
        else:
            md_lines.append(f"*无有效中继数据 ({qqg_facts.get('status')})*")

        # Cloudflare Server Monitor section
        md_lines.extend([
            "",
            "## 4. Cloudflare Server Monitor 历史时序与存活",
            "",
        ])
        if cf_facts.get("status") == "ok":
            md_lines.extend([
                f"- **纳管服务器总数**：`{cf_facts.get('servers_count')}` 台",
                "",
                "| 服务器名称 | 在线状态 | 覆盖度 | 窗口采样点 | CPU 均值 | 内存均值 | 电信丢包 | 电信时延 p50 |",
                "|---|---|---|---|---|---|---|---|",
            ])
            for sname, sstat in cf_facts.get("servers", {}).items():
                on_label = "🟢 在线" if sstat.get("online") else "🔴 离线"
                cov_label = sstat.get("coverage", "none")
                cpu_disp = f"{sstat.get('cpu_avg_pct')}%" if sstat.get("cpu_avg_pct") is not None else "N/A"
                ram_disp = f"{sstat.get('ram_avg_pct')}%" if sstat.get("ram_avg_pct") is not None else "N/A"
                loss_disp = f"{sstat.get('loss_ct_avg')}%" if sstat.get('loss_ct_avg') is not None else "N/A"
                ping_disp = f"{sstat.get('ping_ct_p50_ms')} ms" if sstat.get('ping_ct_p50_ms') is not None else "N/A"
                md_lines.append(
                    f"| `{sname}` | {on_label} | `{cov_label}` | {sstat.get('window_points_count')} | {cpu_disp} | {ram_disp} | "
                    f"{loss_disp} | {ping_disp} |"
                )
        else:
            md_lines.append(f"*CF 历史拉取受限或未完成: {cf_facts.get('error', cf_facts.get('status'))}*")

        md_report_str = "\n".join(md_lines) + "\n"
        self.assert_sanitized(md_report_str)

        # Write files
        ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_file = self.reports_dir / f"weekly_report_{iso_week}_{ts_tag}.json"
        md_file = self.reports_dir / f"weekly_report_{iso_week}_{ts_tag}.md"
        latest_json = self.reports_dir / "weekly_report_latest.json"
        latest_md = self.reports_dir / "weekly_report_latest.md"

        with open(json_file, "w", encoding="utf-8") as f:
            f.write(json_report_str)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_report_str)

        # Update symlinks / latest
        if latest_json.is_symlink() or latest_json.exists():
            latest_json.unlink()
        if latest_md.is_symlink() or latest_md.exists():
            latest_md.unlink()

        try:
            latest_json.symlink_to(json_file.name)
            latest_md.symlink_to(md_file.name)
        except OSError:
            with open(latest_json, "w", encoding="utf-8") as f:
                f.write(json_report_str)
            with open(latest_md, "w", encoding="utf-8") as f:
                f.write(md_report_str)

        return {
            "json_path": str(json_file),
            "md_path": str(md_file),
            "latest_json": str(latest_json),
            "latest_md": str(latest_md),
            "iso_week": iso_week,
        }


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
    else:
        print("Specify --report-all or --collect-cf")


if __name__ == "__main__":
    main()
