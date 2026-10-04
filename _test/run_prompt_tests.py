#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证交互提问: 模拟终端 + 喂回答, 看脚本问得对不对、解析得对不对。

用 pty 在 Windows 上不好使, 所以这里直接测两个更可靠的层面:
  1. 非终端环境下必须"明确报错"而不是挂住
  2. parse_row_range 对各种回答的解析
  3. 用 monkeypatch 把 _stdin_is_interactive 和 _ask_line 换掉, 跑完整流程
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import github_inviter as g  # noqa: E402

PASS, FAIL = [], []


def report(title: str, problems: list[str]) -> None:
    (PASS if not problems else FAIL).append(title)
    print(f"[{'PASS' if not problems else 'FAIL'}] {title}")
    for p in problems:
        print(f"         {p}")


work = Path(__file__).resolve().parent / "_tmp_ask"
work.mkdir(exist_ok=True)
weird = work / "weird.csv"
weird.write_text("日期,QQ,微信,怎么称呼,备注\n"
                 "2026-03-01,zhangsan,zhangsan@example.com,张三,组长\n"
                 "2026-03-01,lisi-2026,lisi@example.com,李四,\n",
                 encoding="utf-8")


def args(**kw) -> argparse.Namespace:
    base = dict(cols="", email_col="", username_col="", sheet="",
                no_header=False, skip_rows=0)
    base.update(kw)
    return argparse.Namespace(**base)


def answers_from(items: list[str]):
    """做一个按顺序回答的假 input; 回答用完了就显式报错(而不是抛 StopIteration)。"""
    queue = list(items)

    def _ask(prompt: str) -> str:
        if not queue:
            raise AssertionError(f"脚本多问了不该问的问题: {prompt!r}")
        return queue.pop(0)

    return _ask


# ---------- 1. 行号解析 ----------
problems = []
for answer, want in [("2", (2, 11)), ("2-10", (2, 10)), ("2-", (2, 11)),
                     ("-5", (1, 5)), ("2~4", (2, 4)), ("  3  ", (3, 11)),
                     ("2 4", (2, 4)),
                     # 超出行数会被夹到表尾, 而不是留下一个无效行号
                     ("999", (11, 11)), ("5-999", (5, 11))]:
    try:
        got = g.parse_row_range(answer, 11)
        if got != want:
            problems.append(f"{answer!r} -> {got}, 期望 {want}")
    except SystemExit:
        problems.append(f"{answer!r} 不该报错")
for bad in ["abc", "", "5-2"]:
    try:
        g.parse_row_range(bad, 11)
        problems.append(f"{bad!r} 应该报错却没报")
    except SystemExit:
        pass
report("parse_row_range 解析各种回答", problems)

# ---------- 2. 非终端环境要明确报错(不能挂住) ----------
problems = []
real_isatty = sys.stdin.isatty
try:
    sys.stdin = io.StringIO("2\n")           # StringIO.isatty() -> False
    try:
        g.ask_data_rows([[str(i) for i in range(3)]])
        problems.append("非终端环境下居然没报错")
    except SystemExit as exc:
        if exc.code != 2:
            problems.append(f"退出码应为 2, 实际 {exc.code}")
finally:
    sys.stdin = sys.__stdin__

# 错误信息要给出两条出路
try:
    sys.stdin = io.StringIO("")
    g.ask_data_rows([["a"]])
except SystemExit:
    pass
finally:
    sys.stdin = sys.__stdin__
buf = io.StringIO()
old_stderr = sys.stderr
sys.stderr = buf
try:
    sys.stdin = io.StringIO("")
    g.ask_data_rows([["a"]])
except SystemExit:
    pass
finally:
    sys.stdin = sys.__stdin__
    sys.stderr = old_stderr
msg = buf.getvalue()
for needle in ["没法向你提问", "--cols", "终端"]:
    if needle not in msg:
        problems.append(f"报错里缺少 {needle!r}")
report("非终端环境明确报错且给出出路", problems)

# ---------- 3. 模拟人回答: 先答列顺序, 再答数据行范围 ----------
problems = []
orig_isatty, orig_ask = g._stdin_is_interactive, g._ask_line
try:
    g._stdin_is_interactive = lambda: True
    answers = iter(["-,username,email,name", "2"])      # 第1问列顺序, 第2问行范围
    g._ask_line = lambda prompt: next(answers)
    source = g._load_table_source(str(weird), args())
    if len(source.records) != 2:
        problems.append(f"应解析出 2 条(第2、3行), 实际 {len(source.records)}")
    if [r.row for r in source.records] != [2, 3]:
        problems.append(f"行号不对: {[r.row for r in source.records]}, 期望 [2, 3]")
    if [r.login for r in source.records] != ["zhangsan", "lisi-2026"]:
        problems.append(f"用户名不对: {[r.login for r in source.records]}")
    if [r.email for r in source.records] != ["zhangsan@example.com", "lisi@example.com"]:
        problems.append(f"邮箱不对: {[r.email for r in source.records]}")
    if source.columns.get("email") != 2 or source.columns.get("username") != 1:
        problems.append(f"列映射不对: {source.columns}")
finally:
    g._stdin_is_interactive, g._ask_line = orig_isatty, orig_ask
report("回答列顺序 + 行范围 -> 列和行都对", problems)

# ---------- 4. 模拟人回答 "0"(整张表都是数据) ----------
problems = []
try:
    g._stdin_is_interactive = lambda: True
    answers = iter(["-,username,email,name", "0"])
    g._ask_line = lambda prompt: next(answers)
    source = g._load_table_source(str(weird), args())
    if len(source.records) != 3:
        problems.append(f"应解析出 3 条, 实际 {len(source.records)}")
    if source.records[0].row != 1:
        problems.append(f"首行行号应为 1, 实际 {source.records[0].row}")
finally:
    g._stdin_is_interactive, g._ask_line = orig_isatty, orig_ask
report('回答 "0" -> 整张表都是数据', problems)

# ---------- 4.5 列顺序那问直接回车 -> 按老约定兜底 ----------
problems = []
try:
    g._stdin_is_interactive = lambda: True
    answers = iter(["", "2"])
    g._ask_line = lambda prompt: next(answers)
    source = g._load_table_source(str(weird), args())
    if source.columns.get("username") != 0 or source.columns.get("email") != 1:
        problems.append(f"回车应兜底成第1列用户名/第2列邮箱, 实际 {source.columns}")
finally:
    g._stdin_is_interactive, g._ask_line = orig_isatty, orig_ask
report("列顺序直接回车 -> 按老约定兜底", problems)

# ---------- 5. 表头认得出时不该提问 ----------
problems = []
normal = work / "normal.csv"
normal.write_text("姓名,邮箱,GitHub用户名\n甲,a@example.com,alice\n乙,b@example.com,bob\n",
                  encoding="utf-8")
try:
    g._stdin_is_interactive = lambda: (_ for _ in ()).throw(
        AssertionError("表头认得出来时不该提问!"))
    g._ask_line = lambda prompt: (_ for _ in ()).throw(
        AssertionError("表头认得出来时不该提问!"))
    source = g._load_table_source(str(normal), args())
    if len(source.records) != 2:
        problems.append(f"应解析出 2 条, 实际 {len(source.records)}")
finally:
    g._stdin_is_interactive, g._ask_line = orig_isatty, orig_ask
report("表头认得出 -> 完全不提问", problems)

# ---------- 6. 给了 --cols 且表头认不出 -> 只问行范围, 列按 --cols ----------
problems = []
try:
    g._stdin_is_interactive = lambda: True
    g._ask_line = lambda prompt: "2"        # 只该问一次(列已经由 --cols 指定)
    source = g._load_table_source(str(weird), args(cols="-,username,email,name,-"))
    if len(source.records) != 2:
        problems.append(f"应 2 条, 实际 {len(source.records)}")
    if source.columns.get("email") != 2:
        problems.append(f"--cols 的列映射应生效: {source.columns}")
finally:
    g._stdin_is_interactive, g._ask_line = orig_isatty, orig_ask
report("--cols + 表头认不出 -> 只问行范围, 列按 --cols", problems)

import shutil  # noqa: E402
shutil.rmtree(work, ignore_errors=True)

print(f"\n===== 交互提问测试: 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
for t in FAIL:
    print(f"  失败: {t}")
raise SystemExit(1 if FAIL else 0)
