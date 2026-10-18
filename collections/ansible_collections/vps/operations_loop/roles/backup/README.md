# Role: vps.operations_loop.backup (RETIRED / OBSOLETE)

> **Phase**: phase7 (backup operations) - RETIRED
> **Status**: Legacy unified backup stack (`unified-backup.service`, `manage_docker_data.py`, `verify_backup_system.py`) has been fully retired and removed.
> **Replacement**: Replaced entirely by Rustic (`vps.operations_loop.rustic`).

## 1. 概述
旧版统一备份体系（基于 `manage_docker_data.py` 和 `verify_backup_system.py`）已彻底退役并从代码库中移除。现已全面切换至 Rust 编写的现代化内存去重备份工具 **Rustic**。

本角色保留仅用于对可能残存在旧节点上的 `unified-backup.service` 和 `unified-backup.timer` 进行清理（需显式开启 `backup_manage_retired_units: true`）。
