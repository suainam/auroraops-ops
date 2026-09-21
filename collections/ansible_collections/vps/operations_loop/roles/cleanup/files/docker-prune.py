#!/usr/bin/env python3
"""
AuroraOps Maintenance Prune Script
统一清理: Docker 资源 + 系统缓存
由 docker-maintenance-prune.service/timer 触发
"""

import subprocess
import os
import sys
import datetime
import fnmatch
import glob

# ============================================
# 环境变量配置 (由 Ansible 注入)
# ============================================

# Docker 保留镜像 (逗号分隔)
KEEP_IMAGES_STR = os.environ.get('KEEP_IMAGES', '')
KEEP_PATTERNS = [p.strip() for p in KEEP_IMAGES_STR.split(',') if p.strip()]

# 系统缓存清理开关
CLEANUP_NPM = os.environ.get('CLEANUP_NPM', 'true').lower() == 'true'
CLEANUP_CAMOUFOX = os.environ.get('CLEANUP_CAMOUFOX', 'true').lower() == 'true'
CLEANUP_BUN = os.environ.get('CLEANUP_BUN', 'true').lower() == 'true'
CLEANUP_APT = os.environ.get('CLEANUP_APT', 'true').lower() == 'true'
CLEANUP_PIP = os.environ.get('CLEANUP_PIP', 'true').lower() == 'true'
CLEANUP_JOURNAL_DAYS = int(os.environ.get('CLEANUP_JOURNAL_DAYS', '7'))
CLEANUP_TMP_DAYS = int(os.environ.get('CLEANUP_TMP_DAYS', '7'))

DOCKER_PRUNE_COMMANDS = (
    ('dangling images', 'docker image prune -f'),
    ('unused images older than 24h', 'docker image prune -af --filter=until=24h'),
    ('unused networks older than 24h', 'docker network prune -f --filter=until=24h'),
    ('build cache older than 24h', 'docker builder prune -af --filter=until=24h'),
)


def run_cmd(cmd, check=False):
    """执行命令并返回输出"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        if check and result.returncode != 0:
            print(f"  [WARN] {cmd} -> {result.stderr.strip()}")
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        print(f"  [WARN] {cmd} -> timeout")
        return False, '', 'timeout'
    except Exception as e:
        return False, '', str(e)


def get_dir_size(path):
    """获取目录大小 (MB)"""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except Exception:
        pass
    return total / (1024 * 1024)


# ============================================
# Docker 清理
# ============================================

def get_images():
    """获取所有镜像列表"""
    ok, stdout, _ = run_cmd('docker images --format "{{.Repository}}:{{.Tag}}"')
    if not ok or not stdout:
        return []
    return stdout.split('\n')


def tag_image(img, suffix):
    """给镜像打临时 tag"""
    run_cmd(f'docker tag "{img}" "{img}{suffix}"')


def remove_temp_tag(img, suffix):
    """移除临时 tag"""
    orig = img.replace(suffix, '')
    run_cmd(f'docker tag "{img}" "{orig}"')
    run_cmd(f'docker rmi "{img}"')


def cleanup_docker():
    """Docker 资源清理"""
    print("\n=== Docker Cleanup ===")

    # 1. 临时标记需要保留的镜像
    if KEEP_PATTERNS:
        print("Protecting images:")
        all_images = get_images()
        for img in all_images:
            for pattern in KEEP_PATTERNS:
                if fnmatch.fnmatch(img, pattern):
                    tag_image(img, '-auroraops-keep')
                    print(f"  - {img}")
                    break

    # Prune resource classes explicitly. Stopped containers and volumes are
    # lifecycle state and must never be removed by generic maintenance.
    failures = []
    for description, command in DOCKER_PRUNE_COMMANDS:
        print(f"Pruning {description}...")
        ok, stdout, stderr = run_cmd(command)
        if stdout:
            print(stdout)
        if not ok:
            print(f"  [WARN] {command} -> {stderr}")
            failures.append(description)

    # Restore protected image tags.
    if KEEP_PATTERNS:
        print("Restoring protected images...")
        all_images = get_images()
        for img in all_images:
            if img.endswith('-auroraops-keep'):
                remove_temp_tag(img, '-auroraops-keep')

    return not failures


# ============================================
# 系统缓存清理
# ============================================

def cleanup_system_caches():
    """系统缓存清理"""
    print("\n=== System Cache Cleanup ===")

    # npm cache
    if CLEANUP_NPM:
        cache_dir = os.path.expanduser('~/.npm/_cacache')
        if os.path.exists(cache_dir):
            size = get_dir_size(cache_dir)
            print(f"npm cache: {size:.0f}MB")
            run_cmd('npm cache clean --force', check=True)

    # camoufox fonts cache
    if CLEANUP_CAMOUFOX:
        cache_dir = os.path.expanduser('~/.cache/camoufox/fonts')
        if os.path.exists(cache_dir):
            size = get_dir_size(cache_dir)
            print(f"camoufox fonts: {size:.0f}MB")
            run_cmd(f'rm -rf {cache_dir}', check=True)

    # bun install cache
    if CLEANUP_BUN:
        cache_dir = os.path.expanduser('~/.bun/install')
        if os.path.exists(cache_dir):
            size = get_dir_size(cache_dir)
            print(f"bun install cache: {size:.0f}MB")
            run_cmd(f'rm -rf {cache_dir}', check=True)

    # apt cache
    if CLEANUP_APT:
        cache_dir = '/var/cache/apt/archives'
        if os.path.exists(cache_dir):
            size = get_dir_size(cache_dir)
            print(f"apt cache: {size:.0f}MB")
            run_cmd('apt-get clean -y', check=True)

    # pip cache
    if CLEANUP_PIP:
        cache_dir = os.path.expanduser('~/.cache/pip')
        if os.path.exists(cache_dir):
            size = get_dir_size(cache_dir)
            print(f"pip cache: {size:.0f}MB")
            run_cmd('pip cache purge', check=True)

    # journal logs
    if CLEANUP_JOURNAL_DAYS > 0:
        print(f"journalctl: vacuum to {CLEANUP_JOURNAL_DAYS}d...")
        run_cmd(f'journalctl --vacuum-time={CLEANUP_JOURNAL_DAYS}d --no-pager', check=True)

    # /tmp old files
    if CLEANUP_TMP_DAYS > 0:
        print(f"/tmp: remove files older than {CLEANUP_TMP_DAYS}d...")
        run_cmd(f'find /tmp -type f -atime +{CLEANUP_TMP_DAYS} -delete 2>/dev/null || true', check=True)


# ============================================
# Main
# ============================================

def main():
    start = datetime.datetime.now()
    print(f"=== AuroraOps Maintenance Prune @ {start} ===")

    docker_cleanup_ok = cleanup_docker()
    cleanup_system_caches()

    elapsed = (datetime.datetime.now() - start).total_seconds()
    print(f"\n=== Completed in {elapsed:.1f}s ===")
    if not docker_cleanup_ok:
        print("[ERROR] One or more Docker prune operations failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
