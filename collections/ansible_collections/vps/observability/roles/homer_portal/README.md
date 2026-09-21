# Homer Web Portal Role (Cloudflare Pages)

将 **Homer** 静态轻量服务导航门户自动构建并发布到 **Cloudflare Pages**。

## 功能特性

- **Serverless 零服务器占用**：完全托管在 Cloudflare 全球 CDN 边缘网络，免 VPS 资源开销。
- **全服务自动聚合**：
  - 监控大盘：`CF-Server-Monitor` (探针大盘)、`AuroraOps Uptime` (心跳监控)
  - AI 接口与中继：`New API`、`GCLI2API`、`CLI Proxy API`、`DS2API`、`Vertex AI Proxy`、`opctoai`、`OpenCode`
  - 生产力工具：`Vaultwarden`、`SillyTavern`、`Pi Terminal (OMP)`、`Sub-Store`、`Singbox Optimizer`
- **声明式配置**：通过 Jinja2 模板动态渲染 `assets/config.yml`，支持自由扩展分组与卡片。
- **一键自动化发布**：通过 `wrangler pages deploy` 自动上传至 Cloudflare Pages。

## 角色变量

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `homer_portal_enabled` | `true` | 是否启用该角色 |
| `homer_portal_project_name` | `"homer-portal"` | Cloudflare Pages 项目名称 |
| `homer_portal_title` | `"Suai Operations Hub"` | 门户站点标题 |
| `homer_portal_subtitle` | `"Cloud & Self-Hosted Services"` | 门户副标题 |
| `homer_portal_version` | `"v26.08.3"` | Homer 官方发布版本 |
| `homer_portal_services` | `[...]` | 分组服务列表 |
| `homer_portal_links` | `[...]` | 顶部导航快捷链接 |

## 验证地址

部署完成后，访问：
`https://<homer_portal_project_name>.pages.dev`
例如：`https://homer-portal.pages.dev`
