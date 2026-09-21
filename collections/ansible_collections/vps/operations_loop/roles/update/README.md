# Role: vps.operations_loop.update

## 1. 概述
该角色负责系统的软件包更新、服务更新和版本检查。包含 APT 更新、Docker 镜像拉取、Node.js 包更新和 Zinit shell 更新。

## 2. 变量说明

### APT 更新
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `update_system_packages` | `true` | 是否执行 apt upgrade |
| `update_dist_upgrade` | `false` | 是否使用 dist-upgrade（更激进） |
| `update_autoremove` | `true` | 是否清理不再需要的依赖包 |
| `update_reboot_if_required` | `false` | 更新后是否自动重启 |

### Docker 镜像更新
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `update_docker_images` | `false` | 是否拉取已部署容器的最新镜像 |

### Node.js 更新
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `update_nodejs_packages` | `false` | 是否更新 npm/bun 全局包 |

### Python 更新
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `update_pip_report` | `true` | 是否报告可升级的 pip 包（仅报告不执行） |

### 版本建议
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `update_recommended_postgresql_version` | `"18"` | 推荐 PostgreSQL 版本 |
| `update_recommended_redis_version` | `"7"` | 推荐 Redis 版本 |
| `update_latest_debian_release` | `"trixie"` | 最新 Debian 版本代号 |
| `update_recommended_kernel_major` | `"6.12"` | 推荐内核大版本 |

## 3. 内部逻辑

### APT 更新
1. `apt-get update` 刷新索引
2. `apt-get upgrade/dist-upgrade` 升级所有包
3. `apt-get autoremove` 清理无用依赖
4. 检查 `/var/run/reboot-required` 判断是否需要重启

### Docker 镜像更新 (默认关闭)
1. 获取当前运行容器的镜像列表
2. 对每个镜像执行 `docker pull` 拉取最新版
3. 不自动重启容器（需手动或配合 docker_apps 角色）

### Node.js 更新 (默认关闭)
1. `npm update -g` 更新全局 npm 包
2. `bun update -g` 更新全局 bun 包

### Python 报告 (默认开启)
1. `pip list --outdated` 列出可升级的包
2. 仅输出到日志，不执行升级

### Zinit 更新
1. 创建 zinit-update service/timer
2. 每天 02:00 执行 `zinit self-update && zinit update`

## 4. 依赖关系
- `vps.applications.application_service`
- `vps.applications.application_timer`

## 5. 维护与排查
- **更新日志**: `journalctl -u apt-daily.service`
- **Zinit 更新日志**: `journalctl -u zinit-update-root.service`
- **检查可升级包**: `apt list --upgradable`
- **手动执行更新**: `make run ROLE=operations_loop.update`

## 6. 修复记录

### 2026-06-01 集成服务更新任务
**变更**: 新增 Docker 镜像拉取、Node.js 包更新和 pip 报告任务。

**设计原则**: Docker/Node.js 更新默认关闭 (`false`)，需在 host_vars 中显式启用。pip 仅报告不执行，避免破坏 venv 环境。

### 2026-02-16 Zinit 更新服务
**变更**: 新增 zinit-update service/timer，每天自动更新 Zinit shells
