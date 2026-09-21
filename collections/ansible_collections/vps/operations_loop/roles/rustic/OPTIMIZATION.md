# Rustic 备份清理优化方案

## 问题
- 备份每10分钟执行，清理也在备份后执行
- 清理慢（尤其gdrive），导致锁冲突
- gdrive积压94个快照无法清理

## 解决方案

### 1. 时间分离（避免冲突）
```yaml
# defaults/main.yml
rustic_backup_timer_calendar: "*-*-* *:0/20"    # 每20分钟（00, 20, 40分）
rustic_cleanup_timer_calendar: "*-*-* *:30"     # 每小时 :30 分
```

**时间线示例**：
```
16:00 - 备份开始
16:01 - 备份完成（约1分钟）
16:30 - 清理开始（备份后30分钟）
16:40 - 下次备份
```

### 2. 职责分离
- **backup_all()**: 只备份，不清理
- **prune_all()**: 独立清理，可指定仓库 `--repo gdrive`

### 3. 智能解锁
- 只移除陈旧锁（>30分钟）
- 新锁（正在备份）则等待重试

### 4. 批量删除优化
- 一次性传递所有要删除的快照ID
- 只执行一次prune（回收空间）

## 当前部署状态

### 已修改文件
1. `rustic_backup.py` - 优化清理逻辑，支持指定仓库
2. `defaults/main.yml` - 添加定时器变量
3. `tasks/deploy.yml` - 使用变量
4. `tasks/cleanup.yml` - 新增独立清理定时器
5. `tasks/main.yml` - 导入cleanup任务

### 定时器配置
- **备份**: 每20分钟 (`*:0/20`)
- **清理**: 每6小时的05分 (`00/6:05`)

## 手动清理积压

```bash
# 只清理gdrive（在cc15上执行）
/usr/local/bin/rustic_backup.py cleanup --repo gdrive
```

## 验证

```bash
# 检查定时器
systemctl list-timers rustic-*

# 检查快照数量
Rustic -r rclone:gdrive:backup/cc15/complete snapshots | wc -l
```
