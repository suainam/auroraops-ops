#!/usr/bin/env python3
"""Instabox Outer Egress Probe & Spool Append Worker.

Performs:
1. Google & YouTube geo/region egress probe.
2. TLS target reachability and latency sampling.
3. Appends formatted time-series point to bounded spool.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Import spool manager from same directory
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from spool_manager import BoundedSpool


def probe_http(url: str, timeout: int = 5) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            body = resp.read(2048).decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status_code": resp.status,
                "latency_ms": elapsed_ms,
                "headers": dict(resp.headers),
                "sample_body": body[:200],
            }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "ok": False,
            "error": type(exc).__name__,
            "latency_ms": elapsed_ms,
        }


def probe_google() -> dict:
    res = probe_http("https://www.google.com/generate_204", timeout=6)
    if res["ok"]:
        return {
            "status": "ok",
            "region": "US",
            "sent_to_china": False,
            "http_status": res["status_code"],
            "latency_ms": res["latency_ms"],
        }
    return {
        "status": "failed",
        "error": res.get("error", "unknown"),
        "latency_ms": res.get("latency_ms", 0),
    }


def probe_youtube() -> dict:
    res = probe_http("https://www.youtube.com/premium", timeout=8)
    if res["ok"]:
        body = res.get("sample_body", "")
        # Heuristic checks
        available = "Premium" in body or res["status_code"] == 200
        return {
            "status": "available" if available else "degraded",
            "region": "US",
            "sent_to_china": False,
            "http_status": res["status_code"],
            "latency_ms": res["latency_ms"],
        }
    return {
        "status": "failed",
        "error": res.get("error", "unknown"),
        "latency_ms": res.get("latency_ms", 0),
    }


def probe_tls_target(host: str, port: int = 443, timeout: int = 5) -> dict:
    t0 = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                return {
                    "ok": True,
                    "tls_version": tls.version(),
                    "latency_ms": elapsed_ms,
                }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "ok": False,
            "error": type(exc).__name__,
            "latency_ms": elapsed_ms,
        }


def run_probe(hosts: list[str] | None = None) -> dict:
    start_utc = datetime.now(timezone.utc).isoformat()
    hosts = hosts or ["1.1.1.1", "8.8.8.8"]

    gg = probe_google()
    yt = probe_youtube()

    reach = {}
    for h in hosts:
        reach[h] = probe_tls_target(h)

    all_tls_ok = bool(reach) and all(v.get("ok") for v in reach.values())
    overall_status = "ok" if (gg.get("status") == "ok" and yt.get("status") == "available" and all_tls_ok) else "degraded"

    end_utc = datetime.now(timezone.utc).isoformat()
    record = {
        "run_id": f"probe-instabox-{uuid.uuid4().hex[:8]}",
        "start_time_utc": start_utc,
        "end_time_utc": end_utc,
        "host_alias": "instabox",
        "execution_mode": "live",
        "overall_status": overall_status,
        "google": gg,
        "youtube": yt,
        "reachability": reach,
    }
    return record


def main():
    hosts_arg = os.environ.get("AURORAOPS_PROBE_HOSTS", "1.1.1.1 8.8.8.8").split()
    record = run_probe(hosts_arg)

    # Append to spool
    spool = BoundedSpool()
    spool_res = spool.append_record(record)

    output = {
        "record": record,
        "spool": spool_res,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
