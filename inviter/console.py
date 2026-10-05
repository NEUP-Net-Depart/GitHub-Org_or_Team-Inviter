# -*- coding: utf-8 -*-
"""控制台输出与中文对齐小工具。

`display_width` / `pad` / `clip` 按**显示宽度**算: 中文/全角按 2 列, 这样终端里的
表格在等宽字体下才对得齐。
"""
from __future__ import annotations

import sys
import unicodedata
from datetime import datetime


def log(message: str = "") -> None:
    print(message, flush=True)


def die(message: str, code: int = 2) -> None:
    print(f"错误: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def display_width(text: str) -> int:
    """中文/全角按 2 列宽计算, 用来把表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def clip(text: str, width: int) -> str:
    """按显示宽度截断, 超出部分换成省略号。"""
    if display_width(text) <= width:
        return text
    out = ""
    used = 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > width - 1:
            break
        out += ch
        used += step
    return out + "…"
