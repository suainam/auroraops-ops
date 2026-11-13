#!/usr/bin/env python3
"""OpenViking 3-Node Daily Rotation & Lifecycle Orchestrator.

Manages the daily 08:30-23:58 (Asia/Shanghai) sequential rotation across
three InstaBox nodes:
  - Node 1: instabox-suai   (10.144.10.16) [08:30 - 13:55] (~5.4h)
  - Node 2: instabox        (10.144.10.15) [13:55 - 19:20] (~5.4h)
  - Node 3: instabox-suaily (10.144.10.17) [19:20 - 23:58] (~4.6h)

Guarantees:
1. Strict Single-Writer: At any time, exactly one node is designated in cc15's ovcli.conf.
2. Verified Handoff: Queue empty check -> ov backup -> SHA256 pipe verify -> ov restore ->
   target health probe -> cc15 cutover -> source keepalive=0 (natural idle, no forced kill).
3. Overnight Continuity: 23:00 final backup -> preserved on cc15 archive -> 08:25 morning restore.
4. Zero third-party dependencies: Standard Python library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import inspect
import shlex
import sys
import tempfile
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import TypedDict

# Asia/Shanghai is UTC+8
TZ_SHANGHAI = timezone(timedelta(hours=8))


class NodeConfig(TypedDict):
    ip: str
    port: int
    duty_start: dt_time
    duty_end: dt_time


NODES: dict[str, NodeConfig] = {
    "instabox-suai": {
        "ip": "10.144.10.16",
        "port": 30012,
        "duty_start": dt_time(8, 30),
        "duty_end": dt_time(13, 55),
    },
    "instabox": {
        "ip": "10.144.10.15",
        "port": 30012,
        "duty_start": dt_time(13, 55),
        "duty_end": dt_time(19, 20),
    },
    "instabox-suaily": {
        "ip": "10.144.10.17",
        "port": 30012,
        "duty_start": dt_time(19, 20),
        "duty_end": dt_time(23, 58),
    },
}

CC15_HOST = "cc15"
CC15_OVCLI_CONF = "/opt/dockers/magpie/config/home/.openviking/ovcli.conf"
CC15_BACKUP_PATH = "/opt/dockers/magpie/config/home/.openviking/backups/openviking_night_final.ovpack"

CC15_MAGPIE_PROVIDERS = "/opt/dockers/magpie/config/magpie/providers.json"
CC15_MAGPIE_REMOTE_ID = "instabox"
CC15_MAGPIE_API_PORT = 30022


def get_shanghai_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(TZ_SHANGHAI)


def determine_current_duty_node(now: datetime | None = None) -> tuple[str | None, str]:
    if now is None:
        now = get_shanghai_now()
    cur_t = now.time()

    for node_name, info in NODES.items():
        if info["duty_start"] <= cur_t < info["duty_end"]:
            return node_name, f"On duty ({info['duty_start'].strftime('%H:%M')} - {info['duty_end'].strftime('%H:%M')})"

    return None, "Off duty (Night window 23:58 - 08:30, all nodes idle)"


def run_cmd(cmd: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr

def is_local_cc15() -> bool:
    return os.path.exists(CC15_OVCLI_CONF)


def ssh_cmd(target: str, cmd_str: str, timeout: float = 30.0) -> tuple[int, str, str]:
    if target == CC15_HOST and is_local_cc15():
        return run_cmd(["bash", "-c", cmd_str], timeout=timeout)
    full_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, cmd_str]
    return run_cmd(full_cmd, timeout=timeout)


def get_cc15_active_url() -> str:
    if is_local_cc15():
        with open(CC15_OVCLI_CONF) as f:
            data = json.load(f)
        return str(data.get("url", ""))
    code, stdout, stderr = ssh_cmd(CC15_HOST, f"cat {CC15_OVCLI_CONF}")
    if code != 0:
        raise RuntimeError(f"Failed to read ovcli.conf on cc15: {stderr}")
    data = json.loads(stdout)
    return str(data.get("url", ""))


def set_cc15_active_url(new_url: str) -> None:
    if is_local_cc15():
        with open(CC15_OVCLI_CONF) as f:
            d = json.load(f)
        d["url"] = new_url
        with open(CC15_OVCLI_CONF, "w") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
        return
    code, _, stderr = ssh_cmd(
        CC15_HOST,
        f"python3 -c '\n"
        f"import json\n"
        f"p = \"{CC15_OVCLI_CONF}\"\n"
        f"with open(p) as f: d = json.load(f)\n"
        f"d[\"url\"] = \"{new_url}\"\n"
        f"with open(p, \"w\") as f: json.dump(d, f, indent=2)\n"
        f"print(\"ok\")\n"
        f"'",
    )
    if code != 0:
        raise RuntimeError(f"Failed to set ovcli.conf on cc15: {stderr}")

def _align_magpie_file(path: str, provider_id: str, expected_url: str) -> None:
    """Replace only the URL values in the top-level providers array."""
    with open(path, encoding="utf-8") as f:
        original = f.read()
        metadata = os.fstat(f.fileno())
    decoder = json.JSONDecoder()

    def whitespace(pos: int) -> int:
        while pos < len(original) and original[pos].isspace():
            pos += 1
        return pos

    def members(start: int):
        pos = whitespace(start + 1)
        while original[pos] != "}":
            key, pos = decoder.raw_decode(original, pos)
            pos = whitespace(pos)
            if original[pos] != ":":
                raise ValueError("Invalid JSON object")
            value_start = whitespace(pos + 1)
            value, pos = decoder.raw_decode(original, value_start)
            yield key, value, value_start, pos
            pos = whitespace(pos)
            if original[pos] == ",":
                pos = whitespace(pos + 1)
            else:
                break

    json.loads(original)  # Validate the whole document before changing it.
    providers = next((item for item in members(whitespace(0)) if item[0] == "providers"), None)
    target_start = None
    if providers and isinstance(providers[1], list):
        pos = whitespace(providers[2] + 1)
        while original[pos] != "]":
            entry_start = pos
            entry, pos = decoder.raw_decode(original, pos)
            if isinstance(entry, dict) and entry.get("id") == provider_id:
                target_start = entry_start
                break
            pos = whitespace(pos)
            if original[pos] == ",":
                pos = whitespace(pos + 1)
            else:
                break
    if target_start is None:
        print(f"[magpie-align] provider '{provider_id}' not found; skipping")
        return

    expected = {"chat": expected_url + "/v1", "responses": expected_url + "/v1",
                "anthropic": expected_url, "decide": expected_url + "/v1"}
    replacements = []
    seen = set()
    for key, value, start, end in members(target_start):
        seen.add(key)
        if key in expected and value != expected[key]:
            replacements.append((start, end, json.dumps(expected[key])))
    missing = expected.keys() - seen
    if missing:
        _, end = decoder.raw_decode(original, target_start)
        addition = "".join(
            ", " + json.dumps(key) + ": " + json.dumps(value)
            for key, value in expected.items() if key in missing
        )
        replacements.append((end - 1, end - 1, addition))
    if not replacements:
        return
    for start, end, value in reversed(replacements):
        original = original[:start] + value + original[end:]

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".magpie-align-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchown(f.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(f.fileno(), metadata.st_mode & 0o7777)
            f.write(original)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            os.unlink(temp_path)
    print(f"[magpie-align] {provider_id} -> {expected_url}")


def align_magpie_provider_url(node_name: str) -> None:
    expected_url = f"http://{NODES[node_name]['ip']}:{CC15_MAGPIE_API_PORT}"
    if is_local_cc15():
        _align_magpie_file(CC15_MAGPIE_PROVIDERS, CC15_MAGPIE_REMOTE_ID, expected_url)
        return
    code = ("import json, os, tempfile\n" + inspect.getsource(_align_magpie_file)
            + f"\n_align_magpie_file({CC15_MAGPIE_PROVIDERS!r}, {CC15_MAGPIE_REMOTE_ID!r}, {expected_url!r})\n")
    status, stdout, stderr = ssh_cmd(CC15_HOST, "python3 -c " + shlex.quote(code))
    if stdout:
        print(stdout, end="")
    if status:
        raise RuntimeError(f"Failed to align magpie provider URL on cc15: {stderr}")

def get_queue_status_counts(node_name: str) -> tuple[int, int]:
    """Query OpenViking queue observer API on target node via internal endpoint."""
    code, stdout, _ = ssh_cmd(
        node_name,
        "python3 -c '\n"
        "import urllib.request, json\n"
        "try:\n"
        "    conf = json.load(open(\"/app/.openviking/ovcli.conf\"))\n"
        "    key = conf.get(\"api_key\", \"\")\n"
        "except Exception:\n"
        "    key = \"\"\n"
        "req = urllib.request.Request(\"http://127.0.0.1:1933/api/v1/observer/queue\", headers={\"X-API-Key\": key})\n"
        "res = json.loads(urllib.request.urlopen(req, timeout=5).read())\n"
        "table = res.get(\"result\", {}).get(\"status\", \"\")\n"
        "total_line = [l for l in table.splitlines() if \"TOTAL\" in l]\n"
        "if total_line:\n"
        "    parts = [p.strip() for p in total_line[0].split(\"|\") if p.strip()]\n"
        "    print(f\"{parts[1]},{parts[2]}\")\n"
        "else:\n"
        "    print(\"-1,-1\")\n"
        "'\n",
    )
    if code == 0 and "," in stdout:
        try:
            p_str, ip_str = stdout.strip().split(",")
            return int(p_str), int(ip_str)
        except Exception:
            pass
    code, stdout, _ = ssh_cmd(node_name, "docker exec openviking ov status")
    if code == 0:
        total_line = [l for l in stdout.splitlines() if "TOTAL" in l]
        if total_line:
            parts = [p.strip() for p in total_line[0].split("|") if p.strip()]
            try:
                return int(parts[1]), int(parts[2])
            except Exception:
                pass
    return -1, -1


def wait_for_queue_drain(node_name: str, max_wait_sec: int = 180, poll_interval: int = 5) -> bool:
    """Poll until pending == 0 and in_progress == 0 on the node."""
    print(f"[*] Awaiting queue drain on {node_name} (timeout {max_wait_sec}s)...")
    start_t = time.monotonic()
    while time.monotonic() - start_t < max_wait_sec:
        pending, in_progress = get_queue_status_counts(node_name)
        if pending == 0 and in_progress == 0:
            print(f"[✓] Queue drained completely on {node_name} (pending=0, in_progress=0)")
            return True
        print(f"    Pending: {pending}, In-Progress: {in_progress}. Waiting {poll_interval}s...")
        time.sleep(poll_interval)
    return False

def check_node_health(node_name: str) -> dict[str, object]:
    info = NODES[node_name]
    ip = info["ip"]
    port = info["port"]
    # Probe via cc15 across WireGuard
    code, stdout, _ = ssh_cmd(
        CC15_HOST,
        f"curl -s -m 5 http://{ip}:{port}/api/v1/system/status",
    )
    is_healthy = False
    details: dict[str, object] = {}
    if code == 0:
        try:
            d = json.loads(stdout)
            # unauthenticated error with code UNAUTHENTICATED is also proof the service is up
            if d.get("status") == "ok" or (d.get("error", {}).get("code") == "UNAUTHENTICATED"):
                is_healthy = True
            details = d
        except (ValueError, KeyError, TypeError):
            pass

    return {
        "node": node_name,
        "ip": ip,
        "port": port,
        "healthy": is_healthy,
        "response": details,
    }


def execute_handoff(src_node: str, dst_node: str) -> dict[str, object]:
    print(f"[*] Starting controlled handoff: {src_node} -> {dst_node}")
    t0 = time.monotonic()
    report: dict[str, object] = {"src": src_node, "dst": dst_node}
    steps: dict[str, object] = {}
    report["steps"] = steps

    # 1. Prewarm & handshake target
    print(f"[*] Prewarming target {dst_node} (pinging hub to open NAT)...")
    _ = ssh_cmd(dst_node, "ping -c 2 10.144.10.1")
    _ = ssh_cmd(dst_node, "sudo wg set wg0 peer $(sudo wg show wg0 peers) persistent-keepalive 25")
    target_health = check_node_health(dst_node)
    steps["target_prewarm"] = target_health["healthy"]
    if not target_health["healthy"]:
        raise RuntimeError(f"Target node {dst_node} is not healthy, aborting handoff.")

    # 2. Record original URL for safe rollback
    original_url = get_cc15_active_url()
    dst_url = f"http://{NODES[dst_node]['ip']}:{NODES[dst_node]['port']}"

    # 3. Wait for in-flight tasks and queues to completely drain on source
    queue_clean = wait_for_queue_drain(src_node, max_wait_sec=180, poll_interval=5)
    steps["source_queue_clean"] = queue_clean
    if not queue_clean:
        raise RuntimeError(f"Queue on {src_node} failed to drain within timeout; handoff halted to protect data integrity.")

    # 4. Export backup from source (including dense vectors)
    print(f"[*] Exporting complete backup (with vectors) on source {src_node}...")
    t_export = time.monotonic()
    code, stdout, stderr = ssh_cmd(
        src_node,
        "docker exec openviking ov backup --include-vectors /tmp/handoff.ovpack && docker exec openviking sha256sum /tmp/handoff.ovpack",
    )
    if code != 0:
        raise RuntimeError(f"Backup export failed on {src_node}: {stderr}")
    src_sha256 = stdout.strip().split()[-2]
    export_duration = time.monotonic() - t_export
    steps["export_duration_sec"] = round(export_duration, 2)
    steps["src_sha256"] = src_sha256

    # 5. Stream backup to target
    print(f"[*] Streaming backup package from {src_node} to {dst_node}...")
    pipe_cmd = (
        f"ssh {src_node} 'docker exec openviking base64 -w 0 /tmp/handoff.ovpack' | "
        f"ssh {dst_node} 'docker exec -i openviking bash -c \"base64 -d > /tmp/handoff.ovpack && sha256sum /tmp/handoff.ovpack\"'"
    )
    proc = subprocess.run(pipe_cmd, shell=True, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Package stream failed: {proc.stderr}")
    dst_sha256 = proc.stdout.strip().split()[0]
    steps["dst_sha256"] = dst_sha256
    steps["sha256_match"] = (src_sha256 == dst_sha256)
    if src_sha256 != dst_sha256:
        raise RuntimeError(f"SHA256 mismatch! {src_sha256} vs {dst_sha256}")

    # 6. Restore on target with vector auto-restore
    print(f"[*] Restoring package on {dst_node} with vector verification...")
    code, _, stderr = ssh_cmd(
        dst_node,
        "docker exec openviking ov restore /tmp/handoff.ovpack --on-conflict overwrite --vector-mode auto",
    )
    if code != 0:
        raise RuntimeError(f"Restore failed on {dst_node}: {stderr}")
    steps["restore_ok"] = True

    # 7. Verify target health and queue stability
    print(f"[*] Verifying target {dst_node} after restore...")
    post_health = check_node_health(dst_node)
    if not post_health["healthy"]:
        print(f"[!] Target verification failed on {dst_node}! Reverting cc15 URL to {original_url}...")
        set_cc15_active_url(original_url)
        raise RuntimeError(f"Target verification failed on {dst_node}, cutover reverted.")
    steps["post_restore_verify"] = True

    # 8. Switch traffic on cc15 to destination
    print(f"[*] Switching cc15 active URL to {dst_url}...")
    set_cc15_active_url(dst_url)
    steps["switched_url"] = dst_url

    # 9. Manage keepalives for natural idle: target=25, source=0
    print("[*] Updating keepalives: target=25, source=0 (natural idle)...")
    _ = ssh_cmd(dst_node, "sudo wg set wg0 peer $(sudo wg show wg0 peers) persistent-keepalive 25")
    _ = ssh_cmd(src_node, "sudo wg set wg0 peer $(sudo wg show wg0 peers) persistent-keepalive 0")
    steps["keepalive_updated"] = True

    # 10. Clean temp files
    _ = ssh_cmd(src_node, "docker exec openviking rm -f /tmp/handoff.ovpack")
    _ = ssh_cmd(dst_node, "docker exec openviking rm -f /tmp/handoff.ovpack")

    total_duration = time.monotonic() - t0
    report["total_duration_sec"] = round(total_duration, 2)
    print(f"[✓] Handoff completed successfully in {report['total_duration_sec']}s")
    return report


def execute_night_backup() -> dict[str, object]:
    """Execute end-of-day 23:00 state preservation."""
    print("[*] Starting end-of-day overnight backup & hibernation...")
    active_url = get_cc15_active_url()
    active_node = None
    for n, info in NODES.items():
        if f"http://{info['ip']}:{info['port']}" == active_url:
            active_node = n
            break

    if not active_node:
        active_node = "instabox-suaily"

    print(f"[*] Active node for night backup: {active_node}")

    # Wait for queue drain before night backup
    drain_ok = wait_for_queue_drain(active_node, max_wait_sec=120, poll_interval=5)
    if not drain_ok:
        print(f"[!] Warning: Queue on {active_node} did not drain completely before night backup.")

    # Ensure directory on cc15
    _ = ssh_cmd(CC15_HOST, "mkdir -p /opt/dockers/magpie/config/home/.openviking/backups")

    # Export on active node with dense vectors
    code, stdout, stderr = ssh_cmd(
        active_node,
        "docker exec openviking ov backup --include-vectors /tmp/night_final.ovpack && docker exec openviking sha256sum /tmp/night_final.ovpack",
    )
    if code != 0:
        raise RuntimeError(f"Night backup export failed on {active_node}: {stderr}")
    src_sha = stdout.strip().split()[-2]

    # Stream to cc15 archive
    if is_local_cc15():
        pipe_cmd = (
            f"ssh {active_node} 'docker exec openviking base64 -w 0 /tmp/night_final.ovpack' | "
            f"base64 -d > {CC15_BACKUP_PATH} && sha256sum {CC15_BACKUP_PATH}"
        )
    else:
        pipe_cmd = (
            f"ssh {active_node} 'docker exec openviking base64 -w 0 /tmp/night_final.ovpack' | "
            f"ssh {CC15_HOST} 'base64 -d > {CC15_BACKUP_PATH} && sha256sum {CC15_BACKUP_PATH}'"
        )
    proc = subprocess.run(pipe_cmd, shell=True, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Overnight archive copy failed: {proc.stderr}")
    dst_sha = proc.stdout.strip().split()[0]
    if src_sha != dst_sha:
        raise RuntimeError(f"Night backup SHA256 mismatch! {src_sha} vs {dst_sha}")
    print(f"[✓] Night state preserved on cc15 with verified SHA256: {dst_sha}")

    # Set keepalive=0 on all nodes so they enter natural idle during the night window
    print("[*] Setting persistent-keepalive=0 across all nodes for night sleep...")
    for n in NODES:
        _ = ssh_cmd(n, "sudo wg set wg0 peer $(sudo wg show wg0 peers) persistent-keepalive 0")

    # Cleanup temp
    _ = ssh_cmd(active_node, "docker exec openviking rm -f /tmp/night_final.ovpack")

    return {
        "status": "archived",
        "archive_node": active_node,
        "cc15_path": CC15_BACKUP_PATH,
        "sha256": dst_sha,
    }



def execute_morning_restore() -> dict[str, object]:
    """Execute 08:25 morning service initialization on instabox-suai."""
    target_node = "instabox-suai"
    print(f"[*] Starting morning service restoration on {target_node}...")

    # Check cc15 backup archive exists and get sha256
    code, stdout, stderr = ssh_cmd(CC15_HOST, f"sha256sum {CC15_BACKUP_PATH}")
    if code != 0:
        raise RuntimeError(f"Overnight backup archive missing on cc15: {stderr}")
    cc15_sha = stdout.strip().split()[0]

    # Prewarm & handshake
    _ = ssh_cmd(target_node, "ping -c 2 10.144.10.1")
    _ = ssh_cmd(target_node, "sudo wg set wg0 peer $(sudo wg show wg0 peers) persistent-keepalive 25")

    # Stream backup from cc15 to target_node
    if is_local_cc15():
        pipe_cmd = (
            f"base64 -w 0 {CC15_BACKUP_PATH} | "
            f"ssh {target_node} 'docker exec -i openviking bash -c \"base64 -d > /tmp/morning.ovpack && sha256sum /tmp/morning.ovpack\"'"
        )
    else:
        pipe_cmd = (
            f"ssh {CC15_HOST} 'base64 -w 0 {CC15_BACKUP_PATH}' | "
            f"ssh {target_node} 'docker exec -i openviking bash -c \"base64 -d > /tmp/morning.ovpack && sha256sum /tmp/morning.ovpack\"'"
        )
    proc = subprocess.run(pipe_cmd, shell=True, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Morning package stream failed: {proc.stderr}")
    node_sha = proc.stdout.strip().split()[0]
    if cc15_sha != node_sha:
        raise RuntimeError(f"Morning package SHA256 mismatch! {cc15_sha} vs {node_sha}")

    # Restore with vector auto-restore
    code, _, stderr = ssh_cmd(
        target_node,
        "docker exec openviking ov restore /tmp/morning.ovpack --on-conflict overwrite --vector-mode auto",
    )
    if code != 0:
        raise RuntimeError(f"Morning restore failed: {stderr}")

    # Clean temp on target
    _ = ssh_cmd(target_node, "docker exec openviking rm -f /tmp/morning.ovpack")

    # Switch cc15 URL
    target_url = f"http://{NODES[target_node]['ip']}:{NODES[target_node]['port']}"
    set_cc15_active_url(target_url)
    print(f"[✓] Morning service restored on {target_node}, routed to {target_url}")

    return {
        "status": "restored",
        "duty_node": target_node,
        "active_url": target_url,
        "sha256": node_sha,
    }

def main() -> None:
    parser = argparse.ArgumentParser(description="OpenViking 3-Node Rotation Orchestrator")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    _ = subparsers.add_parser("plan", help="Show rotation schedule and current duty status")
    _ = subparsers.add_parser("status", help="Inspect all nodes and active cc15 routing")

    handoff_p = subparsers.add_parser("handoff", help="Execute handoff between two nodes")
    _ = handoff_p.add_argument("--from", dest="src", required=True, choices=list(NODES.keys()))
    _ = handoff_p.add_argument("--to", dest="dst", required=True, choices=list(NODES.keys()))

    _ = subparsers.add_parser("night-backup", help="Execute 23:00 night backup to cc15 archive")
    _ = subparsers.add_parser("morning-restore", help="Execute 08:25 morning restore on first node")
    _ = subparsers.add_parser("tick", help="Periodic scheduler tick: checks duty and triggers handoff if needed")

    args = parser.parse_args()

    if args.subcommand == "plan":
        now = get_shanghai_now()
        duty_node, duty_msg = determine_current_duty_node(now)
        print("=== OpenViking 3-Node Rotation Plan ===")
        print(f"Current Shanghai Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print("Active Duty Window: 08:30 - 23:58 (15.5 hours)")
        print(f"Current Duty Status: {duty_msg}")
        print(f"Current Designated Node: {duty_node or 'None (Night Idle)'}\n")
        print("Schedule:")
        for node_name, info in NODES.items():
            active_marker = " -> [ACTIVE]" if node_name == duty_node else ""
            start_s = info["duty_start"].strftime("%H:%M")
            end_s = info["duty_end"].strftime("%H:%M")
            print(f"  - {node_name:<16}: {start_s} - {end_s} ({info['ip']}:{info['port']}){active_marker}")

    elif args.subcommand == "status":
        print("=== Cluster Status ===")
        active_url = get_cc15_active_url()
        print(f"cc15 Active URL: {active_url}")
        for node_name, info in NODES.items():
            expected_url = f"http://{info['ip']}:{info['port']}"
            is_active = expected_url == active_url
            h = check_node_health(node_name)
            status_str = "ACTIVE" if is_active else "STANDBY"
            health_str = "HEALTHY" if h["healthy"] else "DOWN/SLEEPING"
            print(f"  - {node_name:<16} ({info['ip']}): [{status_str}] [{health_str}]")

    elif args.subcommand == "handoff":
        res = execute_handoff(str(args.src), str(args.dst))
        print(json.dumps(res, indent=2))

    elif args.subcommand == "night-backup":
        res = execute_night_backup()
        print(json.dumps(res, indent=2))

    elif args.subcommand == "morning-restore":
        res = execute_morning_restore()
        print(json.dumps(res, indent=2))

    elif args.subcommand == "tick":
        now = get_shanghai_now()
        expected_node, msg = determine_current_duty_node(now)
        active_url = get_cc15_active_url()
        print(f"Tick at {now.strftime('%H:%M:%S')}: {msg}")
        if expected_node is None:
            print("Night window: all nodes idle.")
            return

        expected_url = f"http://{NODES[expected_node]['ip']}:{NODES[expected_node]['port']}"
        try:
            if active_url == expected_url:
                print(f"Already routed to expected duty node: {expected_node}")
            else:
                current_node = None
                for n, info in NODES.items():
                    if f"http://{info['ip']}:{info['port']}" == active_url:
                        current_node = n
                        break
                if current_node:
                    print(f"Triggering duty rotation: {current_node} -> {expected_node}")
                    execute_handoff(current_node, expected_node)
                else:
                    print(f"Setting initial active node: {expected_node}")
                    set_cc15_active_url(expected_url)
        finally:
            handoff_failed = sys.exc_info()[0] is not None
            try:
                align_magpie_provider_url(expected_node)
            except Exception as exc:
                print(f"[magpie-align] failed: {exc}")
                if not handoff_failed:
                    raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
