## CF-Server-Monitor 探针

基于 **CF-Server-Monitor** 官方 Go 探针 `cf-probe` 的 Ansible 角色。使用 Cloudflare Workers + D1 + Durable Objects 部署的免费服务器监控系统的 Agent 端。

### 角色功能

- **一键部署**：下载最新版 cf-probe 二进制程序，配置并启动 systemd 服务
- **配置管理**：通过角色变量自定义 Server ID、上报间隔、探测节点、网络接口等
- **自动启用**：可选自动启动 systemd 服务并设置开机自启
- **安全无 root 依赖**：配合 `systemd --user` 或 root systemd 服务均可使用
- **完整回滚**：提供完整的卸载/回滚任务

### 先决条件

- 服务器已注册 CF-Server-Monitor，获取 **Server ID**、**Secret** 与 **Worker URL**（已在 `secrets/vault.yml` 中记录）
- Ansible 2.14+ 与 Python 3.11+

### 角色变量

```yaml
cf_probe_enabled: false              # 是否启用（手动设置或由 make 任务注入）
cf_probe_server_id: ""               # Server ID (必填)
cf_probe_secret: ""                  # Secret (从 vault 注入)
cf_probe_worker_url: "https://cf-server-monitor.suainam.workers.dev/update"
cf_probe_collect_interval: 2         # 秒
cf_probe_report_interval: 60         # 秒
cf_probe_ct_node: "gd-ct-dualstack.ip.zstaticcdn.com"
cf_probe_cu_node: "gd-cu-dualstack.ip.zstaticcdn.com"
cf_probe_cm_node: "gd-cm-dualstack.ip.zstaticcdn.com"
cf_probe_bd_node: ""
cf_probe_interface: ""
cf_probe_reset_day: 1
cf_probe_connection_mode: "auto"
cf_probe_ping_mode: "tcp"
cf_probe_auto_update: 0
cf_probe_debug: 0
cf_probe_install_dir: "/usr/local/bin"
cf_probe_config_dir: "/etc/config/cf-probe"
cf_probe_config_file: "/etc/config/cf-probe/config.conf"
cf_probe_arch: "{{ ansible_architecture }}"
cf_probe_download_url: ""          # 自动根据架构生成
```

### 部署示例

**方法一：使用 Make 任务（推荐）**

```bash
# 在 cc15 或 qqg1299 上执行
make switch_remote.<host> && make env_show && \
  cf_probe_server_id="<ID>" cf_probe_secret="<Secret>" \
  make deploy-observability.cf_probe
```

**方法二：在 host_vars 中手动注入**

```yaml
# inventories/host_vars/cc15.yml
cf_probe_enabled: true
cf_probe_server_id: "1d0060e5-d8e1-441f-b4b1-ae89d6086a34"
cf_probe_secret: "cfsm_74b947bfcd9473924c84fa528d84b0d2"
```

### 指标契约与归一化边界

- `cf-probe` 上报 `ram_used`、`ram_total`、`disk_used`、`disk_total`，单位均为 MiB；角色不再对这些值做乘除或百分比换算。权威采集实现见 [`cfsm-agent`](https://github.com/huilang-me/cfsm-agent)。
- Server Monitor 前端对有效值按 `used / total * 100` 计算并限制在 `0..100%`；本 Issue 要求无效/未知值显式标记，不得伪装成健康的 `0%`。当前上游实现对非有限值或非正分母回退为 `0%`，这是已记录的外部契约缺口，不能据此判定监控正常。权威展示实现见 [`CF-Server-Monitor`](https://github.com/huilang-me/CF-Server-Monitor)。
- 磁盘取探针可见根文件系统；内存沿用探针的 `/proc/meminfo` 可见总量与可回收内存语义。`memory.current` 包含页缓存等 cgroup 记账值，不能直接替代探针的用户态 `ram_used`。
- AuroraOps 不在部署层补偿外部监控的单位/分母转换。若要改为 cgroup `memory.current` 口径，应在 [`cfsm-agent`](https://github.com/huilang-me/cfsm-agent) 与 [`CF-Server-Monitor`](https://github.com/huilang-me/CF-Server-Monitor) 的 D1/UI 契约中联动变更；当前仓库仅记录该外部依赖，避免制造第二套上报协议。

### License

MIT