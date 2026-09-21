# Vaultwarden Snapshot

为运行中的 Vaultwarden SQLite 实例生成专用、一致、可验证的本地恢复快照。该角色只负责产生恢复点，不负责跨主机同步和长期对象存储保留。

## 数据流

```text
Vaultwarden 内置 SQLite backup
        +
attachments / sends 双遍非删除复制
        +
config.json / rsa_key*
        ↓
.staging-<snapshot-id>
        ↓ quick_check + SHA-256
<snapshot-id>/manifest.json
        ↓ 原子更新
latest.json
```

已发布快照不会包含活动 `-wal/-shm`、日志、`icon_cache` 或 `tmp`。每次创建在取得独占锁后会清理历史 `.staging-*`，用于自愈被重启或强制终止打断的未发布快照。

## 生命周期

```bash
make switch_remote.cc15
make check-operations_loop.vaultwarden_snapshot
make deploy-operations_loop.vaultwarden_snapshot
make deploy-operations_loop.vaultwarden_snapshot.vaultwarden_snapshot_create \
  ANSIBLE_EXTRA_ARGS='-e vaultwarden_snapshot_create_now=true'
make verify-operations_loop.vaultwarden_snapshot
make rollback-operations_loop.vaultwarden_snapshot
```

回滚默认只删除 systemd unit 和 CLI，保留已发布快照。只有显式设置 `vaultwarden_snapshot_rollback_remove_snapshots: true` 才删除快照数据。

## CLI

```bash
vaultwarden_snapshot.py create
vaultwarden_snapshot.py verify latest
vaultwarden_snapshot.py restore-test latest
vaultwarden_snapshot.py prune --keep 12
```

## 主要变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `vaultwarden_snapshot_source_dir` | `/opt/dockers/vaultwarden/data` | Vaultwarden 活动数据目录 |
| `vaultwarden_snapshot_root` | `/opt/backups/vaultwarden-snapshots` | 已发布快照根目录 |
| `vaultwarden_snapshot_timer_calendar` | `*-*-* *:0/10:00` | systemd timer 周期 |
| `vaultwarden_snapshot_retention` | `12` | 本地快照保留数量 |
| `vaultwarden_snapshot_health_url` | `http://127.0.0.1:30007/alive` | 创建前后健康探针 |
| `vaultwarden_snapshot_notify_failure` | `true` | 失败时复用 health_checks notifier |
| `vaultwarden_snapshot_create_now` | `false` | 显式 focused 调用时立即创建并发布一个新快照 |

## 边界

- 不停止或重启 Vaultwarden。
- 不替代 Rustic；Rustic 仍负责长期和云端备份。
- 不处理 Pi 同步、灾备启动、DNS 切换或回切。
