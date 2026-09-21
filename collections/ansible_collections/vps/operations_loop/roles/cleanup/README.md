# Role: vps.operations_loop.cleanup

## 1. 概述
该角色负责系统的定期清理工作，包括 Docker 资源回收、系统缓存清理以及计划性重启。所有清理任务统一由 `docker-maintenance-prune.service/timer` 执行。

## 2. 变量说明

### Docker 清理
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `docker_prune_keep_images` | `[]` | 保留的 Docker 镜像列表（支持通配符） |
| `docker_prune_timer_on_calendar` | `"*-*-01/3 03:00:00"` | Docker 清理 Timer 调度时间 |
| `system_reboot_timer_on_calendar` | `"*-*-01/3 04:00:00"` | 系统重启 Timer 调度时间 |

### 系统缓存清理
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `cleanup_npm` | `true` | 清理 npm cache (`~/.npm/_cacache`) |
| `cleanup_camoufox` | `true` | 清理 camoufox 字体缓存 (`~/.cache/camoufox/fonts`) |
| `cleanup_bun` | `true` | 清理 bun install 缓存 (`~/.bun/install`) |
| `cleanup_apt` | `true` | 清理 apt 缓存 (`/var/cache/apt/archives`) |
| `cleanup_pip` | `true` | 清理 pip cache (`~/.cache/pip`) |
| `cleanup_journal_days` | `7` | 保留 journal 日志天数（0=不清理） |
| `cleanup_tmp_days` | `7` | 保留 /tmp 文件天数（0=不清理） |

## 3. 内部逻辑

### Docker 清理
- 临时标记 `docker_prune_keep_images` 中的镜像（支持 `*` 通配符）
- 清理悬空镜像 (`docker image prune -f`)
- 清理 24h 未使用的镜像、网络和 build cache
- 不执行 `docker system prune`、`docker container prune` 或 `docker volume prune`
- stopped container 属于应用生命周期状态，不由通用维护任务删除
- 恢复保留镜像的原始 tag

### 系统缓存清理
按顺序执行以下清理（大小先于清理输出，便于日志审计）：
1. npm cache → `npm cache clean --force`
2. camoufox fonts → `rm -rf ~/.cache/camoufox/fonts`
3. bun install → `rm -rf ~/.bun/install`
4. apt cache → `apt-get clean -y`
5. pip cache → `pip cache purge`
6. journal logs → `journalctl --vacuum-time={N}d`
7. /tmp old files → `find /tmp -type f -atime +{N} -delete`

### 系统重启
- 每 3 天定时重启，释放内存并应用内核更新
- 服务设置为 `enabled: false` + `state: stopped`，仅由 timer 触发，防止无限重启循环

## 4. 依赖关系
- `application_service` 与 `application_timer` 是参数化实现工具，由 Cleanup 显式 `include_role`，不属于 Meta Dependency 或 Catalog 全量顺序。
- Catalog 保留 Docker 的全量部署顺序；focused lifecycle 只检查 daemon 能力，不隐式安装或执行 Docker Role。

## 5. 维护与排查
- **清理日志**: `journalctl -u docker-maintenance-prune.service`
- **重启日志**: `journalctl -u system-maintenance-reboot.service`
- **定时器状态**: `systemctl status docker-maintenance-prune.timer`
- **重启定时器**: `systemctl status system-maintenance-reboot.timer`
- **手动执行清理**: `/usr/local/bin/docker-prune.py`
- **安全回滚**: `make rollback-operations_loop.cleanup` 仅恢复 Ansible
  部署时保留的上一版脚本；没有备份时明确失败，不删除任何 Docker 资源。

## 6. 修复记录

### 2026-06-01 集成系统缓存清理
**变更**: 将 `docker-prune.py` 扩展为通用清理脚本，同时处理 Docker 资源和系统缓存。

**新增清理项**: npm, camoufox fonts, bun install, apt, pip, journal, /tmp

**设计原则**: 复用已有的 `docker-maintenance-prune.service/timer`，通过环境变量控制各项开关，不新增 systemd 单元。

### 2026-02-16 修复无限重启漏洞
**问题**: 部署 cleanup 角色后系统进入无限重启循环
**修复**: 重启服务设置 `enabled: false` + `state: stopped`

### 2026-02-16 取消随机延迟
**变更**: `app_timer_randomized_delay` 从 `"1h"` 改为 `"0h"`
**原因**: 多主机场景下需要同步重启时间
