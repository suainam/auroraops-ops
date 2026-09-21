# auroraops-ops Agent Operating Directives

## 1. 仓库定位与职责 (Child Capability Repository)
- **定位**：公开子仓库，提供主机日常维护、健康巡检、备份灾备、系统级压测、破坏性重装以及自建 CI/CD Runner 调度。
- **边界**：
  - 承载自动化运维循环 (Operations Loop) 与稳定性保障体系。
  - **严禁**包含通用 OS 引导（属于 `auroraops-base`）或业务中间件应用（属于 `auroraops-services`）。
  - **特种高危操作隔离**：重装 (`reinstall`) 等高危破坏性脚本必须具备明确的交互或保护开关。
- **父仓关系**：被父仓 `auroraops-control` 通过 Release Tag + Commit Hash 精确锁定使用。

---

## 2. 包含角色列表 (共 19 个)
- **备份与灾备** (4): `backup`, `rustic`, `vaultwarden_snapshot`, `vaultwarden_standby`
- **周期维护与巡检** (4): `audit`, `cleanup`, `update`, `dist_upgrade`
- **监控与可观测** (4): `monitoring_stack`, `health_checks`, `cf_probe`, `homer_portal`
- **特种诊断与高危运维** (3): `benchmark` (性能压测), `reinstall` (破坏性重装), `systemd_priority` (调度优先级)
- **CI/CD 与自动化** (4): `github_actions_watchdog`, `gh_key`, `runner`, `workflow`

---

## 3. 开发与测试指南
1. **修改 Role**：
   - 维护/诊断类角色应保证只读安全，不随意篡改已有服务数据。
2. **重新生成 Playbook**：
   ```bash
   python3 scripts/generate_ansible_playbooks.py
   ```
3. **语法检查**：
   ```bash
   ansible-playbook -i localhost, playbooks/deploy.yml --syntax-check
   ```
4. **提交与推送**：
   - 在本仓库的 `main` 或特性分支提交并推送。
