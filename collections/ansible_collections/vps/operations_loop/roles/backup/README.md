# Role: vps.operations_loop.backup

> **Phase**: phase7 (backup operations)
>
> **Prerequisite**: Requires `vps.services.docker` (phase2) to be deployed first

## 1. 概述
该角色负责系统的全面备份策略，包括系统配置文件、Docker 数据、数据库及云端同步。

## 2. 变量说明
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `backup_src_paths` | `[/root/AuroraOps, ...]` | 需要备份的系统路径列表。 |
| `docker_backup_enabled` | `true` | 是否启用 Docker 相关备份。 |
| `backup_source_docker_dir` | `/opt/dockers` | Docker 数据源目录。 |
| `backup_base_dir` | `/opt/backups` | 备份文件存储基础目的地。 |
| `docker_backup_manifest` | `{}` | 核心清单，定义各应用的备份维度（seeds/data）。 |
| `backup_retention_*` | `3/1/1/1` | 保留策略配置（10min/日/周/月）。 |

## 3. 内部逻辑
- **统一调度**: 通过 `backup_service` 和 `backup_timer` 注册 `unified-backup.service`，每 10 分钟执行一次全维度扫描。
- **Python 驱动**: 核心逻辑封装在 [manage_docker_data.py](file:///root/AuroraOps/scripts/manage_docker_data.py) 中，实现备份、派生、清理与同步。
- **内容哈希校验与节流 (Content-Aware Throttling)**:
    - 备份前先计算临时压缩包的 **SHA256 哈希值**。
    - 将其与现有的 `*_current.tar.gz` 进行比对。
    - **节流策略**: 只有当内容发生变化（哈希不同）时，才会更新 `_current` 文件并生成带时间戳的新副本（10min/Daily等）。
    - 如果内容无变化，**不更新任何文件**（包括 `_current` 的时间戳），从而让云端同步（rclone）完全跳过该维度，极大节省同步流量和 API 调用。
- **固定文件名策略**:
    - 每个备份维度均维护一个 `*_current.tar.gz` 文件。
    - 该文件始终指向该维度的最新成功备份，方便云端 `rclone` 进行增量同步，并简化 `docker_apps` 的恢复逻辑。
- **确定性压缩 (Deterministic Compression)**:
    - 强制使用 `tar -I "pigz -n"` 进行备份。
    - `-n` 参数禁用 gzip 头部的时间戳，确保只要原始文件内容不变，生成的压缩包哈希值就绝对一致，保证了节流逻辑的准确性。
- **多维度并行化 (Parallel Execution)**:
    - **并行备份**: 使用 Python 线程池同时启动 `Seeds`、`DB`、`Full` 和 `System` 四个维度的备份任务，互不阻塞。
    - **并行压缩**: 使用 `pigz` 多核并行压缩引擎。
    - **并行同步**: 同时向多个云端 Remote（如 OneDrive, GDrive）推送。
- **验证体系**:
    - 集成 [verify_backup_system.py](file:///root/AuroraOps/scripts/verify_backup_system.py) 自动化验证工具。
    - 支持备份完整性、增量节流逻辑、恢复一致性（Hash 级别）的全自动校验。

### 3.1 核心技术特性
- **高性能**: 全链路并行化（备份任务并行 + 压缩并行 + 同步并行）。
- **极速同步**: 内容感知节流技术，确保只有真正的数据变动才会产生上传流量。
- **可靠性**: 具备 **300s 硬超时控制**，防止网络波动导致服务挂死；跳过 OneDrive 个人保管库清理，规避已知 API 限制。
- **隔离性**: 严格区分配置（Seeds）、数据库（DB）和系统（System）维度。
- **结构化归档**: 数据库导出文件（SQL/RDB）被归整在备份包的 `database_backups/` 子目录中，保持根目录整洁。
- **智能排除**:
    - 全局排除机制 (`backup_exclude_paths`)，支持 glob 模式。
    - 自动剔除垃圾文件：Source Maps (`.map`)、各类缓存 (`cache`, `tmp`)、日志文件 (`.log`)。
    - 针对性优化：排除 SillyTavern 内部冗余备份、Vaultwarden 图标缓存等。
    - **2026-02-02 修复**: Seeds 备份现在严格遵循 `MANIFEST.seeds` 清单，仅包含定义的配置文件，不再错误包含 data 目录。

## 4. 依赖关系
- `vps.applications.application_service`
- `vps.applications.application_timer`

## 5. 维护与排查
- **备份验证**:
    - 检查 `backup_dest_path` 下生成的 `.tar.gz` 文件。
    - 运行验证脚本: `python3 scripts/verify_backup_system.py` 或 `make verify-operations_loop.backup`。
- **任务状态**: `systemctl list-timers | grep backup`。
- **手动执行**: `systemctl start unified-backup.service`。

## 6. 常见陷阱与经验教训 (Lessons Learned)

### ⚠️ 陷阱 1：本地连接主机数据冲突
**问题**：当多个主机使用 `ansible_connection=local` 时（如 ccuai 和 nas 都指向 127.0.0.1），它们共享同一个本地文件系统。备份脚本运行时，两个主机的备份会写入相同的本地目录，然后同步到各自的云端路径，导致数据混乱。

**解决方案**：
- 确保每个主机有独立的 `backup_cloud_prefix`（如 `backup/ccuai` 和 `backup/nas`）
- 对于本地连接主机，部署前必须确认当前运行的是哪个主机的配置
- 避免在本地连接主机上同时运行多个主机的备份角色

### ⚠️ 陷阱 2：GDrive 竞态条件创建重复目录
**问题**：GDrive API 在并行同步时可能出现竞态条件，导致创建多个同名目录（如 4 个 `nas` 目录）。OneDrive 通常不会出现此问题。

**解决方案**：
- 已在 `manage_docker_data.py` 中添加线程锁机制 (`_cloud_mkdir_lock`)
- 使用 `rclone mkdir` 预先创建目录结构，再执行同步
- 如果发生重复目录，需要手动清理（谨慎使用 `rclone purge`）

### ⚠️ 陷阱 3：代理配置遗漏
**问题**：在国内环境，GDrive 需要通过代理访问。如果 systemd 服务未配置代理环境变量，会导致 GDrive 同步超时失败。

**解决方案**：
- 必须在 `backup_service_environment` 中添加：
  - `http_proxy`, `https_proxy`, `HTTP_PROXY`, `HTTPS_PROXY`
- 确保 `proxy_enabled`, `proxy_http`, `proxy_https` 变量已正确定义

### ⚠️ 陷阱 4：清理操作风险
**问题**：使用 `rclone purge` 或 `rclone delete` 清理云端目录时，如果路径指定错误，可能误删其他主机的数据。

**解决方案**：
- 清理前务必使用 `rclone tree` 或 `rclone lsjson` 确认目录内容
- 区分 `--drive-use-trash=false`（直接删除）和默认行为（放入回收站）
- 永远不要假设一个目录"看起来错误"就删除 - 先确认数据归属

### ⚠️ 陷阱 5：环境变量传递
**问题**：手动测试脚本时，`sudo` 默认不会传递当前 shell 的环境变量，导致 `BACKUP_CLOUD_PREFIX` 等变量丢失，使用默认值"backup"而非预期的"backup/nas"。

**解决方案**：
- 使用 `sudo -E` 保留环境变量
- 或者显式传递变量：`sudo BACKUP_CLOUD_PREFIX=backup/nas ...`
- 推荐使用 systemd 服务运行，而非手动执行

### ⚠️ 陷阱 6：路径一致性
**问题**：不同主机的备份路径必须在所有配置中保持一致（host_vars, group_vars, 服务环境变量）。

**解决方案**：
- 统一使用 `rclone_backup_path_prefix` 和 `backup_cloud_prefix` 变量
- 部署前检查：`make check-operations_loop.backup`
- 验证云端目录结构：`rclone tree onedrive:backup --level 2`

### ⚠️ 陷阱 7：Seeds 备份错误包含 data 目录
**问题**：原 `backup_config()` 方法使用"备份整个目录 + 排除 data"的逻辑，导致 `extensions`、`node_modules` 等不应包含的文件被误备份，备份体积膨胀。

**解决方案**：
- 修改 `manage_docker_data.py` 的 `backup_config()` 方法，改为**精确包含** `MANIFEST.seeds` 中定义的路径
- 将通配排除规则（`*/*.log`, `*/cache` 等）硬编码到 Python 脚本，`backup_exclude_paths` 只保留应用级特定排除

### ⚠️ 陷阱 8：随意删除系统包
**问题**：在排查问题时使用 `apt-get remove --purge` 删除系统包（如 postgresql-15）可能导致严重后果：
- 数据库集群被完全删除
- 依赖该数据库的应用（如 FnOS 飞牛OS）无法运行
- 数据永久丢失（如果没有备份）

**解决方案**：
- **永远不要** 使用 `--purge` 删除生产环境的系统包
- 如果遇到 dpkg 锁定问题，先检查 `lsof` 或 `fuser` 找出占用进程
- 等待锁释放或使用 `dpkg --configure -a` 修复，而非删除重装
- 关键系统包（数据库、Web服务器等）的操作必须先备份数据

## 🚨 部署前检查清单
- [ ] 确认所有主机的 `backup_cloud_prefix` 已正确定义且互不冲突
- [ ] 确认代理环境变量已配置（国内环境必需）
- [ ] 检查现有云端备份结构，避免覆盖其他主机数据
- [ ] 对于本地连接主机（127.0.0.1），确认当前运行环境
- [ ] 首次部署时先 dry-run：`make check-operations_loop.backup`
- [ ] 验证 rclone 配置是否正确（`rclone listremotes`）

