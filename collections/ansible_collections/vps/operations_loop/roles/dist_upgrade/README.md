# Role: vps.operations_loop.dist_upgrade

## 1. 概述
该角色用于执行 Debian 操作系统的跨版本升级（例如从 Bookworm 升级到 Trixie）。它包含备份源文件、替换 APT 源、分步升级软件包以及升级后重启等关键流程。

## 2. 变量说明
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `dist_upgrade_confirm` | `false` | 必须手动设为 `true` 才会执行升级，以防误操作。 |
| `dist_upgrade_target_codename` | `"trixie"` | 目标发行版的代号。 |
| `dist_upgrade_old_codename` | `"bookworm"` | 当前发行版的代号。 |
| `dist_upgrade_reboot_after` | `true` | 升级完成后是否自动重启系统以应用新内核。 |
| `dist_upgrade_backup_sources` | `true` | 是否在修改前备份 `/etc/apt/sources.list`。 |

## 3. 内部逻辑
1. **安全检查**: 验证 `dist_upgrade_confirm` 是否为 `true`，以及系统是否已处于目标版本。
2. **预升级**: 确保当前版本的所有软件包已更新到最新（`dist-upgrade`）。
3. **备份与源替换**: 备份 `sources.list` 并使用 `sed` 类似的逻辑替换代号。
4. **两阶段升级**: 先执行 `safe-upgrade` 最小化风险，再执行全量 `dist-upgrade`。
5. **清理与重启**: 执行 `autoremove` 并根据配置重启系统。

## 4. 依赖关系
- 操作系统: 仅支持 Debian 系。
- 权限: 需要 `become: true`。

## 5. 维护与排查
- **升级日志**: 检查 `/var/log/apt/term.log` 获取详细的升级过程输出。
- **中断恢复**: 如果升级过程中断，尝试运行 `dpkg --configure -a` 和 `apt install -f`。
- **配置冲突**: 升级过程中默认使用 `force-confold` 以保持现有配置，旧配置备份为 `.dpkg-old`。
