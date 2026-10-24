# Role: vps.ops.os_reinstall

## 1. 概述与核心风险 (CAUTION: Destructive Action)

> [!CAUTION]
> **本角色为最高危险等级（Manual-Destructive）的物理级破坏性操作角色！**
> 它通过远程调用上游官方 `bin456789/reinstall.sh` 脚本，将目标 VPS 的物理/虚拟磁盘进行**全盘抹除**，重装为官方纯净 Debian 操作系统（默认 Debian 13 或 12）。
> 
> **此操作将永久清空磁盘上的所有数据！**
> 本角色具备不可逆性，**绝不支持自动 rollback**。重装前必须满足以下前置条件：
> 1. 已在外部完成核心数据异地归档与快照备份；
> 2. 具备云厂商外部带外控制台（如腾讯云/阿里云 VNC 救援终端）权限以应对引导异常；
> 3. 已获得明确的操作授权，严禁在生产运行时无通知触发。

---

## 2. 角色安全防御机制

为防止误触导致生产事故，本角色内置四重严格防御：
1. **默认禁用与双重确认 (Fail-Closed & Explicit Double Confirmation)**:
   - 统一通过 `auroraops_roles.ops.os_reinstall: false` 默认关闭。未开启时任务自动安全跳过，不执行任何远程探测或变更。
   - 即使显式设置 `auroraops_roles.ops.os_reinstall: true`，必须同时传入 `os_reinstall_confirm: true`，否则立即 fail loud 阻断执行。
2. **严禁 Check Mode 写入**:
   - 若处于 `--check` 模式，立即终止执行，防止任何探测代码产生非预期写入或网络副作用。
3. **单主机执行作用域限制 (Single-Host Guard)**:
   - 检查 `ansible_play_hosts_all | length == 1`。严禁通过批处理或 `serial` 漏洞并发重装多台机器。
4. **强校验认证参数**:
   - `os_reinstall_ssh_pubkey` 必须是非空合法公钥（正则校验 `ssh-ed25519` / `ssh-rsa` / `ecdsa` 等格式）。仅采用 SSH 公钥认证，不设默认密码。

---

## 3. 上游脚本机制与踩坑经验

本角色基于上游开源项目 `bin456789/reinstall` 实现：
- **上游地址**:
  - 国内加速源 (默认): `https://cnb.cool/bin456789/reinstall/-/git/raw/main/reinstall.sh`
  - 国际官方源: `https://raw.githubusercontent.com/bin456789/reinstall/main/reinstall.sh`

### 关键踩坑与技术经验
1. **非交互式用户名 (Non-interactive Username)**:
   - 上游脚本在未指定用户名时会触发交互式 `prompt_username` (`read -r -p`)。本角色通过传参 `--username root` 原生消除交互提示，杜绝 SSH 非交互执行时的 hang 挂起或 exit 报错。
2. **两阶段安装与 SSH 误报 (Two-Stage Installer Environment)**:
   - 目标机器重启后，首先清空硬盘进入内存中的 Debian installer 环境。
   - **关键陷阱**：在此阶段 SSH 服务可能已率先开放，但此时 `/etc/os-release` 不存在，实际系统挂载在 `/target`。若探测脚本仅检查 SSH 端口通畅就判定“重装完成”，会产生严重误报。只有当 installer 写入完成并二次重启、正式系统启动且 `/etc/os-release` 存在时，重装才真正完成。
3. **公钥与密码互斥**:
   - 上游脚本 `--ssh-key` 与 `--password` 参数强互斥。本角色坚持 SSH 公钥单一认证规范，重装后密码默认为空并禁用密码登录。
4. **网卡名称变迁与网络持久化风险**:
   - 重装后内核与 udev 规则刷新，网卡接口名可能发生变更（如从腾讯云旧镜像的 `eth0` 变为 `ens5`）。
   - 腾讯云 VPC 具有特殊网络拓扑（网关不在子网内、IPv4 私网 + 公网 /128 双栈绑定单网卡）。`bin456789/reinstall` 脚本会在 `/etc/network/interfaces` 中持久化写入静态路由与双栈网络。但运维人员在后续配置网络工具（如 Tailscale/WireGuard）时，必须注意网卡名变更。
5. **SSH Host Key 变更与配置碎片**:
   - 重装后目标主机的 SSH host key 会重新生成，控制端已知主机文件 `~/.ssh/known_hosts` 会触发指纹不匹配警告，连接时需清理旧指纹或使用 `StrictHostKeyChecking=accept-new`。
   - 新系统自带标准的 `/etc/ssh/sshd_config.d/` 碎片目录，后续安全加固需检查碎片文件的覆盖情况。
6. **安装后必须手动更新**:
   - 上游脚本默认拉取官方最小 cloud 基础包，安装完毕后系统补丁未打齐，进入正式系统后需第一时间执行 `apt update && apt full-upgrade`。

---

## 4. 变量说明 (Defaults)

| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `auroraops_roles.ops.os_reinstall` | `false` | 角色全局总开关。必须显式设为 `true`。 |
| `os_reinstall_confirm` | `false` | 破坏性操作二次确认开关。必须显式设为 `true`。 |
| `os_reinstall_debian_version` | `"13"` | 目标 Debian 大版本，支持 `"12"` 或 `"13"`。 |
| `os_reinstall_username` | `"root"` | 安装后默认管理员用户名。 |
| `os_reinstall_ssh_port` | `22` | 重装后 SSH 监听端口。 |
| `os_reinstall_ssh_pubkey` | `""` | **必须指定**，部署后写入 root authorized_keys 的公钥内容。 |
| `os_reinstall_script_url` | `https://cnb.cool/...` | 下载重装脚本的 URL。海外节点可在 host_vars 覆盖为 GitHub 源。 |
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

### 6.1 显式调用重装准备 (以 host_vars 为例)
在临时执行变量或 `inventories/host_vars/<target-node>.yml` 中定义：
```yaml
auroraops_roles:
  ops:
    os_reinstall: true

os_reinstall_confirm: true
os_reinstall_ssh_pubkey: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... ops@control"
os_reinstall_trigger_reboot: false  # 建议由运维人员核对输出后手动重启
```

执行命令（必须指向专有生成的 ops deploy 剧本）：
```bash
ansible-playbook -i inventories/prod.ini playbooks/ops/deploy.yml --limit <target-node> --tags os_reinstall
```

### 6.2 安装后纯净化只读验证
等待正式系统重启就绪后，执行独立验证剧本（内部绑定 `tasks_from: verify`）检查去监控效果：
```bash
ansible-playbook -i inventories/prod.ini playbooks/ops/verify.yml --limit <target-node> --tags os_reinstall
```

> [!WARNING]
> **切勿在 deploy 剧本上执行 `--tags os_reinstall,verify`！**
> Ansible 命令行中的多标签为逻辑 OR 关系。在 `playbooks/ops/deploy.yml` 上执行 `--tags os_reinstall,verify` 会命中所有带 `os_reinstall` 标签的任务，从而再次触发重装准备流程！
> **只读验证必须严格且唯一通过专门生成的验证剧本 `playbooks/ops/verify.yml` 执行。**

验证清单覆盖：
1. `/etc/os-release` 存在且解析出匹配的目标 `VERSION_ID`（如 `"13"` 或 `"12"`）；
2. `/usr/local/qcloud`、`/usr/local/sa`、`/usr/local/agenttools` 物理目录均已消除；
3. `pgrep -l -f '(tat_agent|barad|sgagent|yunjing)'` 无存活监控进程（返回码 1）；
4. `dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n'` 无任何处于 `installed` 状态的云监控包；
5. `systemctl list-unit-files` 无任何云监控残留 unit 文件（返回码 0 且输出为空）；
6. `ip -brief addr show` 仅收集并记录已识别的网络接口名称，不打印具体地址结构且不宣称双栈通用支持。
