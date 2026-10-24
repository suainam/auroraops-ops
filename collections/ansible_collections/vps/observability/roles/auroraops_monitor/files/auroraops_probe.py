#!/usr/bin/env python3
"""Instabox Outer Egress Probe & Spool Append Worker.

Performs:
1. Google & YouTube real geo/region egress probe via ipquality_geo_probe.
2. TLS target reachability and latency sampling.
3. Appends formatted time-series point to bounded spool via BoundedSpool.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from ipquality_geo_probe import build_opener, probe_google, probe_youtube
from spool_manager import BoundedSpool


def probe_tls_target(host: str, port: int = 443, timeout: int = 6) -> dict:
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
    hosts = hosts or os.environ.get("AURORAOPS_PROBE_HOSTS", "handclap6764.suai.eu.org uk.suai.eu.org").split()

    opener = build_opener("")
    gg = probe_google(opener)
    yt = probe_youtube(opener)

    reach = {}
    for h in hosts:
        reach[h] = probe_tls_target(h)

    all_tls_ok = bool(reach) and all(v.get("ok") for v in reach.values())
    gg_ok = (gg.get("status") == "ok")
    yt_ok = (yt.get("status") == "available")

    if gg_ok and yt_ok and all_tls_ok:
        overall_status = "ok"
    elif (gg_ok or yt_ok or all_tls_ok):
        overall_status = "degraded"
    else:
        overall_status = "failed"

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
    hosts_arg = os.environ.get("AURORAOPS_PROBE_HOSTS", "handclap6764.suai.eu.org uk.suai.eu.org").split()
    record = run_probe(hosts_arg)

    # Append to spool via BoundedSpool
    spool = BoundedSpool()
    spool_res = spool.append_record(record)

    output = {
        "record": record,
        "spool": spool_res,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
