"""xana 主目录的唯一解析点:$XANA_HOME,缺省 ~/.xana。只依赖标准库,任何模块都可导入。"""
from __future__ import annotations

import os
from pathlib import Path


def xana_home() -> Path:
    override = os.environ.get("XANA_HOME")
    return Path(override).expanduser() if override else Path.home() / ".xana"
