# -*- coding: utf-8 -*-
"""交互提问: 表头认不出来时, 问人「哪一列是什么」「哪几行是数据」。

回答格式与 `--cols` 完全一致, 学一次两边都能用。

**这里是测试唯一替换过的模块**: `_test/run_prompt_tests.py` 会把
`_stdin_is_interactive` 和 `_ask_line` 换掉来模拟人回答。因此调用方必须写成
`prompt._stdin_is_interactive()`(模块限定), 不要 `from .prompt import ...`
—— 后者会让 patch 点分裂成两个, 测试会静默失效(踩过一次)。
"""
from __future__ import annotations

import re
import sys
from typing import Sequence

from .columns import _blank_cols, parse_cols_spec
from .console import clip, die, log


def _stdin_is_interactive() -> bool:
    """有没有人在终端前面等着回答问题。"""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_line(prompt: str) -> str:
    """问一句并等回答。Ctrl-C / Ctrl-D 就当成放弃, 别抛一堆栈。"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        die("已取消。", 2)


def parse_row_range(answer: str, total: int) -> tuple[int, int]:
    """解析「哪几行是数据」的回答, 返回 (起始行号, 结束行号), 都是 1-based 且含两端。

    接受: "2" / "2-10" / "2-" / "-10" / "2~10" / "2 10"
    """
    text = (answer or "").strip().replace("~", "-").replace("－", "-")
    text = re.sub(r"[,\s]+", "-", text)
    if not text.strip("-"):
        die("没听明白: 回答是空的。请写行号, 例如 2 表示从第 2 行开始, 或 2-10 表示到第 10 行。")

    # 前导 '-' 表示"从第 1 行开始", 所以先把它单独记下来再合并多余的连字符,
    # 否则 "-5" 会被 strip 成 "5-" 而变成"第5行到结尾"(踩过这个)。
    starts_at_first = text.startswith("-")
    body = re.sub(r"-+", "-", text.strip("-")).strip("-")
    if not body:
        die(f"没听明白: {answer!r}。请写行号, 例如 2 或 2-10。")

    parts = body.split("-")
    try:
        if len(parts) == 1:
            start = 1 if starts_at_first else int(parts[0])
            end = int(parts[0]) if starts_at_first else total
        else:
            start = int(parts[0]) if parts[0] else 1
            end = int(parts[1]) if parts[1] else total
    except ValueError:
        die(f"没听明白: {answer!r}。请写行号, 例如 2 表示从第 2 行开始, 或 2-10 表示到第 10 行。")

    start = max(1, min(start, total))
    end = max(1, min(end, total))
    if start > end:
        die(f"行号反了: 起始 {start} 大于结束 {end}(这张表共 {total} 行)。")
    return start, end


def show_rows(rows: Sequence[Sequence[str]], count: int = 5, width: int = 60) -> None:
    """把表格前几行带行号打印出来, 好让人指出数据从哪行开始。"""
    log("")
    log(f"  表格前 {min(count, len(rows))} 行(共 {len(rows)} 行):")
    for index, row in enumerate(rows[:count], start=1):
        cells = " | ".join(str(c) for c in row)
        log(f"    第 {index} 行: {clip(cells, width)}")
    log("")


def ask_data_rows(rows: Sequence[Sequence[str]]) -> tuple[int, int]:
    """认不出表头时, 问人「从第几行到第几行是数据」。

    返回 (起始行号, 结束行号), 1-based 含两端。回答 0 表示整张表都是数据。
    """
    if not _stdin_is_interactive():
        die("表头认不出来, 而这里没法向你提问(输入不是终端)。\n"
            "两种办法:\n"
            "  1) 在终端里直接运行这个命令, 脚本会问你数据从第几行开始;\n"
            '  2) 用 --cols 按位置说明数据列, 例如 --cols "-,username,email,name,-"\n'
            "     (位置从 1 开始, 不用的列写 '-')。")
    show_rows(rows)
    log("  我是从表头名字认列的, 这张表认不出来。请告诉我:")
    log("    · 数据从第几行开始   直接写行号, 例如 2")
    log("    · 到第几行结束       例如 2-10; 只写 2 表示一直到表尾")
    log("    · 整张表都没有表头   写 0")
    answer = _ask_line("  数据行范围(例如 2 或 2-10, 写 0 表示全是数据): ")
    if answer.strip() in ("0", "无", "没有", "-"):
        return 1, len(rows)
    return parse_row_range(answer, len(rows))


def ask_columns(rows: Sequence[Sequence[str]], width: int) -> dict[str, int]:
    """认不出哪一列是什么时, 问人一句, 回答格式和 --cols 一样。

    留空就按老约定兜底(第1列用户名、第2列邮箱)。
    """
    if not _stdin_is_interactive():
        die("认不出哪一列是邮箱、哪一列是用户名, 而这里没法向你提问(输入不是终端)。\n"
            "请在终端里直接运行, 或用 --cols 按位置说明, 例如:\n"
            '  --cols "-,username,email,name,-"   (位置从 1 开始, 不用的列写 \'-\')')

    log("")
    log(f"  这一行数据长这样(共 {width} 列):")
    for index, cell in enumerate(rows[0] if rows else [], start=1):
        log(f"    第 {index} 列: {clip(str(cell), 40)}")
    log("")
    log("  我没认出哪一列是邮箱、哪一列是用户名。请按位置告诉我, 和 --cols 一个写法:")
    log("    第1列姓名、第2列用户名、第3列邮箱  就写  name,username,email")
    log("    不想用的列写 -                       就写  -,username,email,name")
    answer = _ask_line("  列的顺序(直接回车 = 按老约定: 第1列用户名、第2列邮箱): ")
    if not answer.strip():
        fallback = _blank_cols()
        fallback["username"] = 0
        fallback["email"] = 1 if width >= 2 else None
        return fallback
    cols = dict(parse_cols_spec(answer, width))
    if cols.get("email") is None and cols.get("username") is None:
        die(f'你填的 "{answer}" 里没有一列是 email 或 username, 没东西可发。\n'
            f'每项只能填 username / email / name, 不想用的列写 "-"。')
    return cols
