# Role: vps.ci_cd.runner

## 1. 概述
该角色用于在目标主机上安装并配置 GitHub Actions Self-hosted Runner，使主机能够作为 GitHub 工作流的执行器。

## 2. 变量说明
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `github_runner_version` | `"2.311.0"` | Runner 的版本号。 |
| `github_runner_user` | `"admin"` | 运行 Runner 的系统用户。 |
| `github_runner_token` | `""` | 注册 Runner 所需的 Token (需从 Vault 传入)。 |
| `github_runner_repo_url` | `""` | 关联的 GitHub 仓库 URL。 |

## 3. 内部逻辑
- **安装**: 创建家目录，下载并解压 Runner 软件包。
- **注册**: 使用 Token 将 Runner 注册到 GitHub 仓库（仅在未注册时执行）。
- **服务化**: 将 Runner 配置为 Systemd 服务，确保随系统自启并保持后台运行。

## 4. 依赖关系
- 操作系统: Linux (x64)。
- 网络: 需要能够访问 `github.com`。

## 5. 维护与排查
- **查看状态**: `systemctl status actions.runner.*`。
- **查看日志**: `journalctl -u actions.runner.* -f`。
- **重置 Runner**: 删除 Runner 目录并清理 GitHub 端的注册信息后重新运行角色。
