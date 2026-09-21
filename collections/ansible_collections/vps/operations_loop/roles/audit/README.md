# Role: vps.operations_loop.audit

## 1. 概述
该角色用于配置系统的审计与自动更新策略，确保系统安全补丁能够及时安装。

## 2. 变量说明
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `audit_enable_unattended_upgrades` | `true` | 是否开启自动安全更新。 |
| `audit_enable_automatic_reboot` | `false` | 是否在更新后需要时自动重启系统。 |
| `audit_reboot_time` | `"04:00"` | 自动重启的时间。 |
| `audit_security_audit_lynis_dir` | `security_audit_lynis_dir` 或 `/opt/lynis` | Lynis 目录；兼容旧 inventory 输入。 |
| `audit_security_audit_report_dir` | `security_audit_report_dir` 或 `/var/log/lynis` | Lynis 报告目录；兼容旧 inventory 输入。 |
| `audit_security_audit_log_dir` | `security_audit_log_dir` 或 `/var/log/audit` | 审计日志目录；兼容旧 inventory 输入。 |
| `audit_security_audit_timer_schedule` | `security_audit_timer_schedule` 或每天 03:00 | 审计定时器；兼容旧 inventory 输入。 |

## 3. 内部逻辑
- **自动更新**: 安装并配置 `unattended-upgrades`，默认仅自动安装安全补丁。
- **配置优化**: 修改 `20auto-upgrades` 和 `50unattended-upgrades` 以满足项目需求。

## 4. 依赖关系
- 系统包: `unattended-upgrades`, `apt-listchanges`。

## 5. 维护与排查
- **检查更新日志**: `tail -f /var/log/unattended-upgrades/unattended-upgrades.log`。
- **手动测试**: `unattended-upgrades --dry-run --debug`。
- **配置文件**: `/etc/apt/apt.conf.d/50unattended-upgrades`。
