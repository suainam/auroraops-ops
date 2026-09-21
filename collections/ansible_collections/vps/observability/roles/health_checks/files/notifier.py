#!/usr/bin/env python3

import os
import time
import sys
import json
import logging
import socket
import re
import requests
import redis
import requests.packages.urllib3.util.connection as connection

_orig_create_connection = connection.create_connection

def patched_create_connection(address, *args, **kwargs):
    """Disable IPv6 for requests."""
    host, port = address
    if host.startswith('['):
        host = host.strip('[]')
    return _orig_create_connection((host, port), *args, **kwargs)

connection.create_connection = patched_create_connection

import argparse

# --- Configuration ---
SCRIPT_VERSION = "1.7.3"
LOG_FILE = "/var/log/health_checks/notifier.log"
API_ENDPOINT = "https://moe.saui.dpdns.org/api/push"
REDIS_QUEUE_KEY = "health_checks:alert_queue"
ALERT_SUPPRESSION_KEY_PREFIX = "health_checks:last_alert:"
ALERT_SUPPRESSION_INTERVAL = 4 * 3600  # 4 hours

# Global Redis connection pool
_redis_pool = None

def get_redis_client():
    global _redis_pool
    if _redis_pool is None:
        redis_host = get_env_var("REDIS_HOST")
        redis_port = int(get_env_var("REDIS_PORT"))
        redis_password = get_env_var("REDISCLI_AUTH")
        _redis_pool = redis.ConnectionPool(
            host=redis_host, 
            port=redis_port, 
            password=redis_password, 
            decode_responses=True
        )
    return redis.Redis(connection_pool=_redis_pool)

# Ensure every LogRecord has hostname and script_name to avoid Formatter KeyError
_original_log_record_factory = logging.getLogRecordFactory()
def _record_factory(*args, **kwargs):
    record = _original_log_record_factory(*args, **kwargs)
    if not hasattr(record, "hostname"):
        record.hostname = socket.gethostname()
    if not hasattr(record, "script_name"):
        record.script_name = SCRIPT_NAME
    return record
logging.setLogRecordFactory(_record_factory)

SCRIPT_NAME = os.path.basename(__file__) if '__file__' in globals() else 'notifier'
class HostnameFilter(logging.Filter):
    def filter(self, record):
        record.hostname = getattr(record, 'hostname', socket.gethostname())
        record.script_name = getattr(record, 'script_name', SCRIPT_NAME)
        return True

log_formatter = logging.Formatter(
    '%(asctime)s [%(hostname)s] [%(script_name)s] [%(process)d] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ'
)

from logging.handlers import RotatingFileHandler
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
file_handler.setFormatter(log_formatter)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
root_logger.setLevel(log_level)
if root_logger.handlers:
    root_logger.handlers = []
root_logger.addHandler(file_handler)
root_logger.addHandler(stderr_handler)
root_logger.addFilter(HostnameFilter())

logger = logging.getLogger(__name__)

def get_env_var(name: str, required: bool = True, default=None):
    value = os.environ.get(name)
    if required and value is None:
        logger.critical(f"Required environment variable {name} is not set. Exiting.")
        sys.exit(1)
    return value if value is not None else default

def escape_markdown_v2(text: str) -> str:
    """
    语义化替换 Telegram MarkdownV2 保留字符，完全避免使用反斜杠转义。
    这是因为 API 代理服务器可能无法正确处理 JSON 中的反斜杠。
    """
    if not isinstance(text, str):
        text = str(text)

    original_text = text

    # 使用视觉相似的全角字符或非保留字符进行替换
    replacements = {
        ".": "․",
        "_": "ˍ",
        "[": "［",
        "]": "］",
        "(": "（",
        ")": "）",
        "!": "！",
        "-": "－",
        "=": "＝",
        "+": "＋",
        "|": "｜",
        "*": "＊",
        "~": "～",
        "`": "＇",
        ">": "＞",
        "#": "＃",
        "{": "｛",
        "}": "｝",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    if original_text != text:
        logger.debug(f"MarkdownV2 Escaping (Semantic Only):\n  BEFORE: {original_text}\n  AFTER : {text}")

    return text

def simplify_service_name(service: str) -> str:
    """
    简化服务名称，使其更清晰更短。
    """
    if not service:
        return "unknown"

    # cpu_usage -> cpu
    # memory_usage -> memory
    # network_interfaces -> network
    if "_" in service:
        parts = service.split("_")
        if len(parts) >= 2:
            if parts[1] == "usage":
                return parts[0]
            if parts[0] == "network" and parts[1] == "interfaces":
                return "network"
            if parts[0] == "system" and parts[1] == "audit":
                return "audit"
            if parts[0] == "tcp" and parts[1] == "connections":
                return "tcp"
            if parts[0] == "backups" and parts[1] == "rclone":
                return "backups"
            if parts[0] == "disk" and parts[1].startswith("usage"):
                return parts[0]

    return service

def format_service_status(item: dict) -> str:
    """
    格式化服务状态，优先显示 metrics 中的实际数值。
    """
    service = item.get('service', '')
    message = item.get('message', '')
    metrics = item.get('metrics', {})
    logger.debug(f"format_service_status: service={service}, metrics={metrics}, message={message}")
    
    # Memory: 1.4G/5G (usage_percent: 81.31%)
    if 'memory' in service.lower():
        if metrics.get('total_kb') and metrics.get('avail_kb'):
            total_kb = metrics['total_kb']
            avail_kb = metrics['avail_kb']
            used_kb = total_kb - avail_kb
            total_gb = total_kb / 1024 / 1024
            used_gb = used_kb / 1024 / 1024
            return f"Memory: {used_gb:.1f}G/{total_gb:.1f}G"
        return optimize_message(message)
    
    # Swap: 1.4G/5G
    if 'swap' in service.lower():
        if metrics.get('total_kb') and metrics.get('used_kb'):
            total_kb = metrics['total_kb']
            used_kb = metrics['used_kb']
            total_gb = total_kb / 1024 / 1024
            used_gb = used_kb / 1024 / 1024
            return f"Swap: {used_gb:.1f}G/{total_gb:.1f}G"
        return optimize_message(message)
    
    # Disk: 35G/200G
    if 'disk' in service.lower() and 'disk_io' not in service.lower():
        if metrics.get('usage') is not None:
            usage_percent = metrics['usage']
            return f"Disk: {usage_percent:.1f}%"
        return optimize_message(message)
    
    # File descriptors: 1.6K/1024K
    if 'file_descriptor' in service.lower():
        if metrics.get('used') and metrics.get('ref_limit'):
            used = metrics['used']
            limit = metrics['ref_limit']
            # Format numbers with K suffix
            if used >= 1000000:
                used_str = f"{used/1000000:.1f}M"
            elif used >= 1000:
                used_str = f"{used/1000:.1f}K"
            else:
                used_str = str(used)
            
            if limit >= 1000000:
                limit_str = f"{limit/1000000:.0f}M"
            elif limit >= 1000:
                limit_str = f"{limit/1000:.0f}K"
            else:
                limit_str = str(limit)
            
            return f"File descriptors: {used_str}/{limit_str}"
        return optimize_message(message)
    
    # Disk IO: 0.5ms (only show value from metrics, no "is normal")
    if 'disk_io' in service.lower():
        if metrics and isinstance(metrics, dict):
            # Get max await from all devices
            max_await = 0.0
            for device, stats in metrics.items():
                if isinstance(stats, dict) and 'await' in stats:
                    max_await = max(max_await, stats['await'])
            return f"Disk IO: {max_await:.1f}ms"
        return optimize_message(message)
    
    # Backups: 去掉调用方已经补充的标题前缀，避免显示为 "Backups: Backups: ..."
    if 'backup' in service.lower() and message.startswith("Backups: "):
        return message.removeprefix("Backups: ")

    # 其他服务使用原来的 optimize_message
    return optimize_message(message)

def optimize_message(msg: str) -> str:
    """
    优化消息格式，移除或简化括号内的详细信息，使消息更紧凑。
    保留关键数值信息，移除冗余的详细信息。
    """
    if not isinstance(msg, str):
        return str(msg)

    # Audit: [ReqReboot: no] [SecUpdates(24h): 1] [FailedLogins(1h): 0] [LastBoot: 2026-01-16 04:00]
    # → Boot=OK, Updates=1, Logins=0 (移除 "Audit:" 前缀，由调用方添加)
    if msg.startswith("Audit:"):
        req_reboot = "OK" if "ReqReboot: no" in msg else "Required"
        sec_updates = re.search(r'SecUpdates\(24h\): (\d+)', msg)
        failed_logins = re.search(r'FailedLogins\(1h\): (\d+)', msg)
        updates = sec_updates.group(1) if sec_updates else "0"
        logins = failed_logins.group(1) if failed_logins else "0"
        return f"Boot={req_reboot}, Updates={updates}, Logins={logins}"

    # Physical(eth0):[up](v4:74.48.35.9,...) | ZeroTier...
    # → eth0:up(74.48.35.9) | zt:up(10.147.20.149) | docker:up(bridge)
    if "Physical" in msg or "ZeroTier" in msg or "Docker" in msg:
        parts = msg.split(" | ")
        simplified = []
        for part in parts:
            if part.startswith("Physical"):
                m = re.search(r'Physical\(([^)]+)\):\[([^\]]+)\]\([^)]*v4:([\d.]+)', part)
                if m:
                    name = m.group(1)
                    state = m.group(2)
                    ip = m.group(3)
                    simplified.append(f"{name}:{state}({ip})")
            elif part.startswith("ZeroTier"):
                m = re.search(r'ZeroTier\(([^)]+)\):\[([^\]]+)\]\([^)]*v4:([\d.]+)', part)
                if m:
                    name = "zt"
                    state = m.group(2)
                    ip = m.group(3)
                    simplified.append(f"{name}:{state}({ip})")
            elif "Docker(" in part and "Virtual" not in part and "docker0" not in part:
                # Docker bridge 接口，简化为 docker
                m = re.search(r'Docker\([^)]+\):\[([^\]]+)\]', part)
                if m and m.group(1) != "down":
                    # 如果有 IP 地址，显示 IP
                    m2 = re.search(r'\(v4:([\d.]+)\)', part)
                    ip = f"({m2.group(1)})" if m2 else "(bridge)"
                    simplified.append(f"docker:{m.group(1)}{ip}")
        return " | ".join(simplified) if simplified else msg

    # Backups(24h): Local=28, Cloud=[gdrive:28 onedrive:28] [Note: '_current' counts only if changed]
    # → Local=28, gdrive=28, onedrive=28 (移除 "Backups:" 前缀，由调用方添加)
    if msg.startswith("Backups"):
        m = re.search(r'Backups\([^)]+\): Local=(\d+), Cloud=\[([^\]]+)\]', msg)
        if m:
            local = m.group(1)
            cloud_info = m.group(2)
            clouds = []
            for cloud_part in cloud_info.split():
                if ":" in cloud_part:
                    name, count = cloud_part.split(":")
                    clouds.append(f"{name}={count}")
            return f"Local={local}, {', '.join(clouds)}"

    # File descriptors: 1728 used (RefLimit: 1048576).
    # → File descriptors: 2K
    if "File descriptors" in msg:
        m = re.search(r'File descriptors: (\d+) used', msg)
        if m:
            used = int(m.group(1))
            if used >= 1000000:
                used_str = f"{used/1000000:.1f}M"
            elif used >= 1000:
                used_str = f"{used/1000:.0f}K"
            else:
                used_str = str(used)
            return f"File descriptors: {used_str}"

    # CPU usage is normal (13.57%).
    # → CPU: 13.6%
    if "CPU usage" in msg:
        m = re.search(r'\((\d+\.\d+)%\)', msg)
        if m:
            percent = float(m.group(1))
            return f"CPU: {percent:.1f}%"

    # Memory usage is normal.
    # → Memory: 81.31% (如果有百分比)
    # → Memory: OK (如果没有百分比)
    if "Memory usage" in msg:
        m = re.search(r'\((\d+\.\d+)%\)', msg)
        if m:
            percent = m.group(1)
            return f"Memory: {percent}%"
        return "Memory: OK"

    # Disk IO is normal (Max await: 0ms).
    # → Disk IO: 0.0ms
    if "Disk IO" in msg:
        m = re.search(r'Max await: (\d+)ms', msg)
        if m:
            await_ms = float(m.group(1))
            return f"Disk IO: {await_ms:.1f}ms"
        m = re.search(r'\((\d+\.\d+)ms\)', msg)
        if m:
            await_ms = float(m.group(1))
            return f"Disk IO: {await_ms:.1f}ms"

    # System load: 0.59 / 2 CPUs (1m:0.74, 5m:0.85, 15m:0.59).
    # → System load: 0.59/2 CPUs (使用 15m 负载，与告警逻辑一致)
    if "System load" in msg:
        m = re.search(r'System load: ([\d.]+) / (\d+) CPUs \(1m:([\d.]+), 5m:([\d.]+), 15m:([\d.]+)\)\.', msg)
        if m:
            load_15m, n_cpu = m.group(1), m.group(2)
            return f"System load: {load_15m}/{n_cpu} CPUs"

    # TCP Summary: ESTAB:36, FIN-WAIT-2:1, LISTEN:21, SYN-RECV:9, TIME-WAIT:13
    # → ESTAB:36, FIN-WAIT-2:1, LISTEN:21 (移除 "TCP:" 前缀，由调用方添加)
    if msg.startswith("TCP Summary:"):
        return msg.replace("TCP Summary: ", "")

    # Swap usage is normal (0.1G/4.3G).
    # → Swap: 0.1G/4.3G
    if "Swap usage" in msg:
        # 匹配括号内包含 X.XG/X.XG 格式的内容
        m_val = re.search(r'\((?:[^)]*,\s*)?(\d+\.?\d*[GMK]/[\d.]+[^)]*)\)', msg)
        if m_val:
            return f"Swap: {m_val.group(1)}"
        
        # 兼容纯百分比格式
        m_pct = re.search(r'\((\d+\.?\d*)%\)', msg)
        if m_pct:
            return f"Swap: {m_pct.group(1)}%"
        
        return "Swap: OK"

    # Disk usage is normal.
    # → Disk: 17.6%
    if "Disk usage" in msg and "Disk IO" not in msg:
        m = re.search(r'Disk usage is (?:critically )?(?:high )?(?:normal|ok)(?: \((\d+\.?\d*)%\)\.)?', msg)
        if m and m.group(1):
            percent = m.group(1)
            return f"Disk: {percent}%"
        return "Disk: OK"

    # Total processes: 153
    # → Processes: 153
    if msg.startswith("Total processes:"):
        m = re.search(r'Total processes: (\d+)', msg)
        if m:
            return f"Processes: {m.group(1)}"

    # No OOM events detected.
    # → OOM: None
    if "OOM" in msg:
        m = re.search(r'Detected (\d+) OOM', msg)
        if m:
            return f"OOM: {m.group(1)}"
        return "OOM: None"

    # NTP is synchronized.
    # → NTP: OK
    if "NTP" in msg:
        return "NTP: OK"

    # ssh is running.
    # docker is running.
    # nginx is running.
    # redis is running.
    # → SSH: ✓, Docker: ✓, etc.
    if " is running." in msg:
        service = msg.split()[0].lower()
        # SSH -> SSH, docker -> Docker (proper capitalization)
        if service == "ssh":
            return f"SSH: ✓"
        if service == "docker":
            return f"Docker: ✓"
        if service == "nginx":
            return f"Nginx: ✓"
        if service == "redis":
            return f"Redis: ✓"
        return f"{service.capitalize()}: ✓"

    # 其他情况，移除圆括号（但保留方括号，因为它们被escape_markdown_v2处理）
    # 对于 OK 状态的消息，保持简洁
    if "is normal" in msg or "is synchronized" in msg or "is running" in msg:
        return re.sub(r'\([^)]*\)', '', msg).strip('. ')

    return msg

def transform_for_notify2(input_data: dict) -> dict:
    hostname_for_title = input_data.get("host", "N/A").replace('_', ' ')
    service_name_for_title = input_data.get("service", "N/A").replace('_', ' ')
    status = input_data.get("status", "N/A").upper()
    title = f"[{status}] Alert for {service_name_for_title} on {hostname_for_title}"
    description = escape_markdown_v2(input_data.get("message", "No message provided."))
    timestamp_raw = input_data.get('timestamp', 'N/A')
    timestamp_formatted = timestamp_raw.replace('-', '').replace('T', '') if 'T' in timestamp_raw else timestamp_raw
    metrics_data = input_data.get('metrics', {})
    metrics_string = ""
    if "usage_percent" in metrics_data:
        metrics_string = f"usage: {metrics_data['usage_percent']} percent"
    content = f"Timestamp: {timestamp_formatted} Metrics: {metrics_string}"
    return {"title": title, "description": description, "content": content}

def transform_for_notify1(input_data: dict) -> dict:
    hostname_for_title = input_data.get("host", "N/A").replace('_', ' ')
    status = input_data.get("status", "N/A").upper()
    title = f"[{status}] Health Heartbeat on {hostname_for_title}"
    ip_address = input_data.get('ip_address', 'N/A')
    content = escape_markdown_v2(input_data.get("message", "No message provided."))
    return {"title": title, "ip": ip_address, "content": content}

def push_to_api(api_key: str, json_data: dict) -> bool:
    endpoint = f"{API_ENDPOINT}/{api_key}"
    logger.info(f"Attempting to push data to API endpoint: {endpoint}.")
    logger.debug(f"Pushing transformed JSON payload: {json.dumps(json_data)}")
    if not api_key:
        logger.error(f"API Key is not defined for endpoint: {endpoint}. Cannot push data.")
        return False
    try:
        response = requests.post(endpoint, json=json_data, timeout=10)
        if response.status_code == 200:
            logger.info(f"Successfully pushed data to API. HTTP Status: {response.status_code}")
            return True
        else:
            logger.error(f"Failed to push data to API. HTTP Status: {response.status_code}. Response: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Request to API failed: {e}. Enqueuing to Redis.")
        return False

def enqueue_to_redis(api_key: str, original_json_data: dict):
    try:
        r = get_redis_client()
        queued_item = {
            "api_key": api_key,
            "payload": original_json_data,
            "retries": 0
        }
        r.rpush(REDIS_QUEUE_KEY, json.dumps(queued_item))
        logger.info("Original payload and API Key successfully enqueued to Redis.")
    except Exception as e:
        logger.critical(f"Failed to enqueue payload to Redis: {e}.")

def detect_threshold_alerts(item: dict, thresholds: dict) -> list:
    alerts = []
    metrics = item.get("metrics", {}) or {}
    for metric_name, thresh in thresholds.items():
        if metric_name in metrics:
            try:
                value = float(metrics[metric_name])
                if value >= float(thresh):
                    alerts.append({
                        "host": item.get("host"),
                        "service": item.get("service"),
                        "metric": metric_name,
                        "value": value,
                        "threshold": float(thresh),
                        "status": item.get("status", "unknown"),
                        "message": item.get("message", ""),
                        "timestamp": item.get("timestamp")
                    })
            except: continue
    return alerts

def filter_suppressed_alerts(alerts: list) -> list:
    if not alerts: return []
    r = get_redis_client()
    filtered = []
    now = time.time()
    for alert in alerts:
        service = alert.get("service")
        metric = alert.get("metric", "status")
        status = alert.get("status")
        message = alert.get("message")
        key = f"{ALERT_SUPPRESSION_KEY_PREFIX}{service}:{metric}"
        last_alert_raw = r.get(key)
        should_send = True
        if last_alert_raw:
            try:
                last_alert = json.loads(last_alert_raw)
                if status == last_alert.get("status") and message == last_alert.get("message"):
                    if now - last_alert.get("timestamp", 0) < ALERT_SUPPRESSION_INTERVAL:
                        should_send = False
            except: pass
        if should_send:
            filtered.append(alert)
            r.set(key, json.dumps({"status": status, "message": message, "timestamp": now}))
    return filtered

def is_within_alert_window(window_str: str) -> bool:
    if not window_str or window_str.lower() == 'all': return True
    try:
        now_hour = time.localtime().tm_hour
        for window in window_str.split(','):
            if '-' in window:
                start, end = map(int, window.split('-'))
                if start <= now_hour < end: return True
            elif now_hour == int(window): return True
    except: return True
    return False

def aggregate_and_push(items: list, notify1_api_key: str, notify2_api_key: str):
    if not items: return
    now = time.localtime()
    total_minutes = now.tm_hour * 60 + now.tm_min
    heartbeat_interval = int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES", "60"))
    is_heartbeat_time = (0 <= (total_minutes % heartbeat_interval) <= 10) if heartbeat_interval > 0 else False

    thresholds_env = os.environ.get("THRESHOLDS_JSON", '{"usage_percent":90}')
    try:
        thresholds = json.loads(thresholds_env)
    except:
        thresholds = {"usage_percent": 90}

    heartbeats = []
    threshold_alerts_map = {}
    hosts_seen = set()
    self_healing_services = []

    for item in items:
        if item.get("service") == "__self_healing_config__":
            sh_data = item.get("metrics", {}).get("services", [])
            if sh_data:
                self_healing_services = sh_data
            continue
        host = item.get("host")
        if host: hosts_seen.add(host)
        status = (item.get("status") or "").lower()
        service = item.get("service")
        ts_raw = item.get("timestamp", "")
        ts_formatted = ts_raw.replace('-', '') if isinstance(ts_raw, str) else ts_raw

        if status == "ok":
            heartbeats.append({
                "host": host,
                "service": service,
                "message": item.get("message", ""),
                "timestamp": ts_formatted,
                "ip": item.get("ip_address") or item.get("ip"),
                "metrics": item.get("metrics", {})
            })
        else:
            alert_key = (host, service)
            threshold_alerts_map[alert_key] = {
                "host": host, "service": service, "metric": "status", "value": status,
                "threshold": "ok", "status": status, "message": item.get("message", ""), "timestamp": ts_formatted
            }

        alerts = detect_threshold_alerts(item, thresholds)
        if alerts:
            for a in alerts:
                if isinstance(a.get("timestamp"), str): a["timestamp"] = a["timestamp"].replace('-', '')
                if not a.get("host") and host: a["host"] = host
                threshold_alerts_map[(a.get("host"), a.get("service"))] = a

    all_alerts = list(threshold_alerts_map.values())
    filtered_alerts = filter_suppressed_alerts(all_alerts) if all_alerts else []

    if filtered_alerts and notify2_api_key:
        alert_window = os.environ.get("ALERT_WINDOW_HOURS", "8-12,14-18")
        if is_within_alert_window(alert_window):
            n_alerts = len(filtered_alerts)
            title = f"[ALERT] {n_alerts} issues on {next(iter(hosts_seen)) if hosts_seen else 'unknown'}"
            description = f"Critical anomalies detected during health check."
            rows = [f"⚠️ {n_alerts} issues", "", "⚠ Alerts"]
            for a in filtered_alerts:
                msg = optimize_message(a.get('message', ''))
                rows.append(f"❌ {msg}")
            payload = {
                "title": escape_markdown_v2(title), 
                "description": escape_markdown_v2(description), 
                "content": escape_markdown_v2("\n".join(rows))
            }
            logger.info("Pushing ALERT payload with %d anomalies", n_alerts)
            if not push_to_api(notify2_api_key, payload): enqueue_to_redis(notify2_api_key, payload)
        
        if not is_heartbeat_time: return

    if is_heartbeat_time and heartbeats and notify1_api_key:
        n_ok = len(heartbeats)
        n_err = len(all_alerts)
        status_tag = "OK" if n_err == 0 else "DEGRADED"
        title = f"[{status_tag}] Health Heartbeat from {next(iter(hosts_seen)) if hosts_seen else 'unknown'}"
        ips = sorted({h.get("ip") for h in heartbeats if h.get("ip")})
        ip_field = ",".join(ips) if ips else "N/A"
        rows = [f"🟠 {n_err} issues" if n_err > 0 else "✓ All healthy", ""]
        if all_alerts:
            rows.append("⚠ Anomalies")
            for a in all_alerts:
                msg = optimize_message(a.get('message', ''))
                rows.append(f"❌ {msg}")
            rows.append("")
        rows.append("── Status ──")
        
        # 按照 README 文档的排序：系统核心 -> 网络/审计 -> 备份 -> 服务
        resource_services = []  # 系统核心: CPU, Memory, Swap, Disk, Load, etc.
        network_services = []   # 网络: network_interfaces, tcp_connections
        audit_services = []     # 审计: system_audit
        backup_services = []    # 备份: backups_rclone
        daemon_services = []    # 服务: SSH, Docker, Nginx, Redis, etc.
        
        for h in heartbeats:
            service = h.get('service', '')
            service_simple = simplify_service_name(service)
            logger.debug(f"Processing heartbeat: service={service}, metrics={h.get('metrics', {})}")

            # 分类服务（按 README 定义的优先级）
            if any(x in service.lower() for x in ['cpu', 'memory', 'swap', 'disk', 'load', 'process', 'file_descriptor', 'oom']):
                # Disk IO 特殊处理：只在有实际活动时显示
                if 'disk_io' in service.lower():
                    metrics = h.get('metrics', {})
                    if metrics and isinstance(metrics, dict):
                        max_await = 0.0
                        for device, stats in metrics.items():
                            if isinstance(stats, dict) and 'await' in stats:
                                max_await = max(max_await, stats['await'])
                        # 只有当 await > 0 时才显示
                        if max_await > 0:
                            msg = format_service_status(h)
                            resource_services.append((5, f"✓ {msg}"))
                    # 如果 max_await = 0，跳过不显示
                    continue
                
                # 系统核心资源 - 使用元组 (order, msg) 进行排序
                msg = format_service_status(h)
                # 定义显示顺序：load_average 放在 file_descriptor 之后
                if 'load' in service.lower():
                    order = 99  # 放在最后
                elif 'file_descriptor' in service.lower():
                    order = 98  # 倒数第二
                elif 'cpu' in service.lower():
                    order = 1
                elif 'memory' in service.lower():
                    order = 2
                elif 'swap' in service.lower():
                    order = 3
                elif 'disk' in service.lower():
                    order = 4
                else:
                    order = 50
                resource_services.append((order, f"✓ {msg}"))
            elif any(x in service.lower() for x in ['network', 'tcp']):
                # 网络相关
                msg = format_service_status(h)
                if 'network' in service.lower():
                    network_services.append(f"✓ Network: {msg}")
                elif 'tcp' in service.lower():
                    network_services.append(f"✓ TCP: {msg}")
                else:
                    network_services.append(f"✓ {msg}")
            elif 'audit' in service.lower():
                # 审计
                msg = format_service_status(h)
                audit_services.append(f"✓ Audit: {msg}")
            elif 'backup' in service.lower():
                # 备份
                msg = format_service_status(h)
                backup_services.append(f"✓ Backups: {msg}")
            elif any(x in service.lower() for x in ['ssh', 'nginx', 'docker', 'redis', 'postgresql', 'ntp', 'fail2ban', 'cron', 'hermes']):
                # 守护进程服务
                svc_name = service_simple.upper() if service_simple in ['ssh'] else service_simple.capitalize()
                svc_full = service  # e.g. "postgresql@17-main", "hermes-gateway"
                # 显示友好名: postgresql, hermes-gateway
                display_name = svc_name
                if '@' in svc_full:
                    display_name = svc_full.split('@')[0].capitalize()
                if svc_full in self_healing_services:
                    display_name += "🔁"
                daemon_services.append(display_name)
            else:
                # 其他未分类服务
                msg = format_service_status(h)
                resource_services.append((50, f"✓ {msg}"))
        
        # 按照文档顺序显示：系统核心 -> 网络 -> 审计 -> 备份 -> 服务
        
        # 1. 系统核心资源（每项单独一行，按order排序）
        resource_services.sort(key=lambda x: x[0])
        for order, line in resource_services:
            rows.append(line)
        
        # 2. 网络（每项单独一行）
        for line in network_services:
            rows.append(line)
        
        # 3. 审计（每项单独一行）
        for line in audit_services:
            rows.append(line)
        
        # 4. 备份（每项单独一行）
        for line in backup_services:
            rows.append(line)
        
        # 5. 守护进程服务（合并为一行）
        if daemon_services:
            daemon_status = " | ".join([f"{s}:OK" for s in daemon_services])
            rows.append(f"✓ Services: {daemon_status}")
        payload = {
            "title": escape_markdown_v2(title), 
            "ip": ip_field,  # IP 字段不转义
            "content": escape_markdown_v2("\n".join(rows))
        }
        logger.info("Pushing HEARTBEAT payload (OK: %d, ERR: %d)", n_ok, n_err)
        if not push_to_api(notify1_api_key, payload): enqueue_to_redis(notify1_api_key, payload)

def main():
    logger.info(f"Starting notifier.py version {SCRIPT_VERSION}")
    log_sensitive_env_var("NOTIFY1_API_KEY")
    log_sensitive_env_var("NOTIFY2_API_KEY")
    notify1_api_key = get_env_var("NOTIFY1_API_KEY")
    notify2_api_key = get_env_var("NOTIFY2_API_KEY")
    
    input_data = sys.stdin.read()
    if not input_data:
        logger.debug("No input on stdin; exiting.")
        return

    try:
        data = json.loads(input_data)
        if isinstance(data, list):
            aggregate_and_push(data, notify1_api_key, notify2_api_key)
        else:
            logger.error("Expected a list of JSON objects from stdin.")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from stdin: {e}")

def log_sensitive_env_var(var_name: str):
    value = os.environ.get(var_name)
    if value: logger.debug(f"{var_name} (length): {len(value)}")

if __name__ == "__main__":
    main()
