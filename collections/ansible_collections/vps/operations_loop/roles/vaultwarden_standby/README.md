# Vaultwarden Standby

为 qqg1299 与 Raspberry Pi 建立最小化的 Vaultwarden 被动温备链路。

```text
qqg1299 vaultwarden_snapshot
        ↓ 10 分钟一致性快照
/opt/backups/vaultwarden-snapshots
        ├── Rustic 每小时写入本地和云端仓库（长期兜底）
        └── ZeroTier + restricted SSH + read-only rsync（Pi 温备主链路）
                                  ↓
                         Pi incoming mirror
                                  ↓ manifest / SHA-256 / SQLite quick_check
                         Pi valid snapshots + atomic latest.json
                                  ↓
                         docker_apps/vaultwarden
                         容器长期 stopped，restart=no
```

## 模块边界

### `docker_apps/vaultwarden`

统一管理两端 Vaultwarden：

- 相同镜像版本；
- 相同 env 生成逻辑；
- 相同数据目录 `/opt/dockers/vaultwarden/data`；
- 相同容器名 `vaultwarden`；
- qqg1299 使用默认 `state=started`、`restart_policy=always`；
- Pi 覆盖为 `state=stopped`、`restart_policy=no`、端口仅绑定 `127.0.0.1`。

### `vaultwarden_standby`

只管理：

- qqg1299 受限只读快照访问；
- Pi 快照同步、校验、保留和状态；
- 同步 oneshot 与 timer；
- Pi 人工接管状态机和每分钟主节点健康告警。

不会创建第二个 Vaultwarden 容器，也不会维护第二份 Vaultwarden env。

### Rustic

Rustic 会备份 `/opt/backups/vaultwarden-snapshots`，用于本地和云端长期恢复。

Rustic **不作为 Pi 的 5 分钟同步主链路**，原因：

- qqg1299 当前 Rustic 每小时执行，RPO 明显大于 10 分钟快照周期；
- Pi 需要额外保存 Rustic 仓库密码与 Rclone 凭据；
- restore 涉及仓库锁、快照选择和更大的恢复面；
- 温备只需读取已发布小目录，ZeroTier rsync 更窄。

## 运行模式

- `source`：在源端（当前 qqg1299）创建专用 `vaultwarden-sync` 用户、固定 rsync daemon-over-SSH 命令和只读 module。
- `standby`：在接收端拉取、验证并保留快照。
  - **默认容器温备模式 (`vaultwarden_standby_snapshot_only: false`)**：依赖 `docker_apps/vaultwarden` 部署冷备容器（stopped, restart=no），保留每分钟 DR 健康监控 timer 与控制端人工接管（prepare/mark-active）能力。
  - **仅快照归档模式 (`vaultwarden_standby_snapshot_only: true`)**：适用于无 Docker 环境的轻量接收端（如完成原生迁移后的 Raspberry Pi）。仅负责周期性同步、强校验（manifest SHA-256 与 SQLite quick_check）和保留历史快照；无 Docker 依赖（`container_status: not_applicable`）；停用 DR 主节点健康监控 timer；严格拒绝本地容器接管（`vaultwarden_dr.py prepare` 明确报错拒绝，避免危险回滚）；灾难恢复需外部恢复（external restore），无本地温备接管。
## DR 变量兼容

Role 内 DR 控制面统一使用 `vaultwarden_standby_dr_*` 变量。现有 inventory 的 `vaultwarden_dr_*` 输入继续作为 defaults 回退来源；脚本环境协议 `VAULTWARDEN_DR_*` 和 `vaultwarden_dr.py` CLI 名称保持不变。

## 部署顺序

```bash
# 1. qqg1299：生成 Pi enrollment key，并配置受限只读源
make switch_remote.qqg1299
make check-operations_loop.vaultwarden_standby
make deploy-operations_loop.vaultwarden_standby
make verify-operations_loop.vaultwarden_standby

# 2. Pi：复用 docker_apps 创建停止状态的 vaultwarden
make switch_remote.rasp
make check-services.docker_apps
make deploy-services.docker_apps

# 3. Pi：部署同步服务和 timer
make check-operations_loop.vaultwarden_standby
make deploy-operations_loop.vaultwarden_standby
make verify-operations_loop.vaultwarden_standby
```

## CLI

```bash
vaultwarden_standby.py sync
vaultwarden_standby.py verify latest
vaultwarden_standby.py status
```

`status` 为只读 JSON，显示：

- mode；
- 容器状态和 restart policy；
- 最新有效快照；
- 快照年龄；
- 最近同步结果；
- 同步来源。

## 人工接管 CLI

接管仅适用于容器温备。控制端在停止主节点前检查 inventory 与接收端运行模式；snapshot-only、未知或非 passive 模式均拒绝接管，必须使用外部恢复流程。主节点健康检查使用 qqg1299 的 `https://bws.suai.eu.org/alive`。

```bash
# 聚合读取当前 DR 状态，不修改容器或 DNS
make vaultwarden-dr-status

# 测试域名接管演练；不会停止真实 qqg1299，但会恢复 Pi 数据、启动 Pi 容器并切换测试 DNS
CONFIRM_REMOTE_MUTATION=issue-11-drill-promotion-authorized \
  make vaultwarden-dr-promote-drill

# 生产接管必须同时确认接管授权和主节点故障
CONFIRM_REMOTE_MUTATION=issue-11-production-promotion-authorized \
CONFIRM_PRIMARY_OUTAGE=primary-is-confirmed-unhealthy \
  make vaultwarden-dr-promote-production
```

`vaultwarden_dr.py` 将接管拆成控制端可回滚的两段：

1. `prepare`：持有本地锁，检查主节点健康与隔离证明，暂停同步，从最新有效快照恢复到 staging，删除 WAL/SHM，执行 SQLite `quick_check`，原子替换数据并启动 Pi；此阶段不修改 DNS。
2. `mark-active`：只有控制端完成 DNS 切换和公网 `/alive` 200 验证后，才写入 `active` 模式、快照 ID、接管时间和 DNS 目标。

任一步骤失败，控制端恢复原 DNS；Pi 停止 Vaultwarden、恢复原数据并重新启用被动同步。显式 `--dangerous-allow-split-brain` 仅用于旧主无法隔离且操作员接受脑裂风险的场景。

每分钟健康 timer 只维护连续失败计数。连续 5 次失败后调用现有 `notifier.py` 告警，绝不自动恢复数据、启动容器或修改 DNS。

## systemd 复用

同步任务通过：

- `vps.applications.application_service`
- `vps.applications.application_timer`

生成和管理，不自建重复模板。

## 安全边界

- 默认源地址固定为 qqg1299 ZeroTier `10.147.20.129:6868`。
- key 使用 `restrict`、`from=10.147.20.192` 和固定命令；即使私钥泄露，也不能获得 shell、端口转发、Agent/X11 转发或写权限。
- sudo 只允许一个固定、只读 rsync daemon 命令。
- 不开放 rsync 873 端口。
- 传输或校验失败不会更新 Pi 的 `latest.json`。
- rollback 默认保留 Pi enrollment key、有效快照和 Vaultwarden 容器。

## 同步网络

温备同步固定使用 ZeroTier，不实现自动双链路切换。公网 SSH 仅作为人工恢复手段；需要临时兜底时，应明确增加单独受限授权，并在 ZeroTier 恢复后删除。不得取消固定命令或改成通用 shell。

## Pi 容器边界

- 容器名仍为 `vaultwarden`。
- 端口只绑定 `127.0.0.1:30007` 和 `127.0.0.1:30008`。
- `restart_policy=no`，容器保持 stopped，系统重启不会自动启动。
- 默认沿用现有 Vaultwarden 128 MiB 内存限制和 0.5 CPU 限制。
- 不修改 SillyTavern 容器、端口 `8000` 或其数据目录。

生产接管只允许通过带双重确认的 Make 入口执行；普通 Role deploy/verify 和每分钟健康 timer 均不会修改生产 DNS或自动启动 Vaultwarden。

## 仅快照模式安全边界 (Snapshot-Only)

当 `vaultwarden_standby_snapshot_only: true` 时：
- **无 Docker 依赖**：`vaultwarden_standby.py` 与 `vaultwarden_dr.py` 的 sync/status/verify 均不调用 Docker CLI 或 daemon。
- **拒绝本地接管**：`vaultwarden_dr.py` 的 `prepare`、`mark-active`、`health-check` 和 `rollback-local` 明确拒绝执行，绝不尝试启动容器或执行本地数据替换。
- **恢复语义**：快照仅作为离线只读数据源。若主节点故障，需通过外部恢复流程将 `/opt/vaultwarden-standby/snapshots` 中的有效快照恢复到目标主节点。
- **回滚安全**：Role rollback 默认保留 `/opt/vaultwarden-standby` 及其全部有效快照与 SSH 凭证。
