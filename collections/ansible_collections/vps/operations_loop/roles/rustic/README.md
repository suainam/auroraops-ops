# Rustic Backup Role

基于 Rustic 的块级去重备份解决方案，复用 AuroraOps 现有变量。

## 1. 概述

Rustic 角色提供企业级块级去重备份功能，支持本地+云端并行备份。

### 核心功能

- **块级去重**: 使用 Rustic 进行高效去重，减少云端存储
- **共享运行锁**: local、cloud、cleanup、check 共用非阻塞锁，避免仓库操作重叠
- **时间标签**: 根据备份时间自动选择标签 (10min/daily/weekly/monthly/yearly)
- **100% 变量复用**: 复用 `group_vars/all/backup.yml` 中所有变量
- **多目的地备份**: 同时备份到本地 + OneDrive + GDrive
- **独立调度**: 本地和云端使用独立 service/timer，故障状态互不掩盖
- **独立清理**: backup 和 prune 由独立定时器执行
- **健康检查**: 每周自动检查仓库完整性
- **日志轮转**: Role-local 管理 `/etc/logrotate.d/rustic`，不隐式执行 Logrotate Role
- **智能排除**: 使用 rustic glob 格式排除不需要的文件

## 2. 变量说明

### 复用变量 (来自 group_vars/all/backup.yml)

| 变量 | 用途 |
|------|------|
| `backup_cloud_prefix` | 云端备份路径前缀 (如 backup/cc15, backup/hdy) |
| `backup_cloud_remotes` | 云端远程存储列表 |
| `backup_retention_10min` | 10分钟快照保留数 (默认3) |
| `backup_retention_daily` | 每日快照保留数 (默认1) |
| `backup_retention_weekly` | 每周快照保留数 (默认1) |
| `backup_retention_monthly` | 每月快照保留数 (默认1) |
| `backup_retention_yearly` | 每年快照保留数 (默认1) |
| `backup_src_paths` | 旧 inventory 输入；映射到 Role 内部 `rustic_backup_src_paths` |
| `backup_exclude_paths` | 排除模式列表 |
| `backup_source_docker_dir` | Docker 源目录 |

### 新增变量

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `vault_rustic_password` | - | Rustic 仓库密码 (从 vault 读取) |
| `rustic_install_method` | `"apt"` | 安装方式 |
| `rustic_backup_timeout` | `900` | 备份超时 (秒) |
| `rustic_backup_src_paths` | `backup_src_paths` 或 `[]` | Role 内系统备份路径；兼容旧 inventory 输入。 |
| `rustic_backup_src_paths_extra` | `backup_src_paths_extra` 或 `[]` | Role 内附加备份路径；兼容旧 inventory 输入。 |
| `rustic_local_service_timeout` | `1800` | 本地备份 systemd 服务启动超时 (秒) |
| `rustic_cloud_service_timeout` | `7200` | 云端备份 systemd 服务启动超时 (秒) |
| `rustic_log_dir` | `/var/log/rustic` | 日志目录 |
| `rustic_rclone_config_path` | `/root/.config/rclone/rclone.conf` | 云端仓库使用的 Rclone 配置。 |
| `rustic_logrotate_enabled` | `true` | 是否部署 Role-local 日志轮转配置 |
| `rustic_temp_dir` | `/var/lib/rustic/.init_markers` | 初始化标记目录 |
| `rustic_cleanup_success_marker` | `/var/lib/rustic/cleanup-success.json` | 全部仓库清理成功后原子刷新的状态标记 |
| `rustic_health_check_enabled` | `true` | 启用健康检查 |
| `rustic_check_interval` | `"weekly"` | 健康检查间隔 |
| `rustic_local_backup_timer_calendar` | `"hourly"` | 本地备份计划 |
| `rustic_cloud_backup_timer_calendar` | `"*-*-* 00/6:17"` | 云端备份计划 |
| `rustic_cleanup_timer_calendar` | `"*-*-* *:45"` | 仓库清理计划（错开云备份窗口） |
| `rustic_forget_timeout_seconds` | `900` | 每次 forget 的超时秒数 |
| `rustic_lock_file` | `/run/lock/rustic-backup.lock` | 所有仓库操作共享锁 |
| `rustic_lock_timeout` | `180` | 操作锁获取超时秒数（有界重试等待） |

健康检查对每个仓库执行 `rustic check --read-data --read-data-subset 1%`，以适配当前 Rustic CLI，并将数据读取限定为 1%。

## 3. 架构

### 服务结构 (Linux 与 macOS 双端对齐)

**Linux (Systemd)**:
```
rustic-backup-local.timer    → 触发本地备份 (hourly / :00/:20/:40)
rustic-backup-local.service  → 执行 local 模式

rustic-backup-cloud.timer    → 触发云端备份 (00/6:17 错峰)
rustic-backup-cloud.service  → 执行 cloud 模式

rustic-cleanup.timer         → 每小时触发 (:45) - 错开云端备份与本地清理
rustic-cleanup.service       → 执行清理脚本 (forget + prune)

rustic-check.timer           → 按 rustic_check_timer_calendar 触发
rustic-check.service         → 健康检查
```

**macOS / Darwin (Launchd)**:
```
com.auroraops.rustic-local   → 触发本地备份 (StartCalendarInterval: :00, :20, :40)
com.auroraops.rustic-cloud   → 触发云端备份 (StartCalendarInterval: 00/6:17)
com.auroraops.rustic-cleanup → 触发定时清理 (StartCalendarInterval: 每小时 :45 分)
```

两端统一使用 `rustic_backup.py` 进行生命周期管理，共享同一套锁机制与保留规则。
四类操作都先获取 `/run/lock/rustic-backup.lock`。锁已被占用时本次运行返回 75，
systemd unit 用 `SuccessExitStatus=75` 将其记录为预期跳过；
不并发修改仓库。所有 timer 使用 `Persistent=false`，部署启用 timer 时不会补跑错过的任务。
清理任务只有在本地与全部已配置云端仓库都成功完成 `forget + prune` 后，才原子刷新
`rustic_cleanup_success_marker`；失败或锁竞争不会覆盖上一次成功证据。需要受控手动刷新时，
先选择单一主机，再运行 `make rustic-cleanup-now`。
云端任一目的地失败时 cloud service 返回失败，并在结构化日志中
记录 `degraded`，不会被本地备份成功掩盖。

定向生命周期只检查 `rclone`、配置文件及所需 remote；不执行完整 `personalization.rclone` Role。全量部署顺序仍由 Catalog 保留。

### 生命周期证据

`make rustic-observe-baseline` 是 ProofLoop before checkpoint 的 Role-owned
只读入口。它容忍旧脚本或缺失 split units，始终以 `changed=0` 报告脚本
capability 和 systemd unit 状态；它不要求部署后目标态，也不写入主机。仓库
identity 仍由后续 check/verify 阶段负责，before 不重复读取。

`verify-operations_loop.rustic` 不以 timer active 代替备份成功。它还会验证已部署
脚本支持 split actions，通过共享锁检查全部配置仓库，并输出不含凭据的
repository ID、latest snapshot ID、时间和快照数量。

`rollback_verify-operations_loop.rustic` 证明 legacy timer 已恢复、split units 已
移除，且 `/etc/rustic/backup.env`、备份脚本和本地仓库仍存在。

`make rustic-recovery-verify` 只把最新本地快照中的一个已知文件恢复到自动清理的
临时目录，校验非空内容和 SHA-256。它不调用完整 `restore.yml`，不会覆盖数据库、
Docker 数据或系统配置。Rustic 单文件恢复会把选定文件直接放在临时目录根部；
验证器按选定路径的文件名读取该结果。

### 备份目的地

| 主机 | 本地路径 | 云端路径 |
|------|---------|---------|
| CC15 | `/opt/backups/complete` | `rclone:onedrive:backup/cc15/complete` |
| HDY | `/opt/backups/complete` | `rclone:onedrive:backup/hdy/complete` |

### 文件位置

| 文件 | 路径 | 说明 |
|------|------|------|
| 备份脚本 | `/usr/local/bin/rustic_backup.py` | Python 脚本 |
| 排除文件 | `/etc/rustic/exclude.txt` | Jinja2 模板生成，使用 glob 格式 |
| 密码文件 | `/etc/rustic/password.txt` | 纯文本密码 |
| 环境变量 | `/etc/rustic/backup.env` | systemd 环境变量文件 |
| 日志 | `/var/log/rustic/rustic_backup.log` | |
| 服务文件 | `/etc/systemd/system/rustic-*.service` | |
| 定时器 | `/etc/systemd/system/rustic-*.timer` | |

## 4. 备份流程

```
Local 或 Cloud Timer 触发
    ↓
执行 /usr/local/bin/rustic_backup.py local|cloud
    ↓
┌─────────────────────────────────────┐
│ 1. 数据库导出                         │
│    - PostgreSQL pg_dumpall           │
│    - Redis SAVE                     │
└─────────────────────────────────────┘
    ↓
┌─────────────────────────────────────┐
│ 2. 确定备份标签                       │
│    - 10min (普通时刻)                │
│    - daily (每天 00:00)             │
│    - weekly (周日 00:00)            │
│    - monthly (每月1号 00:00)        │
│    - yearly (1月1号 00:00)          │
└─────────────────────────────────────┘
    ↓
┌─────────────────────────────────────┐
│ 3. Rustic 备份 (使用排除文件)        │
│    - 读取 /etc/rustic/exclude.txt  │
│    - 使用 --glob-file 参数           │
└─────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────┐
│ 4. 并行备份到多个目的地                               │
│                                                     │
│  ┌──────────────────┐                              │
│  │ 本地备份          │                              │
│  │ /opt/backups/complete                        │
│  └──────────────────┘                              │
│                                                     │
│  ┌──────────────────┐    ┌──────────────────┐    │
│  │ OneDrive          │    │ GDrive           │    │
│  │ rclone:onedrive:  │    │ rclone:gdrive:   │    │
│  │ backup/{host}/complete                       │
│  └──────────────────┘    └──────────────────┘    │
└─────────────────────────────────────────────────────┘
    ↓
备份完成 (不立即执行 prune)
```

## 5. 清理流程

```
独立 Cleanup Timer 触发
    ↓
执行 /usr/local/bin/rustic_backup.py cleanup
    ↓
┌─────────────────────────────────────────────────────┐
│ 按标签独立清理 (--filter-tags)                       │
│                                                     │
│  rustic forget --filter-tags 10min  --keep-last 3   │
│  rustic forget --filter-tags daily  --keep-last 1   │
│  rustic forget --filter-tags weekly --keep-last 1   │
│  rustic forget --filter-tags monthly --keep-last 1  │
│  rustic forget --filter-tags yearly --keep-last 1   │
│                                                     │
│  rustic prune --max-unused 5%  (统一回收空间)        │
└─────────────────────────────────────────────────────┘
    ↓
依次清理:
  - rclone:onedrive:backup/{host}/complete
  - rclone:gdrive:backup/{host}/complete
  - /opt/backups/complete
```

> **注意**: 2026-03-05 改为按标签独立 forget。之前使用全局 `--keep-last 3 --keep-daily 1 ...`，这些是基于时间窗口的规则，和自定义 tag 无关，导致保留数量不符合预期。cloud timer 在质数分钟 `:17` 错峰；午夜周期 tag 按小时判断，因此该运行仍会进入 daily/weekly/monthly/yearly 桶。

## 6. 排除规则

### 格式说明

Rustic 使用 glob 格式，排除文件中的规则需要转换为特定格式：

| 原格式 (backup_exclude_paths) | 转换后 (exclude.txt) |
|-------------------------------|---------------------|
| `**/.git/**` | `!.git/**` |
| `**/logs/**` | `!**/logs/` |
| `**/node_modules/**` | `!**/node_modules/` |
| `**/sillytavern/data/default-user/chats/**` | `!**/sillytavern/data/default-user/chats/` |

### 转换逻辑

```python
# **/pattern/** → !**/pattern/
# **/pattern → !pattern
# 其他 → !pattern
```

### 默认排除项

- Git 版本控制 (.git, .github, .gitignore)
- 日志和临时文件 (logs, tmp, .tmp)
- 缓存目录 (.cache, node_modules, __pycache__)
- SillyTavern 用户数据 (chats, characters, backgrounds, worlds, thumbnails)
- 其他应用缓存

### 验证排除

```bash
# 查看快照包含的文件
rustic ls latest

# 对比排除前后的大小
# 排除前: ~18 MiB
# 排除后: ~3 MiB (减少 ~83%)
```

## 7. 快照大小说明

### 压缩前后概念

Rustci/Restic 的 `snapshots` 命令显示的 **Size 列是原始/压缩前大小**，不是实际存储大小：

| 指标 | 含义 | 示例 |
|------|------|------|
| **Snapshot Size** | 压缩前大小（如果恢复需要的空间） | 96 MiB |
| **Added to repo** | 实际新增到仓库的大小（压缩后） | 150 MiB (raw: 242 MiB) |
| **仓库总大小** | 所有快照共享的压缩后数据 | ~231 MB |

### 压缩效果示例

首次完整备份时的输出：
```
Files:       5307 new, 0 changed, 0 unmodified
Dirs:        1867 new, 0 changed, 0 unmodified
Added:      1.200 GiB      ← 实际存储（压缩后）
processed:  1.720 GiB      ← 原始大小（压缩前）
```

### 后续备份

后续备份（数据未变化）：
```
Files:          0 new, 0 changed, 5307 unchanged
Added to repo: 0 B           ← 无新数据添加
processed:     1.720 GiB     ← 仍显示原始大小
```

这是因为：
1. **去重**：未变化的文件复用已有数据块
2. **显示逻辑**：Snapshot Size 列显示的是"如果恢复此快照需要的空间"，而非"新存储的数据"

### 启用压缩

通过 `--set-compression 3` 参数启用压缩（3=中等等级，-7~22）：
```python
cmd.extend(["--set-compression", "3"])
```

### 查看实际仓库大小

```bash
# 本地仓库
du -sh /opt/backups/complete

# 云端仓库（通过 rclone）
rustic -r "rclone:onedrive:backup/hdy/complete" snapshots
```

## 8. 保留策略

### 策略说明

| 标签 | 触发条件 | 保留数量 |
|------|---------|---------|
| `10min` | 普通时刻 (每20分钟) | 3 |
| `daily` | 每天 00:00 | 1 |
| `weekly` | 周日 00:00 | 1 |
| `monthly` | 每月1号 00:00 | 1 |
| `yearly` | 1月1号 00:00 | 1 |

**总计**: 3 + 1 + 1 + 1 + 1 = **7 个快照** (理论最大值)

### 技术细节

- 清理使用 `--filter-tags <tag> --keep-last N` 对每个标签独立执行 `forget`
- 最后统一执行 `prune --max-unused 5%` 回收空间
- 快照标签由备份脚本根据时间自动选择，见 `rustic_backup.py` 中的 `get_backup_tag()` 函数

### 配置位置

- 运行时变量: `/etc/rustic/backup.env
- 配置文件: `/etc/rustic/rustic.toml

## 9. 安全性配置

### 文件权限

| 文件/目录 | 权限 | 说明 |
|----------|------|------|
| `/etc/rustic/` | `700` | 仅 root 可访问 |
| `/etc/rustic/password.txt` | `600` | 仅 root 可读写 |
| `/etc/rustic/backup.env` | `600` | 仅 root 可读写 |
| `/etc/rustic/exclude.txt` | `644` | 所有人可读 |
| `/usr/local/bin/rustic_backup.py` | `755` | 所有人可执行 |

### 密码管理

密码通过 systemd EnvironmentFile 管理：

```bash
# /etc/rustic/backup.env
RUSTIC_PASSWORD="vault密码"
POSTGRESQL_DB_ADMIN_PASSWORD="postgres密码"
REDIS_DB_ADMIN_PASSWORD="redis密码"
```

## 10. 恢复指南

### 安全恢复原则

恢复遵循**"先下载到临时目录，再增量恢复"**的安全模式：

```
云端快照 → 下载到临时目录 → 增量同步到目标位置
                                ↓
                            rsync (不删不改)
```

这种方式确保：
- **不误删**: 不会删除目标位置已有但快照中没有的文件
- **不误覆盖**: 临时目录独立于生产目录，下载阶段不影响运行中服务
- **可预览**: 下载后可先查看文件内容再决定是否应用
- **可迁移**: 下载到任意机器后，可用 rsync 迁移到任何目标

### 恢复流程总览

```
┌─────────────────────────────────────────────────────┐
│ 1. 查询快照列表 (可选)                               │
│    make restore-ops.Rustic.list_snapshots           │
└─────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────┐
│ 2. 下载快照到临时目录                                 │
│    (自动完成: 所有恢复命令第一步骤就是下载)           │
│    默认路径: /tmp/rustic_restore                     │
└─────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────┐
│ 3. 增量恢复到目标位置                                 │
│    ┌──────────────────────┐                         │
│    │ 数据库: 全覆盖        │                         │
│    │ (PostgreSQL / Redis) │                         │
│    └──────────────────────┘                         │
│    ┌──────────────────────┐                         │
│    │ Docker 应用: 增量更新 │                         │
│    │ (rsync, 不删文件)    │                         │
│    └──────────────────────┘                         │
│    ┌──────────────────────┐                         │
│    │ 系统配置: 增量更新    │                         │
│    │ (rsync, 保留主机密钥) │                         │
│    └──────────────────────┘                         │
└─────────────────────────────────────────────────────┘
```

### 命令速查

```bash
# 0. 先切换到目标主机
make switch_remote.cc15
# 或
make switch_remote.hdy

# 1. 查询云端快照 (查看可用的快照 ID)
make restore-operations_loop.Rustic.list_snapshots SOURCE=onedrive

# 2. 恢复单个 Docker 应用 (常用)
make restore-services.docker_apps.gcli2api SOURCE=onedrive SNAPSHOT=latest

# 3. 恢复所有 Docker 应用
make restore-services.docker_apps SOURCE=onedrive

# 4. 恢复数据库 (全覆盖, 会停机)
make restore-applications.postgresql SOURCE=onedrive
make restore-applications.redis SOURCE=onedrive

# 5. 恢复系统配置 (增量, 覆盖配置文件)
make restore-system SOURCE=onedrive

# 6. 完整恢复 (全部)
make restore-operations_loop.Rustic SOURCE=onedrive
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `SOURCE` | `onedrive` | 云端存储源 (onedrive/gdrive) |
| `SNAPSHOT` | `latest` | 快照 ID 或 `latest` |
| `SNAPSHOT=abc123` | - | 指定特定快照恢复 |

### 恢复详解

#### 步骤 1: 查询快照

```bash
# 从 OneDrive 查询快照
make restore-operations_loop.Rustic.list_snapshots SOURCE=onedrive

# 输出示例:
# ID          Time                Host  Tags    Size
# fd934685    2026-05-01 00:00:20 ccuai monthly 7.2 MiB
# 48fd37b1    2026-05-04 00:00:19 ccuai weekly  7.2 MiB
# 40295b70    2026-05-10 00:00:40 ccuai daily   415.4 MiB
```

#### 步骤 2: 下载快照 (自动完成)

所有 restore 命令的第一个步骤都是**从云端下载快照到临时目录**：

```
云端 (rclone:onedrive:backup/cc15/complete)
    ↓
临时目录 (/tmp/rustic_restore/)
    ├── etc/                     # 系统配置
    ├── opt/dockers/             # Docker 应用数据
    ├── root/                    # 用户配置
    ├── tmp/                     # 数据库导出
    └── var/                     # 数据库文件
```

临时目录路径可通过 `RESTORE_TARGET` 自定义：

```bash
make restore-services.docker_apps \
  SOURCE=onedrive \
  RESTORE_TARGET=/tmp/my_restore_dir
```

#### 步骤 3: 增量恢复

下载完成后，恢复逻辑对不同类型数据执行不同策略：

##### 数据库 (全覆盖)
- **PostgreSQL**: 执行 `psql -f postgresql_all_dbs.sql`，完全替换现有数据库
- **Redis**: 停止 Redis → 复制 dump.rdb → 启动 Redis
- 需要显式指定 `make restore-applications.postgresql`

##### Docker 应用 (增量更新)
- 使用 `rsync -auvt` 同步，**只添加和更新变化的文件**
- **不会删除**目标中已存在但快照中没有的文件
- 排除 `*.log` 和 `logs/` 目录

##### 系统配置 (增量更新)
- SSH: 同步配置，但**排除了主机密钥** (`ssh_host_*_key*`)
- Nginx: 直接同步所有配置
- systemd: 同步并执行 `systemctl daemon-reload`
- rclone: 同步配置文件

### 数据迁移场景

将主机 A 的数据迁移到主机 B：

```bash
# 1. 在主机 A (源) 上确认快照存在
#    快照自动备份到云端

# 2. 登录主机 B (目标)
make switch_remote.<host_b>

# 3. 从云端恢复到主机 B
make restore-operations_loop.Rustic SOURCE=onedrive SNAPSHOT=latest

# 4. 验证恢复结果
systemctl status nginx ssh
docker ps
```

迁移注意事项:
- 数据库全覆盖会替换目标主机上的所有数据
- Docker 应用是增量恢复，目标上已有的应用会更新，不存在的会新增
- 系统配置中的主机密钥会被保留（rsync 排除）
- `/opt/dockers/` 中不属于快照的文件会被保留（rsync 不使用 `--delete`）

### 手动恢复流程

如果 Ansible 不可用，可以手动执行恢复：

```bash
# 1. 设置密码
export RUSTIC_PASSWORD="密码"

# 2. 下载快照到临时目录
rustic -r "rclone:onedrive:backup/cc15/complete" \
  restore latest /tmp/rustic_restore

# 3. 查看下载内容 (确认无误)
ls -la /tmp/rustic_restore/

# 4. 增量恢复 Docker 应用
rsync -auvt --exclude='*.log' --exclude='logs/*' \
  /tmp/rustic_restore/opt/dockers/ /opt/dockers/

# 5. 增量恢复系统配置
rsync -auvt --exclude='ssh_host_*_key*' \
  /tmp/rustic_restore/etc/ssh/ /etc/ssh/
rsync -auvt /tmp/rustic_restore/etc/nginx/ /etc/nginx/

# 6. 迁移到另一台主机
#    在源主机下载后，打包传输到目标主机
tar czf restore.tar.gz -C /tmp rustic_restore
scp restore.tar.gz target-host:/tmp/
# 在目标主机上
tar xzf /tmp/restore.tar.gz -C /tmp
rsync -auvt /tmp/rustic_restore/opt/dockers/ /opt/dockers/
```

### 最佳实践

1. **先查询后恢复**: `list_snapshots` 查看可用快照，选择合适的时间点
2. **单应用恢复**: 优先用 `restore-services.docker_apps.<app>` 精确恢复，避免全量恢复
3. **迁移时注意主机密钥**: SSH 主机密钥不会从快照恢复，避免目标主机 SSH 指纹变化
4. **恢复后验证**: 检查服务状态、应用可用性
5. **回滚准备**: 恢复前手动备份关键数据 (可选)

## 11. 故障排查

### 常见问题

1. **备份失败**
   - 检查网络连通性
   - 验证 rclone 配置
   - 查看日志: `journalctl -u rustic-backup`

2. **定时器不触发**
   - 检查定时器状态: `systemctl status rustic-backup-{local,cloud}.timer`
   - 查看服务日志: `journalctl -u rustic-backup-local.service -u rustic-backup-cloud.service`

3. **排除规则不生效**
   - 检查 `/etc/rustic/exclude.txt` 是否存在
   - 验证 glob 格式是否正确
   - 使用 `--dry-run` 测试

4. **仓库被锁**
   - 解锁: `rustic -r <repo> unlock`

5. **调度回滚**
   - `rollback-operations_loop.rustic` 恢复兼容的 combined service/timer
   - 回滚只修改 systemd units，不删除 `/etc/rustic`、日志、本地仓库或云端仓库

### 已知问题与修复 (2026-05-10)

| 问题 | 症状 | 修复 |
|------|------|------|
| `ansible_env` 未定义 | Tag 模式下 `ansible_env.HOME` 不可用 | 替换为硬编码 `/root` |
| `--target` 参数被拒绝 | rustic v0.11.0 将目标路径改为位置参数 | 改为 `restore <snapshot> <destination>` 格式 |
| `--tag` 参数被拒绝 | v0.11.0 使用 `--filter-tags-exact` 替代 `--tag` | 移除 tag 过滤，展示所有快照 |

> 注: `restore.yml` 中的 `ansible_env` 引用和 rustic CLI 标志已在 2026-05-10 修复。

## 12. 与 Restic 的区别

| 特性 | Restic (旧) | Rustic (新) |
|------|-------------|-------------|
| 锁机制 | 有锁竞争问题 | 所有仓库操作共用非阻塞运行锁 |
| 安装 | apt 仓库 | apt + GitHub fallback |
| 排除格式 | 无特殊要求 | 需要 glob 格式 |
| 清理 | backup 后立即执行 | 独立定时器 |
| 标签 | 固定 complete | 动态时间标签 |

## 13. 版本信息

- Rustic: v0.11.0
- 备份脚本: rustic_backup.py
- 配置文件: backup.env, rustic.toml
