# vps.observability.auroraops_monitor

AuroraOps 多主机监控采集角色。覆盖 InstaBox 外网探测、qqg1299 本机资源采样、cc15 权威汇总与周报。

## 概述

角色按 `auroraops_monitor_mode` 分三种模式部署：

- `outer_probe`：InstaBox 外网探测，采样写入 `/data/home/auroraops-monitor` 下的 Bounded Spool。
- `host_metrics`：qqg1299 本机资源采样，复用 `/proc` 与既有 `geo-audit` 输出。
- `aggregator`：cc15 汇总多机输入，归档 Cloudflare 历史，调用 TypeSafe Jev 裁决并生成周报。

角色提供 9-stage 生命周期任务：`preflight`、`apply`、`verify`、`rollback`、`rollback_verify`，只读阶段严格 `changed=0`。

## 角色变量

```yaml
auroraops_monitor_enabled: true          # 是否启用角色
auroraops_monitor_mode: "outer_probe"    # outer_probe / host_metrics / aggregator
auroraops_monitor_dir: "/var/lib/auroraops-monitor"  # 工作目录
auroraops_monitor_active_file: "spool.jsonl"        # 活动采集文件
auroraops_monitor_spool_dir: "{{ auroraops_monitor_dir }}/spool"  # 封卷目录
auroraops_monitor_spool_file: "{{ auroraops_monitor_dir }}/{{ auroraops_monitor_active_file }}"
auroraops_monitor_lock_file: "{{ auroraops_monitor_dir }}/spool.lock"  # 单写锁
auroraops_monitor_max_spool_bytes: 52428800         # Spool 总容量上限 50MB
auroraops_monitor_user: "auroraops-monitor"          # 远端无特权采集用户
auroraops_monitor_group: "auroraops-monitor"
auroraops_monitor_backup_dir: "{{ auroraops_monitor_dir }}-baseline-backup"  # 变更前基线备份
auroraops_monitor_unacked_archive_dir: "{{ auroraops_monitor_dir }}-unacked-archive"  # 未确认数据归档
auroraops_monitor_probe_hosts: "1.1.1.1 8.8.8.8"    # 外测目标
auroraops_monitor_geo_audit_log: "/var/log/geo_audit/geo_audit.log"  # geo 日志游标来源
auroraops_monitor_roll_interval_seconds: 900         # 封卷年龄门槛
controller_timer_name: "com.auroraops.instabox-scheduler"  # 控制端 LaunchAgent 名称
controller_timer_script: "instabox_scheduler_tick.sh"      # 控制端调度脚本
controller_timer_interval: 14400                           # 控制端调度周期（秒）
```

敏感凭据（Cloudflare 管理口令、TypeSafe provider key、周报推送 key）来自 Ansible Vault，渲染到目标机 `/etc/auroraops/auroraops_monitor.env`，权限 `0600`。

## 内部逻辑

- 采集写入统一经过 `BoundedSpool`：`fcntl.flock` 独占锁、`os.fsync` 落盘、总容量上限 50MB。容量满且存在未确认卷时 fail-closed 拒绝新写入，不删除任何未确认数据。
- 封卷按卷首记录创建时间判定年龄，不随持续追加刷新。封卷采用原子重命名到 `spool/` 目录，不截断活动文件。
- 控制端拉取先写临时文件，校验 SHA256 后原子发布并同步目录，确认成功后回送带摘要的 ACK。远端仅在摘要匹配时删除对应封卷文件。
- geo 日志游标按事件自身 UTC 时间戳推进：窗口前合法事件作为基线，非法时间与未来事件不计入。
- 汇总指标由纯 Python 计算，模型输出不参与数值统计。Jev 裁决受双置信度门控，低于门槛时显式标记待复核。
- 回滚先停控制端调度，再持锁归档未确认数据；基线备份逐文件记录 `exists`、`mode`、`uid`、`gid` 与 systemd 原启用状态。
- 周报生成后自动推送 `weekly_report_latest.md` 至 Moe 通知端点（与 health_checks 心跳同通道）。推送失败仅记录、不阻断周报生成（非致命）。

## 依赖

- 依赖 `vps.common.tasks.managed_timer` 与 `vps.common.tasks.managed_service` 生成 systemd 与 launchd 单元。
- 依赖 `vps.ops.ipquality` 提供的 `ipquality_geo_probe.py` 作为地域判定的权威来源，角色不复制该实现。
- 依赖 `vps.common.tasks.managed_timer` 的 Darwin 分支，该分支需要 `auroraops-base` 提交 `26e81e7` 之后的版本。
- `aggregator` 模式依赖 Cloudflare Server Monitor 私有站点管理凭据与 cc15 本机 Magpie 的 `typesafe` provider。
- 周报推送复用 `health_checks_notify1_api_key`（Moe 通知通道），未配置时跳过推送不影响周报生成。

## 维护与排查

- 查看远端运行状态：`systemctl status auroraops-monitor.timer`、`journalctl -u auroraops-monitor -n 50`。
- 查看控制端调度：`launchctl list | grep com.auroraops`，日志位于 `~/Library/Logs/AuroraOps/`。
- 检查采集文件：`ls -l {{ auroraops_monitor_dir }}`，活动文件为 `spool.jsonl` 或 `metrics.jsonl`。
- 排查封卷不发生：检查 `auroraops_monitor_roll_interval_seconds` 与卷首记录创建时间，确认未超过年龄门槛。
- 排查拉取被拒：确认远端存在 `auroraops-monitor` 无特权用户，且控制端私钥 `~/.auroraops/keys/` 权限为 `0600`。
- 排查 ACK 未生效：核对控制端归档目录 `~/.auroraops/controller_spool/` 中文件是否持久落盘，摘要不一致时远端保留原文件。
- 排查周报待复核：查看 `weekly_report_latest.json` 中 `jev` 字段，置信度低于门槛时保留原始回答供人工判断。
- 排查周报推送失败：确认 `auroraops_monitor.env` 中 `NOTIFY_API_KEY` 已配置，且 `https://moe.saui.dpdns.org/api/push` 可达；推送失败不影响周报文件生成。
