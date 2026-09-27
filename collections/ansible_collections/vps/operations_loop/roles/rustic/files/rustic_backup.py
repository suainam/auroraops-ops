#!/usr/bin/env python3
"""
Rustic Backup Manager for AuroraOps
===================================
功能:
- 本地 + 云端并行备份
- 压缩级别 3
- 根据时间自动选择备份标签
- 使用排除文件
- 完善的日志和错误处理
"""

import os
import sys
import subprocess
import hashlib
import json
import logging
import shutil
import tempfile
import threading
import fcntl
import time
from datetime import datetime


def _load_environment_file():
    """Load a private KEY=value file before constants are initialized."""
    path = os.getenv("RUSTIC_ENV_FILE")
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\\\''))


_load_environment_file()


def _resolve_path(raw_path):
    """Safely expand user and environment variables for filesystem paths."""
    if not raw_path:
        return raw_path
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw_path)))
