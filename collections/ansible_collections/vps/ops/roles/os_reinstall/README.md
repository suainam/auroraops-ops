# Role: vps.ops.os_reinstall

## 1. 概述与核心风险 (CAUTION: Destructive Action)

> [!CAUTION]
> **本角色为最高危险等级（Manual-Destructive）的物理级破坏性操作角色！**
> 它通过远程调用上游官方 `bin456789/reinstall.sh` 脚本，将目标 VPS 的物理/虚拟磁盘进行**全盘抹除**，重装为官方纯净 Debian 操作系统（默认 Debian 13 或 12）。
> 
> **此操作将永久清空磁盘上的所有数据！**
> 本角色具备不可逆性，**绝不支持自动 rollback**。重装前必须满足以下前置条件：
> 1. 已在外部完成核心数据异地归档与快照备份，并通过操作员明确声明确认；
> 2. 具备云厂商外部带外控制台（如腾讯云/阿里云 VNC 救援终端）权限以应对引导异常；
> 3. 已获得明确的操作授权，严禁在生产运行时无通知触发。

---

## 2. 角色安全防御机制

为防止误触导致生产事故，本角色内置不可绕过的多重硬防御：
1. **脱离通用编排 (Excluded from Global/Domain Deployments)**:
   - 角色在 `catalog/roles.yml` 中被显式标记为 `exclusions`，绝不出现在 `playbooks/deploy.yml` 或 `playbooks/ops/deploy.yml` 中。
   - 仅能通过专用手动剧本 `playbooks/ops/os_reinstall.yml` 触发。
2. **原子化控制端动作插件不可绕过防御 (Indivisible Action Plugin Enforcement)**:
   - 核心任务（`download.yml`、`prepare.yml`、`reboot.yml`）统一通过专属控制端 Action 插件 `vps.ops.os_reinstall_execute` 执行。
   - **零注册变量信任**：不依赖任务间变量传递或 `register`（杜绝 `-e` extra-vars 伪造授权）。
   - **标签与跳步穿透防御**：每次操作直接在 Action 插件内部原子执行校验与调用，即使通过 `--tags os_reinstall` 或 `--start-at-task` 亦无法跳过防护。
   - **控制端 `/dev/tty` 强制交互确认**：插件直接打开控制端 `/dev/tty` 要求交互输入确切 `inventory_hostname`。在管道、CI、脱机后台会话（`start_new_session=True`）等无 TTY 环境下直接 fail closed 阻断。
   - **权威 Check Mode 探测**：通过 `self._task.check_mode` 权威阻断 check 模式。
   - **硬编码防篡改 SHA256 校验**：`prepare` 操作在插件内部执行 `stat` 校验，未通过不可变哈希比对前严禁发起任何远程脚本执行。
3. **操作员外部备份恢复就绪责任声明**:
   - 必须通过命令行显式传入 `-e os_reinstall_backup_operator_ack='<target_host>'`。
   - 诚实规约：系统不伪称自动化验证外部备份有效性，但严格强制操作员对该特定主机的外部备份和异地救砖手段做出责任声明。
4. **不可变脚本校验 (Immutable Pinned Checksum)**:
   - 上游脚本严格锁定 Git commit (`2252ca7615cd672dc1d9e67c52243db4ac859151`) 与 SHA256 哈希值 (`84daf5f1220ebdaf8e2d3db780e5f6b5d3a4d66a1fa70b353731662cd19a0010`)。
   - 下载使用 `checksum: "sha256:..."`，执行前 `prepare.yml` 再次读取本地文件哈希比对，任何篡改或缺失直接 fail closed。
5. **严禁 Check Mode 写入**:
   - 若处于 `--check` 模式，立即终止执行，防止任何代码产生非预期写入或网络副作用。
6. **单主机执行作用域限制 (Single-Host Guard)**:
   - 检查 `ansible_play_hosts_all | length == 1`。严禁通过批处理或 `serial` 漏洞并发重装多台机器。
7. **强校验认证参数**:
   - `os_reinstall_ssh_pubkey` 必须是非空合法公钥（正则校验 `ssh-ed25519` / `ssh-rsa` / `ecdsa` 等格式）。仅采用 SSH 公钥认证，不设默认密码。

---

## 3. 上游脚本机制与供应链边界

本角色基于上游开源项目 `bin456789/reinstall` 实现：
- **上游地址**:
  - 国内加速源: `https://cnb.cool/bin456789/reinstall/-/git/raw/2252ca7615cd672dc1d9e67c52243db4ac859151/reinstall.sh`
  - 国际官方源: `https://raw.githubusercontent.com/bin456789/reinstall/2252ca7615cd672dc1d9e67c52243db4ac859151/reinstall.sh`

### 供应链信任边界与未审查资产说明
- **第一阶段（本地已固定）**：引导脚本 `reinstall.sh` 本身已在代码库中严格锁定 commit 与 SHA256，具备防篡改校验。
- **第二阶段（动态远端拉取）**：`reinstall.sh` 执行后，会在运行时动态从 Debian 官方镜像源下载 netboot 内核 (`vmlinuz`)、initrd 及网络配置包。该第二阶段资产未在代码库静态固化，依赖上游脚本逻辑和 Debian 官方镜像签名体系。操作员需明确知晓此供应链边界。

---

## 4. 变量说明 (Defaults)

| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `auroraops_roles.ops.os_reinstall` | `false` | 角色全局总开关。专属剧本中已内嵌开启。 |
| `os_reinstall_confirm` | `false` | 破坏性操作确认开关。 |
| `os_reinstall_backup_operator_ack` | `""` | **必须指定**，操作员备份确认声明，必须与当前目标主机名完全一致。 |
| `os_reinstall_debian_version` | `"13"` | 目标 Debian 大版本，支持 `"12"` 或 `"13"`。 |
| `os_reinstall_username` | `"root"` | 安装后默认管理员用户名。 |
| `os_reinstall_ssh_port` | `22` | 重装后 SSH 监听端口。 |
| `os_reinstall_ssh_pubkey` | `""` | **必须指定**，部署后写入 root authorized_keys 的公钥内容。 |
| `os_reinstall_script_commit` | `"2252ca..."` | 锁定的上游脚本 Commit SHA。 |
| `os_reinstall_script_sha256` | `"84daf5..."` | 锁定的上游脚本 SHA256。 |
| `os_reinstall_script_path` | `"/root/reinstall.sh"` | 目标机缓存脚本路径。 |
| `os_reinstall_trigger_reboot` | `false` | 准备完成后是否由 Ansible 自动触发 `shutdown -r now`。 |

---

## 5. 故障恢复与救砖策略 (Disaster Recovery)

- **安装中断或网络丢失**:
  若重启后超过 15 分钟仍无法通过 SSH 连入目标机：
  1. 登录云服务商外部控制台，打开 VNC 终端查看安装进度或报错；
  2. 上游安装环境在特定失败模式下支持 `/trans.sh alpine` 内存救援；
  3. 若引导彻底损坏，使用服务商控制台自带的“重装系统”恢复出厂镜像后再行处理。
- **无自动化 Rollback**:
  本角色不提供任何回滚任务。`rollback.yml` 被触发时会主动 `fail loud` 并输出安全提示。

---

## 6. 使用与验证示例

### 6.1 专属剧本调用重装准备
必须使用专属入口剧本 `playbooks/ops/os_reinstall.yml`，且在交互式终端中运行：
```bash
ansible-playbook playbooks/ops/os_reinstall.yml -l <target-node> -e os_reinstall_backup_operator_ack='<target-node>' -e os_reinstall_ssh_pubkey='ssh-ed25519 AAAAC3...'
```
执行期间终端将提示：
```
[CRITICAL WARNING] Bare-metal OS reinstallation will permanently destroy all data on '<target-node>'. To confirm, type the exact hostname:
```
必须准确输入 `<target-node>` 才能继续。非 TTY 或输入不匹配将立刻终止。

### 6.2 专属只读验证剧本
等待系统重启进入正式 Debian 环境后，使用专属验证剧本：
```bash
ansible-playbook playbooks/ops/verify_os_reinstall.yml -l <target-node>
```
此剧本与任何 `deploy` 或普通 `verify` 完全解耦，无标签冲突或误触风险。

验证清单覆盖：
1. `/etc/os-release` 存在且解析出匹配的目标 `VERSION_ID`（如 `"13"` 或 `"12"`）；
2. `/usr/local/qcloud`、`/usr/local/sa`、`/usr/local/agenttools` 物理目录均已消除；
3. `pgrep -l -f '(tat_agent|barad|sgagent|yunjing)'` 无存活监控进程（返回码 1）；
4. `dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n'` 以 clean rc=0 基础全量查询包状态，并通过正则严格断言无残留监控包；
5. `systemctl list-unit-files` 无任何云监控残留 unit 文件（返回码 0 且输出为空）；
6. `ip -brief addr show` 仅收集并记录已识别的网络接口名称。
