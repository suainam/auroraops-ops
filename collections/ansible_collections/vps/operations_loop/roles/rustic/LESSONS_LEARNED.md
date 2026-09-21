# Rustic 备份清理优化 - 经验教训

## 问题复盘

### 核心问题：锁冲突导致清理失败

**根本原因**：
- 备份每10分钟执行，持有锁1-2分钟
- 清理使用 `--retry-lock 5m`，等待时间过长
- 清理期间新备份启动，创建新锁
- 清理重试时遇到新锁，失败退出
- 锁不断积累，gdrive仓库积压94个快照

### 具体错误链

```
16:25:54 - forget开始（带--retry-lock 5m）
16:30:00 - 备份开始，创建新锁
16:30:54 - forget重试，遇到16:30的新锁
16:30:57 - 报错：unable to create lock
```

### 遇到的技术问题

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 递归变量错误 | `{{ var \| default(var) }}` 自引用 | 直接赋值，不使用递归default |
| 陈旧锁未自动清理 | `is_stale_lock()` 解析锁时间失败 | 使用 `unlock --remove-all` 强制解锁 |
| 清理太慢 | `--retry-lock 5m` 等待5分钟 | 移除该参数或缩短到30秒 |
| retention太宽松 | `--keep-within 10d` 保留10天快照 | 手动清理，保留5个关键快照 |
| 定时器冲突 | 备份10分钟，清理6小时1次 | 改为20分钟备份，清理在备份后10分钟 |

### 最佳实践总结

#### 1. 定时器设计原则

**时间错开**：
```
备份: 00, 20, 40分
清理: 10, 30, 50分（备份后10分钟）
窗口期: 10分钟（足够清理完成）
```

**避免**：
- ❌ 清理使用 `--retry-lock 5m`（等待太久，易冲突）
- ❌ 清理和备份并行执行
- ❌ retention策略过于宽松（保留10天）

#### 2. 锁处理策略

**智能解锁**：
```python
# 只移除陈旧锁（>30分钟）
if is_stale_lock(lock_info):
    unlock --remove-all
else:
    wait_and_retry()
```

**避免**：
- ❌ 无条件 `unlock --remove-all`（可能破坏正在进行的备份）
- ❌ 忽略锁直接执行（导致冲突）

#### 3. 批量删除优化

**正确做法**：
```bash
# 先forget（标记删除）
Rustic forget --keep-last 3

# 再prune（回收空间）
Rustic prune
```

**避免**：
- ❌ 逐个快照删除（效率低）
- ❌ 多次prune（重复扫描仓库）

#### 4. 监控与告警

**快照数量监控**：
```python
# 超过15个快照告警
if snapshot_count > 15:
    alert("Backup cleanup needed")
```

**锁状态监控**：
```bash
# 检查锁持续时间
Rustic snapshots 2>&1 | grep "lock was created"
```

## 修复清单

### 已修复
- [x] 定时器时间错开（20分钟备份 + 10分钟清理窗口）
- [x] 移除 backup_all() 中的 prune_repo() 调用
- [x] 添加智能解锁逻辑（safe_unlock）
- [x] 手动清理gdrive积压（98→5个快照）
- [x] 使用变量配置定时器（支持host覆盖）
- [x] 调整keep-within为3h（避免保留过多快照）
- [x] 调整告警阈值为10个快照

### 配置默认值
```yaml
# defaults/main.yml
rustic_backup_timer_calendar: "*-*-* *:0/20"    # 00, 20, 40分
rustic_cleanup_timer_calendar: "*-*-* *:30"     # 每小时 :30 分
```

### 待优化
- [ ] 移除 `--retry-lock` 或缩短到30秒
- [ ] 增强陈旧锁检测（解析错误处理）
- [ ] 添加清理失败告警
- [ ] 优化批量删除（使用 forget --keep-last）

## 命令参考

### 手动清理
```bash
# 1. 解锁
Rustic unlock --remove-all

# 2. 查看快照
Rustic snapshots

# 3. 批量删除（保留最近3个）
Rustic forget --keep-last 3

# 4. 回收空间
Rustic prune
```

### 检查状态
```bash
# 查看定时器
systemctl list-timers rustic-*

# 查看锁状态
Rustic snapshots 2>&1 | grep lock

# 查看快照数量
Rustic snapshots --json | jq '. | length'
```

## 相关文件

- `defaults/main.yml` - 定时器配置变量
- `files/rustic_backup.py` - 备份脚本
- `tasks/cleanup.yml` - 清理定时器
- `tasks/deploy.yml` - 备份定时器
