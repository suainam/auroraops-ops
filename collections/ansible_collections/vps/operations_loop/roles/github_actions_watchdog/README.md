# GitHub Actions Watchdog

从 AuroraOps 外部执行面巡检公开仓库 workflow。仅当 GitHub 返回 `disabled_inactivity` 时执行 `enable`，确认状态恢复后触发一次 `workflow_dispatch`。其它状态只记录，不制造提交、不重复触发。

## 权限与密钥

复用现有 Vaultwarden 同步变量 `github_auroraops_token`。该 token 必须覆盖三个目标仓库，并具备 `Actions: write` 与 `Metadata: read`。PAT 注入 root-only environment file。

## 生命周期

```bash
make switch_remote.cc15
make check-operations_loop.github_actions_watchdog
make deploy-operations_loop.github_actions_watchdog
make verify-operations_loop.github_actions_watchdog
make rollback-operations_loop.github_actions_watchdog
```

目标列表保存在非敏感 JSON 配置文件；timer 默认每月 1 日运行，加入最多 6 小时随机延迟并启用持久化补跑。`verify` 只查询远端状态，不启用或触发 workflow。

演练人工停用恢复时，可显式传入 `--recover-state disabled_manually`。systemd timer 不使用此参数，生产路径不会恢复人工停用的 workflow。

## 失败语义

任一目标的查询、启用、状态确认或 dispatch 失败都会令 service 非零退出并写入 journald。若现有 health-check notifier 可用，同时发送 best-effort 告警；通知失败不会掩盖主失败。

### 内部逻辑

- 仅巡检：查询 GitHub 公开仓库 workflow 状态，仅当返回 `disabled_inactivity` 时执行 enable，状态恢复后再触发一次 `workflow_dispatch`；其余状态只记录，不提交、不重复触发。
- 单元与定时器由 `vps.applications.application_service` / `vps.applications.application_timer` 通过 `include_role` 生成（oneshot service + persistent timer）。
- `verify` 以 `--dry-run` 只读查询远端，不启用或触发任何 workflow。

## 依赖与边界

- 依赖 `github_auroraops_token`（来自 `vault_github_token`），必须覆盖三个目标仓库且具备 `Actions: write` 与 `Metadata: read` 权限；PAT 注入 root-only 环境文件（0600）。
- 依赖 `python_executor` 解释器与 `vps.applications.application_service`/`application_timer` 两个模板角色已就绪。
- 失败告警复用 health_checks notifier；通知失败不掩盖主失败。

## 维护与排查

- 查看托管文件: `ls -l /usr/local/sbin/github_actions_watchdog.py /etc/github-actions-watchdog.env /etc/github-actions-watchdog-targets.json /etc/systemd/system/github-actions-watchdog.{service,timer}`。
- 检查定时器: `systemctl is-active github-actions-watchdog.timer` / `systemctl is-enabled github-actions-watchdog.timer`。
- 只读演练: `python3 /usr/local/sbin/github_actions_watchdog.py --dry-run`（需环境变量，由 env 文件提供）。

