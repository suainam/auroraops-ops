#!/usr/bin/env python3
"""
Rustic Backup Manager for AuroraOps
===================================
功能:
- 本地 + 云端并行备份
- 压缩级别 3
- 根据时间自动选择备份标签
- 使用排除文件
- 完善的日志和错误处理
"""

import os
import sys
import subprocess
import hashlib
import json
import logging
import shutil
import tempfile
import threading
import fcntl
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            os.getenv("RUSTIC_LOG_FILE", "/var/log/rustic/rustic_backup.log")
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

HOME_DIR = os.getenv("HOME", "/root")
SOURCE_DIR = os.getenv("BACKUP_SOURCE_DOCKER_DIR", "/opt/dockers")
CLOUD_REMOTES = os.getenv("BACKUP_CLOUD_REMOTES", "onedrive,gdrive").split(",")
CLOUD_PREFIX = os.getenv("BACKUP_CLOUD_PREFIX", "backup")
LOCAL_PREFIX = os.getenv("BACKUP_LOCAL_PREFIX", "/opt/backups/complete")
RUSTIC_PASSWORD = os.getenv("RUSTIC_PASSWORD")
EXCLUDE_FILE = os.getenv("BACKUP_EXCLUDE_FILE", "/etc/rustic/exclude.txt")
LOCK_FILE = os.getenv("RUSTIC_LOCK_FILE", "/run/lock/rustic-backup.lock")
FORGET_TIMEOUT_SECONDS = int(os.getenv("RUSTIC_FORGET_TIMEOUT_SECONDS", "900"))
CLEANUP_SUCCESS_MARKER = os.getenv(
    "RUSTIC_CLEANUP_SUCCESS_MARKER", "/var/lib/rustic/cleanup-success.json"
)
RESTORE_PROBE_DIR = os.getenv("RUSTIC_RESTORE_PROBE_DIR", "/var/tmp")
RESTORE_PROBE_PATH = os.getenv("RUSTIC_RESTORE_PROBE_PATH", "/etc/ssh/sshd_config")

RETENTION = {
    "hourly": int(os.getenv("BACKUP_RETENTION_10MIN", "3")),
    "daily": int(os.getenv("BACKUP_RETENTION_DAILY", "1")),
    "weekly": int(os.getenv("BACKUP_RETENTION_WEEKLY", "1")),
    "monthly": int(os.getenv("BACKUP_RETENTION_MONTHLY", "1")),
    "yearly": int(os.getenv("BACKUP_RETENTION_YEARLY", "1")),
}

SYSTEM_PATHS = [
    p.strip() for p in os.getenv("BACKUP_SRC_PATHS", "").split(",") if p.strip()
]
PG_PASSWORD = os.getenv("POSTGRESQL_DB_ADMIN_PASSWORD", "")
REDIS_PASSWORD = os.getenv("REDIS_DB_ADMIN_PASSWORD", "")


def get_env():
    """获取基本环境变量"""
    env = os.environ.copy()
    env["HOME"] = HOME_DIR
    env["XDG_CACHE_HOME"] = f"{HOME_DIR}/.cache"
    env["RUSTIC_PASSWORD"] = RUSTIC_PASSWORD
    return env


def run_command(cmd, timeout=600, check=True, env=None):
    """执行命令并返回结果"""
    run_env = get_env()
    if env:
        run_env.update(env)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=run_env,
        )
        if result.returncode != 0 and check:
            logger.error(f"Command failed: {' '.join(cmd)}")
            logger.error(f"stderr: {result.stderr}")
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out ({timeout}s): {' '.join(cmd)}")
        return "", f"Timeout after {timeout}s", -1
    except Exception as e:
        logger.error(f"Command exception: {' '.join(cmd)}: {e}")
        return "", str(e), -1


def export_databases():
    """导出数据库"""
    exported = []
    if PG_PASSWORD:
        logger.info("Database: Exporting PostgreSQL...")
        try:
            pg_file = "/tmp/postgresql_all_dbs.sql"
            subprocess.run(
                ["sudo", "-u", "postgres", "pg_dumpall", "-f", pg_file],
                check=True,
                timeout=300,
                env=get_env(),
            )
            exported.append(pg_file)
            logger.info(f"Database: PostgreSQL exported ({pg_file})")
        except Exception as e:
            logger.error(f"Database: PostgreSQL export failed: {e}")

    if REDIS_PASSWORD:
        logger.info("Database: Triggering Redis SAVE...")
        try:
            subprocess.run(
                ["redis-cli", "-a", REDIS_PASSWORD, "SAVE"],
                check=True,
                timeout=60,
                env=get_env(),
            )
            if os.path.exists("/var/lib/redis/dump.rdb"):
                exported.append("/var/lib/redis/dump.rdb")
                logger.info("Database: Redis SAVE completed")
        except Exception as e:
            logger.error(f"Database: Redis SAVE failed: {e}")

    return exported


def get_backup_tag():
    """根据时间确定备份标签"""
    now = datetime.now()
    if now.day == 1 and now.month == 1 and now.hour == 0:
        return "yearly"
    elif now.day == 1 and now.hour == 0:
        return "monthly"
    elif now.weekday() == 0 and now.hour == 0:
        return "weekly"
    elif now.hour == 0:
        return "daily"
    else:
        return "10min"


def backup_to_repo(repo, paths, exclude_file, result_container, idx):
    """备份到指定仓库"""
    start_time = datetime.now()

    backup_tag = get_backup_tag()
    logger.info(f"Backup: Using tag '{backup_tag}' for this backup")

    cmd = [
        "rustic",
        "backup",
        "--repo",
        repo,
    ]

    # 直接传递路径作为参数
    if paths:
        cmd.extend(paths)

    cmd.extend(
        [
            "--tag",
            backup_tag,
            "--one-file-system",
            "--set-compression",
            "5",
        ]
    )

    # 使用排除文件
    if exclude_file and os.path.exists(exclude_file):
        cmd.extend(["--glob-file", exclude_file])

    stdout, stderr, code = run_command(cmd, timeout=1800)

    elapsed = (datetime.now() - start_time).total_seconds()

    if code == 0:
        logger.info(f"Backup: Completed to {repo} ({elapsed:.1f}s)")
        for line in stdout.split("\n"):
            if "added" in line.lower() or "processed" in line.lower():
                logger.info(f"  {line.strip()}")
        result_container[idx] = {
            "success": True,
            "repo": repo,
            "destination": repo,
            "duration_seconds": elapsed,
            "status": "success",
        }
    else:
        logger.error(f"Backup: Failed to {repo}: {stderr[:200]}")
        result_container[idx] = {
            "success": False,
            "repo": repo,
            "destination": repo,
            "duration_seconds": elapsed,
            "status": "degraded",
        }


def get_snapshot_count(repo):
    """获取仓库当前 snapshot 数量，用于 cleanup evidence。"""
    cmd = ["rustic", "snapshots", "--repo", repo, "--json"]
    stdout, _, code = run_command(cmd, timeout=120, check=False)
    if code != 0 or not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            return sum(len(group.get("snapshots", [])) for group in data)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def prune_repo(repo):
    """清理指定仓库 - 按标签独立执行 forget，最后统一 prune"""
    before_count = get_snapshot_count(repo)
    logger.info(f"Prune: Starting cleanup for {repo} snapshots_before={before_count}")

    # 按标签分组独立执行 forget，每个标签使用对应的 keep-last
    tag_retention = {
        "10min": RETENTION["hourly"],  # 3
        "daily": RETENTION["daily"],  # 1
        "weekly": RETENTION["weekly"],  # 1
        "monthly": RETENTION["monthly"],  # 1
        "yearly": RETENTION["yearly"],  # 1
    }

    has_error = False
    for tag, keep_last in tag_retention.items():
        logger.info(f"Prune: Forgetting tag '{tag}' (keep-last {keep_last}) for {repo}")
        cmd = [
            "rustic",
            "forget",
            "--repo",
            repo,
            "--group-by",
            "host",
            "--filter-tags",
            tag,
            "--keep-last",
            str(keep_last),
        ]
        _, stderr, code = run_command(cmd, timeout=FORGET_TIMEOUT_SECONDS)
        if code != 0:
            logger.error(f"Prune: forget tag '{tag}' failed: {stderr[:200]}")
            has_error = True

    # 最后统一执行 prune 回收空间
    logger.info(f"Prune: Pruning unused data for {repo}")
    cmd = [
        "rustic",
        "prune",
        "--repo",
        repo,
        "--max-unused",
        "5%",
    ]
    _, stderr, code = run_command(cmd, timeout=600)
    if code != 0:
        logger.error(f"Prune: prune failed: {stderr[:200]}")
        has_error = True

    after_count = get_snapshot_count(repo)
    forgotten_count = None
    if before_count is not None and after_count is not None:
        forgotten_count = max(before_count - after_count, 0)

    cleanup_result = {
        "repo": repo,
        "snapshots_before": before_count,
        "forgotten": forgotten_count,
        "snapshots_after": after_count,
        "success": not has_error,
    }
    logger.info("CleanupResult: %s", json.dumps(cleanup_result, sort_keys=True))

    if has_error:
        logger.error("Prune: Cleanup completed with errors")
    else:
        logger.info("Prune: Cleanup completed")
    return not has_error


def backup_all(mode="all"):
    """按模式备份本地、云端或全部仓库。"""
    logger.info("=" * 60)
    logger.info("Backup: Starting optimized complete system backup")
    logger.info("=" * 60)

    exported = export_databases()

    all_paths = []
    if os.path.exists(SOURCE_DIR):
        all_paths.append(SOURCE_DIR)
    for p in SYSTEM_PATHS:
        if os.path.exists(p):
            all_paths.append(p)
        else:
            logger.warning(f"Path not found: {p}")
    for f in exported:
        if os.path.exists(f):
            all_paths.append(f)

    # 去重（SOURCE_DIR 可能与 SYSTEM_PATHS 重复，exported 文件也可能与 SYSTEM_PATHS 重复）
    all_paths = list(dict.fromkeys(all_paths))

    if not all_paths:
        logger.warning("Backup: No paths to backup")
        return

    logger.info(f"Backup: {len(all_paths)} paths to backup")

    exclude_file = EXCLUDE_FILE if os.path.exists(EXCLUDE_FILE) else None

    repos = []
    if mode in ("all", "local"):
        repos.append(("local", LOCAL_PREFIX))
    if mode in ("all", "cloud"):
        for remote in CLOUD_REMOTES:
            remote = remote.strip()
            if remote:
                repos.append((remote, f"rclone:{remote}:{CLOUD_PREFIX}/complete"))

    if mode == "cloud" and not repos:
        logger.error("Backup: cloud mode requires at least one configured destination")
        return False

    backup_results = [None] * len(repos)
    logger.info(f"Backup: Starting mode={mode} destinations={len(repos)}")

    threads = []
    for idx, (name, repo) in enumerate(repos):
        t = threading.Thread(
            target=backup_to_repo,
            args=(repo, all_paths, exclude_file, backup_results, idx),
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    for idx, (name, repo) in enumerate(repos):
        if backup_results[idx] is not None:
            logger.info(
                "BackupResult: %s", json.dumps(backup_results[idx], sort_keys=True)
            )
            if backup_results[idx]["success"]:
                logger.info(f"✓ {backup_results[idx]['destination']} backup successful")
            else:
                logger.error(f"✗ {backup_results[idx]['destination']} backup failed")

    all_success = all(result and result["success"] for result in backup_results)
    logger.info("Backup: Complete system backup finished")
    return all_success


def check_repo(repo):
    """检查仓库完整性"""
    logger.info(f"Check: Verifying repository {repo}...")
    cmd = ["rustic", "check", "--repo", repo, "--read-data", "--read-data-subset", "1%"]
    _, stderr, code = run_command(cmd, timeout=300, check=False)
    if code == 0:
        logger.info(f"Check: {repo} is healthy")
        return True
    else:
        logger.warning(f"Check: {repo} has issues: {stderr[:200]}")
        return False


def write_cleanup_success_marker(repositories):
    """原子写入全部仓库清理成功证据。"""
    marker_dir = os.path.dirname(CLEANUP_SUCCESS_MARKER) or "."
    temp_path = None
    try:
        os.makedirs(marker_dir, mode=0o755, exist_ok=True)
        payload = {
            "schema": "auroraops.rustic-cleanup-success/v1",
            "completed_at": datetime.now().astimezone().isoformat(),
            "repositories": repositories,
        }
        fd, temp_path = tempfile.mkstemp(
            prefix=".cleanup-success-", dir=marker_dir, text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8") as marker_file:
            json.dump(payload, marker_file, sort_keys=True)
            marker_file.write("\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CLEANUP_SUCCESS_MARKER)
        logger.info(f"CleanupMarker: wrote {CLEANUP_SUCCESS_MARKER}")
        return True
    except Exception as e:
        logger.error(f"CleanupMarker: failed to write success marker: {e}")
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        return False


def prune_all():
    """清理所有备份；仅在全部成功后刷新成功标记。"""
    all_success = True
    repos = [LOCAL_PREFIX]
    cloud_repos = [
        f"rclone:{remote.strip()}:{CLOUD_PREFIX}/complete"
        for remote in CLOUD_REMOTES
        if remote.strip()
    ]
    repos.extend(cloud_repos)
    for repo in repos:
        try:
            if not prune_repo(repo):
                all_success = False
        except Exception as e:
            logger.error(f"Prune: Unexpected error for {repo}: {e}")
            all_success = False

    if all_success and not write_cleanup_success_marker(repos):
        all_success = False
    return all_success


def check_all():
    """检查所有仓库"""
    repos = [
        f"rclone:{remote.strip()}:{CLOUD_PREFIX}/complete"
        for remote in CLOUD_REMOTES
        if remote.strip()
    ]
    repos.append(LOCAL_PREFIX)
    results = [check_repo(repo) for repo in repos]
    return all(results)


def repository_targets():
    """Return the configured repositories in stable local-first order."""
    repos = [LOCAL_PREFIX]
    repos.extend(
        f"rclone:{remote.strip()}:{CLOUD_PREFIX}/complete"
        for remote in CLOUD_REMOTES
        if remote.strip()
    )
    return repos


def inspect_repo(repo):
    """Read repository identity and latest snapshot evidence."""
    config_stdout, config_stderr, config_code = run_command(
        ["rustic", "cat", "config", "--repo", repo],
        timeout=60,
        check=False,
    )
    if config_code != 0:
        logger.error(f"Inspect: config unreadable for {repo}: {config_stderr[:200]}")
        return None

    snapshots_stdout, snapshots_stderr, snapshots_code = run_command(
        ["rustic", "snapshots", "--repo", repo, "--json"],
        timeout=120,
        check=False,
    )
    if snapshots_code != 0:
        logger.error(
            f"Inspect: snapshots unreadable for {repo}: {snapshots_stderr[:200]}"
        )
        return None

    try:
        repository_id = json.loads(config_stdout)["id"]
        groups = json.loads(snapshots_stdout)
        snapshots = [
            snapshot for group in groups for snapshot in group.get("snapshots", [])
        ]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        logger.error(f"Inspect: invalid Rustic JSON for {repo}: {exc}")
        return None

    if not snapshots:
        logger.error(f"Inspect: no snapshots found for {repo}")
        return None

    latest = max(snapshots, key=lambda snapshot: snapshot["time"])
    return {
        "repository": repo,
        "repository_id": repository_id,
        "snapshot_count": len(snapshots),
        "latest_snapshot_id": latest["id"],
        "latest_snapshot_time": latest["time"],
        "latest_snapshot_host": latest.get("hostname"),
        "latest_snapshot_tags": latest.get("tags", []),
        "latest_snapshot_paths": latest.get("paths", []),
    }


def inspect_all():
    """Emit one identity-bound projection for every configured repository."""
    repositories = []
    for repo in repository_targets():
        evidence = inspect_repo(repo)
        if evidence is None:
            return False
        repositories.append(evidence)
    print(json.dumps({"repositories": repositories}, sort_keys=True))
    return True


def restore_probe():
    """Restore one known file from local backup into an ephemeral directory."""
    evidence = inspect_repo(LOCAL_PREFIX)
    if evidence is None:
        return False

    snapshot_id = evidence["latest_snapshot_id"]
    with tempfile.TemporaryDirectory(
        prefix="rustic-restore-probe-", dir=RESTORE_PROBE_DIR
    ) as target:
        _, stderr, code = run_command(
            [
                "rustic",
                "restore",
                "--repo",
                LOCAL_PREFIX,
                f"{snapshot_id}:{RESTORE_PROBE_PATH}",
                target,
                "--no-ownership",
            ],
            timeout=300,
            check=False,
        )
        if code != 0:
            logger.error(f"RestoreProbe: restore failed: {stderr[:200]}")
            return False

        restored = os.path.join(target, os.path.basename(RESTORE_PROBE_PATH))
        if not os.path.isfile(restored):
            logger.error(
                f"RestoreProbe: expected file was not restored: {RESTORE_PROBE_PATH}"
            )
            return False

        with open(restored, "rb") as restored_file:
            content = restored_file.read()
        if not content:
            logger.error(f"RestoreProbe: restored file is empty: {RESTORE_PROBE_PATH}")
            return False

        print(
            json.dumps(
                {
                    "repository_id": evidence["repository_id"],
                    "snapshot_id": snapshot_id,
                    "path": RESTORE_PROBE_PATH,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
                sort_keys=True,
            )
        )
    return True


def acquire_run_lock(timeout=None, poll_interval=1.0):
    """Acquire the shared lock for every repository operation.

    If timeout is None, defaults to RUSTIC_LOCK_TIMEOUT env var (0 if unset).
    If timeout > 0, retries until timeout expires.
    """
    if timeout is None:
        try:
            timeout = float(os.getenv("RUSTIC_LOCK_TIMEOUT", "0"))
        except (ValueError, TypeError):
            timeout = 0.0

    deadline = time.time() + max(0.0, timeout)
    lock_handle = open(LOCK_FILE, "a+")
    while True:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_handle
        except BlockingIOError:
            remaining = deadline - time.time()
            if remaining <= 0:
                lock_handle.close()
                return None
            sleep_time = min(poll_interval, remaining)
            logger.info(
                f"RunLock: another Rustic operation is active; waiting {sleep_time:.1f}s (timeout in {remaining:.1f}s)..."
            )
            time.sleep(sleep_time)


def init_all():
    """初始化所有仓库"""
    repos = [
        f"rclone:{remote.strip()}:{CLOUD_PREFIX}/complete"
        for remote in CLOUD_REMOTES
        if remote.strip()
    ]
    repos.append(LOCAL_PREFIX)
    for repo in repos:
        logger.info(f"Init: Checking repository {repo}")
        cmd = ["rustic", "init", "--repo", repo]
        _, stderr, code = run_command(cmd, timeout=60, check=False)
        if code == 0:
            logger.info(f"Init: Repository {repo} initialized")
        elif "config already exists" in stderr:
            logger.info(f"Init: Repository {repo} already exists")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "all"

    actions = {
        "all": lambda: backup_all("all"),
        "local": lambda: backup_all("local"),
        "cloud": lambda: backup_all("cloud"),
        "cleanup": prune_all,
        "check": check_all,
        "init": init_all,
        "inspect": inspect_all,
        "restore-probe": restore_probe,
    }

    if action == "capabilities":
        print(
            json.dumps(
                {
                    "schema": "auroraops.rustic-capabilities/v1",
                    "actions": sorted([*actions, "capabilities"]),
                },
                sort_keys=True,
            )
        )
        return

    if not RUSTIC_PASSWORD:
        logger.error("RUSTIC_PASSWORD not set")
        sys.exit(1)

    if action in actions:
        lock_handle = acquire_run_lock()
        if lock_handle is None:
            logger.error(
                "RunLock: another Rustic operation is active; operation not run"
            )
            sys.exit(75)
        try:
            result = actions[action]()
            if result is False:
                sys.exit(1)
        finally:
            lock_handle.close()
    else:
        logger.error(f"Unknown action: {action}")
        sys.exit(1)

    logger.info("Done")


if __name__ == "__main__":
    main()
