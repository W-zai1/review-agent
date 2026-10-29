#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""version.py —— 版本号的唯一事实源。

此前版本号散在三处且互相矛盾：README 标题 v0.2.7、报告 `model_version`
v0.2.7-demo、控制台 `ReviewAgentConsole/0.1`，而 README 正文已经写到 v0.8.2。
读的人无法判断“这份报告是哪个版本出的”。

约定：
- 根目录 `VERSION` 是唯一可写处；
- 代码/文档只读它，不各自硬编码；
- 读不到时返回 `0.0.0+unknown`，不抛异常（版本号坏了不该让审查跑不起来）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
UNKNOWN = "0.0.0+unknown"


def read_version(default: str = UNKNOWN) -> str:
    """读取根目录 VERSION；缺失/为空/读失败时返回 default。"""
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return value or default


__version__ = read_version()


def report_version() -> str:
    """报告里记录的版本串。保留 -demo 后缀，和 README 的定位一致。"""
    return f"v{__version__}-demo"
