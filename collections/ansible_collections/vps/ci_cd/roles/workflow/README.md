# Role: vps.ci_cd.workflow

## 1. 概述

该角色负责渲染并校验仓库内的 GitHub Actions 工作流文件。它运行在控制端，本地生成 `.github/workflows/*.yml`，再用 `verify` 确认文件存在。

## 2. 变量说明

| 变量名 | 默认值 | 描述 |
| :--- | :--- | :--- |
| `github_workflows_dir` | `.github/workflows` | 工作流目录。 |
| `github_workflows_to_deploy` | 见 `defaults/main.yml` | 工作流文件名与模板名。 |

## 3. 角色边界

- 只管理仓库内工作流文件，不负责远程主机部署。
- 适合模板化生成和一致性校验。
- 新增 workflow 时，优先在 `inventories/group_vars/all/` 放配置，在 `templates/` 放 Jinja 模板。

## 4. 内部逻辑

- `main.yml` 使用 `ansible.builtin.template` 将模板渲染到 `.github/workflows/`。
- `verify.yml` 用 `stat` + `assert` 检查文件是否存在。
- `rollback.yml` 仅给出回滚提示，实际回退通过恢复旧模板或重新渲染旧版本完成。

## 5. 依赖关系

- 依赖控制端存在 `make`、`ansible` 和仓库工作区。
- 工作流模板应保持与现有 `make` 命令和角色边界一致。

## 6. 维护与排查

- 新 workflow 先加模板，再加到 `github_workflows_to_deploy`。
- 如果渲染后内容不对，优先检查模板变量和 inventory 中的映射数据。
- 如果只想校验文件是否齐全，运行 `make verify-ci_cd.workflow`。
