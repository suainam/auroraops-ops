# Role: vps.observability.health_checks

## 1. 概述
该角色用于在 VPS 上部署智能、轻量级的健康检查系统。它深度集成系统指标、安全审计及备份状态，通过 Python 驱动的模块化架构确保运维人员实时掌握系统脉搏。

### 核心特性

- **数据采集与深度识别**
  - **基础运行**: CPU (带负载联动)、内存、Swap (支持 zram)、磁盘空间、文件句柄、进程总数。**已优化数值精度 (保留1位小数)**。
  - **网络识别**: **新增** ZeroTier/Docker/VPN 接口分类识别、IPv6 连通性测试与监听端口探测、TCP 连接状态摘要。**已实现无 IP 虚拟接口合并呈现**。
  - **安全审计 (Audit)**: 上次启动时间监控、待重启状态 (`reboot-required`)、近 24h 安全更新数、近 1h SSH 暴力破解尝试统计。
  - **备份监测 (Rustic)**: 按本地与云端各自的备份周期检查最新快照年龄；快照数量仅属于保留策略，不再作为健康告警依据。
  - **核心服务**: 自动检测 SSH/Nginx/Docker/Redis/PostgreSQL 等服务的运行状态及自愈重启。

- **降噪告警与交互逻辑**
  - **摘要总结 (Summary)**: **新增** 在通知详情首行显示一句话状态总结，实现"秒读"服务器状态。
  - **排序优化**: 按照 [系统核心 -> 网络/审计 -> 备份 -> 服务] 逻辑重新排列输出项。
  - **CPU 负载联动**: 仅当利用率与 15min 负载同时超标时触发 Critical，有效过滤尖峰抖动。
  - **IO 延迟导向**: 关注 `await` 响应时间而非 `%util`，适配不同物理存储性能。
  - **智能隐藏**: Disk IO 仅在有实际活动 (await > 0) 时显示，避免无意义的 "0.0ms" 噪音。

- **智能推送体系**
  - **归口调度**: 通过 `health_checks_schedule_hours` 统一管理运行点，支持灵活的任务排程。
  - **心跳报告**: 定时发送全量状态报告，支持告警抑制（4 小时内重复异常仅推一次）。
  
- **通知格式优化 (v1.7.1)**
  - **指标展示**: Memory/Swap 显示 `已用/总量` (如 `1.6G/1.9G`)，File descriptors 显示 `已用/上限` (如 `1.7K/1M`)
  - **服务名简化**: `cpu_usage` → `cpu`, `network_interfaces` → `network`，减少冗余前缀
  - **层次结构**: 使用 `── Status ──` 作为标题，所有指标无缩进平齐显示
  - **精度控制**: CPU/Disk 保留1位小数 (13.6%)，Disk IO 保留1位小数 (0.5ms)
  - **顺序优化**: 系统核心指标优先，System load 置于 File descriptors 之后以改善对齐

## 2. 变量说明 (Defaults)

| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `health_checks_schedule_hours` | `06,08,10,12,14,16,18,20` | 定时执行检查的小时点 (Cron-like) |
| `health_checks_heartbeat_interval` | `360` | 心跳报告发送间隔 (分钟) |
| `health_checks_alert_window_hours` | `8-12,14-18` | 告警/推送的有效时间窗口 |
| `health_checks_queue_on_calendar` | `08..11,14..17:00:00` | 队列重试的调度点 (仅在窗口期内) |
| `health_checks_cpu_warning_threshold` | `90` | CPU 利用率警告阈值 |
| `health_checks_cpu_critical_threshold` | `95` | CPU 利用率严重阈值 |
| `health_checks_io_await_warning_threshold` | `150`| 磁盘 IO Await 延迟警告 (ms) |
| `health_checks_disk_warning_threshold` | `90` | 磁盘空间警告阈值 |
| `health_checks_rustic_local_warning_age_hours` | `2.5` | 本地 Rustic 最新快照警告年龄（小时） |
| `health_checks_rustic_local_critical_age_hours` | `4` | 本地 Rustic 最新快照严重年龄（小时） |
| `health_checks_rustic_cloud_warning_age_hours` | `8` | 云端 Rustic 最新快照警告年龄（小时） |
| `health_checks_rustic_cloud_critical_age_hours` | `14` | 云端 Rustic 最新快照严重年龄（小时） |
| `health_checks_rustic_cleanup_warning_age_hours` | `2.5` | Rustic 全仓库清理成功标记警告年龄（小时） |
| `health_checks_rustic_cleanup_critical_age_hours` | `4` | Rustic 全仓库清理成功标记严重年龄（小时） |
| `health_checks_services_to_monitor` | `[...]` | 需要监控的服务列表 |
| `health_checks_logrotate_enabled` | `true` | 是否部署 Role-local 日志轮转配置 |
| `vault_rustic_password` | - | Rustic 仓库密码 (从 vault 读取，复用 Rustic role 密码) |

## 3. 内部逻辑
- **数据采集层 (health_check_runner.py)**: Python 实现，低开销。支持采样 10s CPU 数据。
- **业务逻辑层 (`notifier.py`)**: Python 实现。负责阈值匹配、抑制逻辑及语义格式化。
- **传输重试层 (`queue_processor.py`)**: 利用 Redis 队列处理在网络波动时的消息重试。
- **Rustic 新鲜度判定**: 本地和云端使用独立阈值。任一仓库超期时整体状态为 `warning`；只有所有已配置仓库都达到严重超期或无法读取时，整体状态才为 `critical`。云端路径优先复用 `rustic_backup_cloud_prefix`，避免与 Rustic 实际仓库路径漂移。
- **Rustic 清理判定**: 读取 `rustic_cleanup_success_marker`，只认本地和全部已配置云端仓库均清理成功后写入的证据；systemd 的退出码 `75` 不作为成功清理依据。清理年龄达到 warning 时仅在心跳中显示 `C=⚠<age>h`，不把新鲜的备份快照误报为异常；达到 critical 或 marker 无法读取时，Backups 才降级为 `warning`。
- **旧脚本回滚**: rollback 会清理 `/usr/local/bin/notify.sh`、`generate_status_json.sh`、`alert_check.sh` 和 `heartbeat.sh`，避免遗留脚本继续被外部调度调用。

## 4. 依赖关系
- `redis`: 用于消息队列存储。
- `python3`: 脚本运行环境。
- Logrotate 是可选增强；Role 只管理自己的配置文件，不隐式执行 `system.logrotate`。

## 5. 维护与排查
- **实时日志**: `tail -f /var/log/health_checks/health_check_runner.log` 和 `notifier.log`。
- **推送验证**: 手动执行以下命令模拟心跳推送：
  ```bash
  # 加载环境并执行 (HEARTBEAT_INTERVAL_MINUTES=1 强制触发心跳)
  set -a; source /etc/health_checks/health_checks.env; set +a
  export HEARTBEAT_INTERVAL_MINUTES=1
  /usr/local/bin/health_check_runner.py | /opt/aurora_venv/bin/python3 /usr/local/bin/notifier.py
  ```

- 修改核心检查算法：编辑 `templates/health_check_runner.py.j2`。
- 修改推送文案或抑制逻辑：编辑 `files/notifier.py`。

## 6. 通知格式示例

### 心跳报告 (正常状态)
```
✓ All healthy

── Status ──
✓ CPU: 13.6%
✓ Memory: 1.6G/1.9G
✓ Swap: 1.4G/4.8G
✓ Disk: 17.6%
✓ Disk IO: 5.2ms          (仅在 await > 0 时显示)
✓ OOM: None
✓ Processes: 147
✓ File descriptors: 1.7K/1M
✓ System load: 1.03/2 CPUs
✓ Network: eth0:up(74.48.35.9) | zt:up(10.147.20.149)
✓ TCP: 51 est, 20 lst
✓ Audit: Boot=OK, Updates=1, Logins=0
✓ Backups: L=✓0.8h | O=✓5.9h | G=✓5.9h | C=✓1.0h
✓ Services: SSH:OK | Nginx:OK | Redis:OK | Docker:OK
```

**注意**: 2026-02-13 优化后：
- System load 格式改为 `1.03/2 CPUs`（使用 15m 负载，与告警逻辑一致）
- Backups 格式为 `L=✓0.8h | O=✓5.9h | G=✓5.9h | C=✓1.0h`；`C` 表示最近一次全部仓库清理成功的年龄
- 默认阈值针对“本地每小时、云端每 6 小时、清理每小时”调度；不同主机应在 `host_vars` 覆盖对应年龄阈值

### 告警报告 (异常状态)
```
🟠 1 issues

⚠ Anomalies
❌ Memory: 1.8G/1.9G

── Status ──
✓ CPU: 82.7%
✓ Memory: 1.8G/1.9G
...
```

**注意**: 2026-02-13 优化后，告警消息移除了冗余的服务名前缀（如 `memory:`），使消息更简洁易读。

## 7. 问题排查与修复记录

### 2026-02-01 Health Checks 通知失败修复

**问题描述**: 通知服务启动失败，错误信息 `Required environment variable NOTIFY1_API_KEY is not set`

**根本原因**:
1. API 密钥变量在 `secrets/vault.yml` 中定义为 `health_checks_notify1_api_key`
2. 但在 `inventories/host_vars/hdy.yml` 中未正确引用该变量
3. 导致环境变量模板渲染时 API 密钥为空

**修复方案**:
```yaml
# 在 inventories/host_vars/hdy.yml 中添加
health_checks_notify1_api_key: "{{ vault_health_checks_notify1_api_key }}"
health_checks_notify2_api_key: "{{ vault_health_checks_notify2_api_key }}"
```

**验证命令**:
```bash
# 检查环境变量是否正确加载
cat /etc/health_checks/health_checks.env | grep API_KEY

# 手动测试推送（需加载环境变量）
set -a; source /etc/health_checks/health_checks.env; set +a
export HEARTBEAT_INTERVAL_MINUTES=1
/root/.local/pipx/venvs/ansible-core/bin/python3 /usr/local/bin/notifier.py
```

### Python 环境说明

**依赖安装方式**:
- 所有核心依赖（`redis`, `requests`）均由 `vps.system.python_environment` 角色统一安装到规范路径。
- Python 解释器: `/opt/aurora_venv/bin/python3` (通过 `python_executor` 变量注入)。
- Systemd service 已同步配置使用该解释器。

# 检查 redis 是否已安装
/opt/aurora_venv/bin/python3 -c "import redis; print('OK')"

# 列出环境中的包
/opt/aurora_venv/bin/pip list

## 8. 故障排查速查表

| 问题现象 | 可能原因 | 解决方案 |
|---------|---------|---------|
| `NOTIFY1_API_KEY is not set` | 变量未在 host_vars 中引用 | 检查 hdy.yml 中的变量引用配置 |
| `ModuleNotFoundError: redis` | pipx 依赖注入失败或未执行 | 手动执行 `pipx inject ansible-core redis requests` |
| 推送无响应 | 网络或 API 配置问题 | 检查日志 `tail -f /var/log/health_checks/notifier.log` |
| 服务未启动 | Systemd 配置问题 | `systemctl status health_check_runner.timer` |

### 依赖问题手动修复

如果遇到 `ModuleNotFoundError: redis` 错误，按以下步骤修复：

```bash
# 1. 重新部署环境角色 (Phase 1)
make deploy-system.python_environment

# 2. 验证依赖已安装
/opt/aurora_venv/bin/python -c "import redis, requests; print('OK')"

# 3. 重新运行健康检查部署
make deploy-observability.health_checks
```

### 依赖安装验证

部署时会自动验证依赖是否安装成功：

```bash
# 手动验证依赖
/root/.local/pipx/venvs/ansible-core/bin/python -c "import redis, requests"

# 查看已安装的包
pipx list
```

> **注意**: `redis` 和 `requests` 依赖已在 `scripts/bootstrap.sh` 中自动安装。如遇导入错误，请重新运行 bootstrap 脚本。

## 9. 2026-02-04 健康检查格式优化

**修复内容**:

| 问题 | 修改位置 | 修改内容 |
|------|----------|----------|
| Swap 显示 OK | `health_check_runner.py.j2` | 移除 early return，始终调用 `add_result()` |
| boot=Need | `notifier.py` | `Need` → `Required` |
| 前缀重复 | `notifier.py` | 移除 `tcp: `, `audit: `, `backups: ` 重复前缀 |
| Network 大写 | `notifier.py` | `network` → `Network` |
| 备份按服务器过滤 | `health_check_runner.py.j2` | 使用 `backup_cloud_prefix` 过滤云端备份 |

**输出格式对比**:

| 项目 | 修改前 | 修改后 |
|------|--------|--------|
| Swap | `Swap: OK` | `Swap: 0.2G/8.6G` |
| Boot | `Boot=Need` | `Boot=Required` |
| TCP | `✓ tcp: TCP: 51 est` | `✓ TCP: 51 est` |
| Audit | `✓ audit: Audit: Boot=OK` | `✓ Audit: Boot=OK` |
| Backups | `✓ backups: Backups: Local=28` | `✓ Backups: Local=28` |
| Network | `network: eth0:up(...)` | `Network: eth0:up(...)` |

**云端备份过滤**: 通过 `backup_cloud_prefix` 变量（如 `backup/hdy`）实现只统计本服务器备份，避免跨服务器重复统计。

## 10. 2026-02-10 Rustic 现代化备份检查适配

**修复内容**:

| 问题 | 修改位置 | 修改内容 |
|------|----------|----------|
| 备份路径不兼容 | `health_check_runner.py.j2` | 适配 Rustic 现代化后的 `complete` 类型备份路径 |

**Rustic 现代化背景**:
- Rustic 备份角色已重构，备份路径从旧的 `docker/seeds`, `docker/db`, `docker/full`, `system` 改为统一的 `backup/hdy/complete` 结构
- 使用 `rclone:local:backup/hdy/complete` 作为本地备份目标
- 云端并行备份到 `onedrive:backup/hdy/complete` 和 `gdrive:backup/hdy/complete`

**修复方案**:

```python
# 修复前（旧逻辑 - 4个子目录分别检查）
for subpath in ["docker/seeds", "docker/db", "docker/full", "system"]:
    repo = f"rclone:{remote}{CONFIG['BACKUP_CLOUD_PREFIX']}/{subpath}"

# 修复后（新逻辑 - 按 remote 分组，统一检查 complete 目录）
for remote in ["onedrive:", "gdrive:"]:
    repo = f"rclone:{remote}{CONFIG['BACKUP_CLOUD_PREFIX']}/complete"
```

**更新后的备份检查逻辑**:
1. **本地备份**: 检查 `rclone:local:backup/hdy/complete` 的快照数量
2. **云端备份**: 并行检查 `onedrive:` 和 `gdrive:` 的 `backup/hdy/complete` 路径
3. **输出格式**: `Backups: Local=28, gdrive=28, onedrive=28`

**相关变量**:
```yaml
# inventories/group_vars/all/backup.yml
backup_cloud_prefix: "backup/hdy"  # 云端备份前缀路径
backup_type: "complete"            # 备份类型（modernize 后统一为 complete）
```

**验证命令**:
```bash
# 检查 health_checks 模板是否已更新
cat /etc/health_checks/health_check_runner.py | grep -A 3 "backup_cloud_prefix"

# 手动执行健康检查
set -a; source /etc/health_checks/health_checks.env; set +a
export HEARTBEAT_INTERVAL_MINUTES=1
/usr/local/bin/health_check_runner.py

# 查看备份检查日志
tail -f /var/log/health_checks/health_check_runner.log | grep -i backup
```

## 11. 2026-02-10 备份显示格式精简

**修改内容**:

| 问题 | 修改位置 | 修改内容 |
|------|----------|----------|
| 备份显示冗长 | `health_check_runner.py.j2` | 精简为 `Backups: L=4 \| O=4 \| G=4` |
| 废弃 tar 检查 | `health_check_runner.py.j2` | 移除本地 tar.gz 备份统计逻辑 |

**新格式说明**:
- `L` = Local (本地 Rustic 仓库)
- `O` = OneDrive (云端 OneDrive)
- `G` = GDrive (云端 Google Drive)

**输出示例**:
```
Backups: L=5 | O=5 | G=5
Backups: L=ERR | O=5 | G=5  # 本地检查失败
```

**相关修改**:
1. 废弃本地 tar.gz 备份检查（Rustic 完全替代）
2. 添加 RCLONE_CONFIG 环境变量支持（`health_checks.env.j2`）
3. 添加本地 Rustic 仓库检查逻辑

## 12. 2026-02-10 Rustic 密码复用

**修复内容**:

| 问题 | 修改位置 | 修改内容 |
|------|----------|----------|
| Rustic 密码重复配置 | `health_checks.env.j2` | 添加 `RUSTIC_PASSWORD={{ vault_rustic_password }}` |

**Rustic 密码复用**:
- health_checks 角色复用 Rustic role 的 `vault_rustic_password` 变量
- 通过 `EnvironmentFile=/etc/health_checks/health_checks.env` 传递给 Python 脚本
- Python 脚本继承环境变量，执行 Rustic 命令时自动读取密码

**配置流程**:
```yaml
# inventories/group_vars/all/backup.yml
vault_rustic_password: "xxx"  # vault 中定义

# inventories/host_vars/hdy.yml
# 无需额外配置，自动继承

# health_checks.env.j2
RUSTIC_PASSWORD={{ vault_rustic_password }}  # 模板中引用
```

**验证命令**:
```bash
# 检查环境变量是否包含 RUSTIC_PASSWORD
cat /etc/health_checks/health_checks.env | grep RUSTIC_PASSWORD

# 检查 health_check_runner.service 是否加载环境变量
cat /etc/systemd/system/health_check_runner.service | grep EnvironmentFile

# 手动验证 Rustic 命令可以正常执行
set -a; source /etc/health_checks/health_checks.env; set +a
Rustic -r rclone:onedrive:backup/hdy/complete snapshots
```

## 13. 2026-02-11 PostgreSQL 自愈机制配置

**问题背景**:
- PostgreSQL 集群意外停止（如 OOM Killer 触发），导致依赖数据库的容器（new-api, cliproxyapi）启动失败
- 需要自动检测并重启 PostgreSQL 集群，作为兜底保障

**PostgreSQL 服务架构**:

| 服务名 | 类型 | 说明 | 检测行为 |
|--------|------|------|----------|
| `postgresql.service` | Wrapper/Meta | `Type=oneshot`，执行 `/bin/true` | ❌ 永远显示 `active`，无法检测集群停止 |
| `postgresql@17-main.service` | 实际集群 | `Type=forking`，管理 PostgreSQL 进程 | ✅ 正确反映集群运行状态 |

**关键发现**:
```bash
# 集群停止时的状态对比
$ pg_ctlcluster 17 main stop
$ systemctl is-active postgresql          # 输出: active (假阳性!)
$ systemctl is-active postgresql@17-main  # 输出: inactive (正确)
```

**修复方案**:

1. **添加 PostgreSQL 到监控列表** (`defaults/main.yml`):
```yaml
health_checks_services_to_monitor:
  - ssh
  - nginx
  - docker
  - redis
  - postgresql@17-main  # 使用实例服务，而非 wrapper

health_checks_self_healing_services:
  - nginx
  - docker
  - redis
  - postgresql@17-main  # 启用自愈重启
```

2. **修复模板实例化服务检测** (`health_check_runner.py.j2`):
```python
def check_service(self, service_name):
    try:
        # 对于模板实例化服务 (如 postgresql@17-main)，使用 systemctl cat 检查是否存在
        if '@' in service_name:
            _, code = run_command(["systemctl", "cat", service_name])
            if code != 0:
                return
        else:
            out, code = run_command(["systemctl", "list-unit-files", f"{service_name}.service"])
            if service_name not in out:
                return
```

**原因**: `systemctl list-unit-files` 不会列出模板实例化的服务（如 `postgresql@17-main`），导致检查被跳过。

3. **更新 host_vars 配置** (`inventories/host_vars/localhost.yml`):
```yaml
health_checks_services_to_monitor:
  - ssh
  - nginx
  - docker
  - redis
  - postgresql@17-main

health_checks_self_healing_services:
  - nginx
  - docker
  - redis
  - postgresql@17-main
```

**自愈测试验证**:
```bash
# 1. 停止 PostgreSQL 集群
pg_ctlcluster 17 main stop

# 2. 运行健康检查
set -a; source /etc/health_checks/health_checks.env; set +a
/usr/local/bin/health_check_runner.py

# 3. 查看自愈日志
[2026-02-11T10:18:51] [hk2818314467] [INFO] Attempting self-healing for postgresql@17-main...
[2026-02-11T10:18:53] [hk2818314467] [WARNING] Anomaly in postgresql@17-main: warning (medium) - postgresql@17-main was restarted successfully.

# 4. 验证集群状态
pg_lsclusters
# Ver Cluster Port Status Owner    Data directory              Log file
# 17  main    5432 online postgres /var/lib/postgresql/17/main pg_log/postgresql-%Y-%m-%d_%H%M%S.log
```

**自愈输出示例**:
```json
{
  "service": "postgresql@17-main",
  "status": "warning",
  "severity": "medium",
  "message": "postgresql@17-main was restarted successfully.",
  "self_healing_attempted": true,
  "self_healing_success": true
}
```

**部署命令**:
```bash
make deploy-observability.health_checks
```

**注意事项**:
- 必须使用 `postgresql@17-main` 而非 `postgresql`，否则无法检测到集群停止
- 自愈机制会执行 `systemctl restart postgresql@17-main`，等效于 `pg_ctlcluster 17 main restart`
- 如果有多个 PostgreSQL 版本/集群，需要分别添加（如 `postgresql@15-main`）

## 14. 2026-02-13 告警格式与备份检测优化

**修复内容**:

| 问题 | 修改位置 | 修改内容 |
|------|----------|----------|
| 告警消息信息冗余 | `notifier.py` | 移除告警消息中的服务名前缀，避免 `cpu: CPU: 95%` 冗余 |
| 本地备份检测失败 | `health_check_runner.py.j2` | 本地备份改用文件系统路径而非 `rclone:local:` |
| System load 显示格式 | `notifier.py` | 使用 15m 负载值，与告警逻辑一致 |

**告警消息格式优化**:

```python
# 修改前（第 599-603 行，第 622-626 行）
rows.append(f"❌ {service_simple}: {msg} [{a.get('value')}/{a.get('threshold')}]")
# 输出: ❌ cpu: CPU: 95% [critical/ok]

# 修改后
rows.append(f"❌ {msg}")
# 输出: ❌ CPU: 95%
```

**本地备份检测算法统一**:

```python
# 修改前（使用 rclone:local: 导致失败）
local_repo = f"rclone:local:{cloud_prefix}/complete"
cmd = ["Rustic", "-r", local_repo, "snapshots", "--tag", "complete", "--json"]
# 错误: rclone: CRITICAL: Failed to create file system for "local:backup/cc15/complete": 
#       didn't find section in config file ("local")

# 修改后（使用本地文件系统路径）
backup_base_dir = CONFIG.get("BACKUP_BASE_DIR", "/opt/backups")
local_repo = f"{backup_base_dir}/complete"
if os.path.isdir(local_repo):
    cmd = ["Rustic", "-r", local_repo, "snapshots", "--tag", "complete", "--json"]
# 成功: Backups: L=4 | O=4 | G=4
```

**System load 显示优化**:

```python
# 修改前（使用 5m 负载）
if "System load" in msg:
    m = re.search(r'System load: [\d.]+ /\d+ CPUs \(1m:[\d.]+, 5m:([\d.]+), 15m:[\d.]+\)\.', msg)
    if m:
        load_5m = m.group(1)
        return f"Load: {load_5m} (5m)"
# 输出: Load: 0.85 (5m)

# 修改后（使用 15m 负载，与告警逻辑一致）
if "System load" in msg:
    m = re.search(r'System load: ([\d.]+) / (\d+) CPUs \(1m:([\d.]+), 5m:([\d.]+), 15m:([\d.]+)\)\.', msg)
    if m:
        load_15m, n_cpu = m.group(1), m.group(2)
        return f"System load: {load_15m}/{n_cpu} CPUs"
# 输出: System load: 0.02/2 CPUs
```

**输出格式对比**:

| 项目 | 修改前 | 修改后 |
|------|--------|--------|
| 告警消息 | `❌ cpu: CPU: 95% [critical/ok]` | `❌ CPU: 95%` |
| 告警消息 | `❌ memory: Memory: 1.8G/1.9G [warning/ok]` | `❌ Memory: 1.8G/1.9G` |
| System load | `✓ Load: 0.85 (5m)` | `✓ System load: 0.02/2 CPUs` |
| Backups | `✓ Backups: L=ERR \| O=4 \| G=4` | `✓ Backups: L=4 \| O=4 \| G=4` |

**验证命令**:

```bash
# 部署更新
make deploy-observability.health_checks

# 手动触发健康检查
ssh cc15 -p 6868 'set -a; source /etc/health_checks/health_checks.env; set +a; \
  export HEARTBEAT_INTERVAL_MINUTES=1; \
  /usr/local/bin/health_check_runner.py | \
  /root/.local/pipx/venvs/ansible-core/bin/python3 /usr/local/bin/notifier.py'

# 查看备份检测结果
ssh cc15 -p 6868 '/usr/local/bin/health_check_runner.py 2>/dev/null | \
  python3 -c "import sys, json; data = json.load(sys.stdin); \
  [print(f\"{item.get('\''service'\'')}: {item.get('\''message'\'')}\") \
  for item in data if '\''backup'\'' in item.get('\''service'\'', '\'\'')"]' \
# 输出: backups: Backups: L=4 | O=4 | G=4
```

**技术细节**:

1. **本地备份路径选择**: 
   - ❌ 不使用 `rclone:local:` - 需要在 rclone 配置中定义 local remote
   - ✅ 直接使用文件系统路径 `/opt/backups/complete` - 简单可靠

2. **云端备份路径**: 
   - 继续使用 `rclone:onedrive:backup/cc15/complete` 和 `rclone:gdrive:backup/cc15/complete`
   - 保持与 Rustic 备份角色的一致性

3. **告警逻辑**: 
   - 15m 负载用于告警触发判断（`check_load()` 函数）
   - 显示格式改为与告警逻辑一致，避免混淆

**相关变量**:

```yaml
# CONFIG 字典（health_check_runner.py.j2）
BACKUP_BASE_DIR: "/opt/backups"              # 本地备份基础目录
BACKUP_CLOUD_PREFIX: "backup/cc15"           # 云端备份前缀路径
BACKUP_CLOUD_REMOTES: ["onedrive", "gdrive"] # 云端备份 remotes
```

## 16. 2026-02-16 备份标签检测修复

**问题描述**:
- 健康检查显示 `Backups: L=ERR | O=ERR | G=ERR`，但实际快照存在
- 手动执行 `Rustic snapshots` 可以正常返回数据

**根本原因**:
- 备份脚本使用动态标签策略：`"10min"` / `"daily"` / `"weekly"` / `"monthly"` / `"yearly"`
- 健康检查脚本使用硬编码 `--tag complete` 过滤快照
- 由于标签不匹配，Rustic 返回空数组，脚本判定为 ERR

```python
# 错误代码
Rustic -r /opt/backups/complete snapshots --tag complete --json
# 返回: [] (空数组，因为实际标签是 "10min")

# 修复后
Rustic -r /opt/backups/complete snapshots --json
# 返回: [{...}, {...}, ...] (所有快照)
```

**修复方案**:

| 文件 | 修改位置 | 修改内容 |
|------|----------|----------|
| `health_check_runner.py.j2` | 第285行 | 移除 `--tag complete` 参数 |
| `health_check_runner.py.j2` | 第307行 | 移除 `--tag complete` 参数 |
| `health_check_runner.py.j2` | 第325行 | 移除 `--tag complete` 参数 |

**修复前后对比**:

```python
# 修复前（本地备份检查）
cmd = ["timeout", "60", "Rustic", "-r", local_repo, "snapshots", "--tag", "complete", "--json"]

# 修复后（本地备份检查）
cmd = ["timeout", "60", "Rustic", "-r", local_repo, "snapshots", "--json"]
```

**验证命令**:

```bash
# 部署更新
make deploy-observability.health_checks

# 手动测试（加载环境变量）
ssh cc15 -p 6868 'set -a; source /etc/health_checks/health_checks.env; set +a; \
  /usr/local/bin/health_check_runner.py 2>&1 | \
  grep -E "(backups|Backups)"'

# 预期输出
# Backups: L=5 | O=5 | G=5
```

**技术说明**:

1. **备份标签策略**:
   - `10min`: 每10分钟执行的常规备份
   - `daily`: 每天凌晨执行的日备份
   - `weekly`: 每周日凌晨执行的周备份
   - `monthly`: 每月1号执行的月备份
   - `yearly`: 每年1月1号执行的年备份

2. **健康检查统计逻辑**:
   - 移除标签过滤后，统计仓库中所有快照数量
   - 不区分备份类型，只要存在快照即视为正常
   - 适用于所有备份策略（完全备份、增量备份等）

3. **向后兼容性**:
   - 对使用旧标签 `complete` 的备份仍然有效
   - 对使用新动态标签的备份也能正确检测

## 17. 2026-02-18 Rustic 快照数量检测修复

**问题描述**:
- 健康检查显示 `Backups: L=1 | O=1 | G=1`，但实际快照有 3-4 个
- 手动执行 `rustic snapshots` 可以正确返回数据

**根本原因**:
- Rustic `--json` 输出是**嵌套结构**：
  ```json
  [
    {
      "snapshots": [snap1, snap2, snap3, snap4]  // 内层才是实际快照
    }
  ]
  ```
- 原代码错误地计算外层数组长度 `len(snapshots)` = 1（组数量）
- 应该计算内层 `len(snapshots[0]['snapshots'])` = 4（实际快照数量）

**修复方案**:

| 文件 | 修改位置 | 修改内容 |
|------|----------|----------|
| `health_check_runner.py.j2` | 第77-88行 | 新增 `get_rustic_snapshot_count()` 统一函数 |
| `health_check_runner.py.j2` | 第295-340行 | 三处备份检查改用统一函数 |

**新增统一函数**:

```python
def get_rustic_snapshot_count(repo_path, rustic_env, timeout=65):
    cmd = ["timeout", str(timeout), "rustic", "-r", repo_path, "snapshots", "--json"]
    out, cmd_code = run_command(cmd, timeout=timeout+5, env=rustic_env)
    if cmd_code == 0 and out.strip():
        try:
            data = json.loads(out)
            if data and isinstance(data, list) and len(data) > 0:
                # 统计所有组的快照数量（按 ID 去重）
                total = sum(len(group.get("snapshots", [])) for group in data)
                return total
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            pass
    return None
```

**修复前后对比**:

| 位置 | 修复前 | 修复后 |
|------|--------|--------|
| 本地 (L) | `data[0]` 只读第一组 = 3 | `sum(all_groups)` = 5 |
| OneDrive (O) | `data[0]` 只读第一组 = 4 | `sum(all_groups)` = 7 |
| GDrive (G) | `data[0]` 只读第一组 = 6 | `sum(all_groups)` = 9 |

**验证命令**:

```bash
# 部署更新
make deploy-observability.health_checks

# 手动测试
ssh cc15 'set -a; source /etc/health_checks/health_checks.env; set +a; \
  /usr/local/bin/health_check_runner.py 2>&1 | grep backups'

# 预期输出
# "service": "backups", "message": "Backups: L=5 | O=7 | G=9"
```

**技术细节**:

1. **JSON 结构说明**:
   - Rustic 的 `--json` 按 `hostname + label + paths` 分组
   - 备份路径变化时（如新增路径）会创建新组
   - 需要累加所有组的快照数量

2. **统一函数优势**:
   - 集中处理 JSON 解析逻辑
   - 统一的错误处理
   - 易于维护和扩展
