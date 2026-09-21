# Role: vps.observability.monitoring_stack

## 1. 概述
该角色用于部署基于 Docker Compose 的监控栈，默认包含 Prometheus 和相关配置，用于收集和存储系统的监控指标。

## 2. 变量说明
| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `monitoring_stack_dir` | `/opt/monitoring` | 监控栈配置文件和数据存储的目录。 |
| `monitoring_stack_prometheus_port` | `monitoring_prometheus_port` 或 `39090` | Prometheus 对外端口；兼容旧 inventory 输入。 |
| `monitoring_stack_grafana_port` | `monitoring_grafana_port` 或 `33000` | Grafana 对外端口；兼容旧 inventory 输入。 |
| `monitoring_stack_node_exporter_port` | `monitoring_node_exporter_port` 或 `39100` | Node Exporter 端口；兼容旧 inventory 输入。 |
| `monitoring_stack_domain` | `monitoring_domain` 或 `mt.msuai.top` | 外部访问域名；兼容旧 inventory 输入。 |
| `monitoring_stack_grafana_root_url` | 由域名生成 | Grafana 外部 URL；兼容旧 `monitoring_grafana_root_url`。 |
| `monitoring_stack_prometheus_external_url` | 由域名生成 | Prometheus 外部 URL；兼容旧 `monitoring_prometheus_external_url`。 |
| `monitoring_stack_grafana_admin_password` | `monitoring_grafana_admin_password` 或 `admin` | Grafana 初始管理员密码；生产 inventory 必须覆盖。 |

## 3. 内部逻辑
1. **环境准备**: 创建必要的目录并设置权限。
2. **配置分发**: 渲染并部署 `prometheus.yml` 和 `docker-compose.yml` 模板。
3. **容器编排**: 使用 `docker_compose_v2` 启动或更新监控容器。

## 4. 依赖关系
- 角色依赖: 需要预先安装 `vps.services.docker`。
- Python 插件: 需要 `community.docker` 集合。

## 5. 维护与排查
- **查看容器状态**: `docker compose -f /opt/monitoring/docker-compose.yml ps`。
- **检查 Prometheus 日志**: `docker logs monitoring-prometheus` (假设容器名为此)。
- **配置热重载**: 如果修改了 `prometheus.yml`，可以通过 API 触发重载：`curl -X POST http://localhost:9090/-/reload`。
