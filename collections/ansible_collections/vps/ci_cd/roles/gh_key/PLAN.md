# Role: vps.ci_cd.github_deploy_key (PLAN)

## 📋 概述

该角色负责为 VPS 配置 GitHub Deploy Key，使其能够克隆和拉取私有仓库。

**设计原则：**
- 🔐 安全：使用 ED25519 密钥，权限最小化
- 🤖 自动化：通过 GitHub API 自动添加密钥
- 🔄 幂等性：重复执行不会产生副作用
- 📝 可追溯：密钥标题包含主机名和时间戳

---

## 🎯 功能范围

### 包含功能
- ✅ 生成 ED25519 SSH 密钥对
- ✅ 配置 `.ssh/config` 的 GitHub 条目
- ✅ 通过 GitHub API 添加 Deploy Key
- ✅ 测试 GitHub SSH 连接
- ✅ 克隆 AuroraOps 仓库（可选）
- ✅ 设置正确的文件权限

### 不包含功能
- ❌ SSH 服务器配置（由 `vps.system.ssh` 负责）
- ❌ 用户管理（由 `vps.system.user_management` 负责）
- ❌ 防火墙配置（由 `vps.system.firewall` 负责）

---

## 📦 变量定义

### 必需变量

| 变量名 | 类型 | 描述 | 示例 |
|--------|------|------|------|
| `github_repo` | string | GitHub 仓库（格式：owner/repo） | `suainam/AuroraOps` |
| `github_token` | string | GitHub Personal Access Token | `ghp_xxxx` (from vault) |

### 可选变量

| 变量名 | 默认值 | 描述 |
|--------|--------|------|
| `github_deploy_key_path` | `~/.ssh/github_key` | 密钥文件路径 |
| `github_deploy_key_type` | `ed25519` | 密钥类型 |
| `github_deploy_key_comment` | `{{ inventory_hostname }}@github` | 密钥注释 |
| `github_deploy_key_read_only` | `false` | 是否只读（false 允许推送） |
| `github_clone_repo` | `true` | 是否克隆仓库 |
| `github_clone_dest` | `~/AuroraOps` | 克隆目标路径 |
| `github_clone_version` | `main` | 克隆分支 |

---

## 🔄 任务流程

### Phase 1: 检测现有配置
```yaml
1. 检查 GitHub SSH 连接是否已工作
   - 执行: ssh -T git@github.com
   - 如果成功 → 跳过后续步骤

2. 检查密钥文件是否存在
   - 路径: {{ github_deploy_key_path }}
   - 如果存在 → 跳过生成步骤

3. 检查 .ssh/config 是否有 GitHub 条目
   - 如果存在 → 跳过配置步骤
```

### Phase 2: 生成密钥
```yaml
1. 生成 ED25519 密钥对
   - 命令: ssh-keygen -t ed25519 -C "{{ github_deploy_key_comment }}" -f {{ github_deploy_key_path }} -N ""
   - 条件: 密钥不存在

2. 设置密钥权限
   - 私钥: 600
   - 公钥: 644
   - .ssh 目录: 700
```

### Phase 3: 配置 SSH
```yaml
1. 读取现有 .ssh/config
   - 检查是否已有 GitHub 配置

2. 添加/更新 GitHub 配置
   - 使用 blockinfile 模块
   - 标记: # {mark} ANSIBLE MANAGED - GitHub Deploy Key
   - 内容:
     Host github.com
         HostName github.com
         User git
         IdentityFile {{ github_deploy_key_path }}
         StrictHostKeyChecking accept-new
         IdentitiesOnly yes
```

### Phase 4: 添加到 GitHub
```yaml
1. 读取公钥内容
   - 使用 slurp 模块

2. 检查密钥是否已存在
   - API: GET /repos/{owner}/{repo}/keys
   - 匹配: title 包含 inventory_hostname

3. 添加 Deploy Key (如果不存在)
   - API: POST /repos/{owner}/{repo}/keys
   - Body:
     {
       "title": "AuroraOps {{ inventory_hostname }} - {{ ansible_date_time.iso8601 }}",
       "key": "{{ public_key_content }}",
       "read_only": {{ github_deploy_key_read_only }}
     }
   - 条件: github_token 已定义

4. 手动添加提示 (如果无 token)
   - 暂停执行
   - 显示公钥内容
   - 提示用户手动添加
```

### Phase 5: 验证
```yaml
1. 测试 GitHub SSH 连接
   - 命令: ssh -T git@github.com
   - 预期: "successfully authenticated"

2. 克隆仓库 (可选)
   - 使用 git 模块
   - 条件: github_clone_repo == true
   - 目标: {{ github_clone_dest }}
```

---

## 📁 文件结构

```
collections/ansible_collections/vps/ci_cd/roles/github_deploy_key/
├── README.md                 # 用户文档
├── PLAN.md                   # 本设计文档
├── defaults/
│   └── main.yml             # 默认变量
├── tasks/
│   ├── main.yml             # 主任务入口
│   ├── detect.yml           # 检测现有配置
│   ├── generate_key.yml     # 生成密钥
│   ├── configure_ssh.yml    # 配置 SSH
│   ├── add_to_github.yml    # 添加到 GitHub
│   └── verify.yml           # 验证连接
├── templates/
│   └── ssh_config_github.j2 # SSH config 模板
└── meta/
    └── main.yml             # 角色元数据
```

---

## 🔐 安全考虑

### 1. 密钥管理
- ✅ 使用 ED25519（比 RSA 更安全）
- ✅ 私钥权限 600（仅所有者可读写）
- ✅ 不在日志中显示私钥内容
- ✅ GitHub Token 从 Vault 读取

### 2. API 安全
- ✅ Token 使用 `no_log: true`
- ✅ API 请求使用 HTTPS
- ✅ 验证 API 响应状态码

### 3. 权限最小化
- ✅ Deploy Key 默认允许推送（可配置为只读）
- ✅ 密钥仅用于特定仓库
- ✅ 不使用个人 SSH 密钥

---

## 🧪 测试计划

### 单元测试
```bash
# 1. 测试密钥生成
ansible-playbook -i localhost, playbooks/test_github_key.yml --tags generate

# 2. 测试 SSH 配置
ansible-playbook -i localhost, playbooks/test_github_key.yml --tags configure

# 3. 测试 API 调用（需要 token）
ansible-playbook -i localhost, playbooks/test_github_key.yml --tags api
```

### 集成测试
```bash
# 完整流程测试
make deploy-ci_cd.github_deploy_key

# 验证
ssh -T git@github.com
git clone git@github.com:suainam/AuroraOps.git /tmp/test-clone
```

### 幂等性测试
```bash
# 重复执行应该不产生变化
make deploy-ci_cd.github_deploy_key
make deploy-ci_cd.github_deploy_key  # 第二次应该全部 ok=X changed=0
```

---

## 📝 使用示例

### 基本使用
```yaml
# playbooks/setup_github.yml
- hosts: all
  roles:
    - vps.ci_cd.github_deploy_key
  vars:
    github_repo: "suainam/AuroraOps"
    github_token: "{{ vault_github_token }}"
```

### 自定义配置
```yaml
- hosts: all
  roles:
    - vps.ci_cd.github_deploy_key
  vars:
    github_repo: "myorg/myrepo"
    github_token: "{{ vault_github_token }}"
    github_deploy_key_path: "~/.ssh/myrepo_key"
    github_deploy_key_read_only: true
    github_clone_repo: false
```

### 仅配置密钥（不克隆）
```yaml
- hosts: all
  roles:
    - vps.ci_cd.github_deploy_key
  vars:
    github_repo: "suainam/AuroraOps"
    github_token: "{{ vault_github_token }}"
    github_clone_repo: false
```

---

## 🔄 与其他角色的关系

### 依赖关系
- **无硬依赖**：该角色可以独立运行
- **建议顺序**：
  1. `vps.system.init` (安装基础包)
  2. `vps.system.ssh` (配置 SSH 服务器)
  3. `vps.ci_cd.github_deploy_key` (配置 GitHub 访问)

### 被依赖关系
- 其他需要克隆私有仓库的角色可以依赖此角色

---

## 📊 实现优先级

| 阶段 | 功能 | 优先级 | 状态 |
|------|------|--------|------|
| 1 | 设计文档 | 高 | ✅ 完成 |
| 2 | 基础任务（生成密钥、配置 SSH） | 高 | 📝 待实现 |
| 3 | GitHub API 集成 | 中 | 📝 待实现 |
| 4 | 验证和测试 | 中 | 📝 待实现 |
| 5 | 文档和示例 | 低 | 📝 待实现 |

---

## 🚀 后续改进

### v1.0 (MVP)
- ✅ 基础密钥生成和配置
- ✅ GitHub API 集成
- ✅ 基本验证

### v1.1 (增强)
- 🔄 支持多个 GitHub 账户
- 🔄 密钥轮换功能
- 🔄 密钥过期检测

### v2.0 (高级)
- 🔄 支持 GitLab、Bitbucket
- 🔄 密钥管理 UI
- 🔄 审计日志

---

## 📚 参考资料

- [GitHub Deploy Keys API](https://docs.github.com/en/rest/deploy-keys)
- [SSH Key Generation Best Practices](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)
- [Ansible Git Module](https://docs.ansible.com/ansible/latest/collections/ansible/builtin/git_module.html)

---

**状态：** 📝 设计阶段  
**创建时间：** 2026-02-12  
**最后更新：** 2026-02-12  
**负责人：** OpenCode Agent
