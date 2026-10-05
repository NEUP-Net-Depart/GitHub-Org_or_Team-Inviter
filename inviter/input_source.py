# -*- coding: utf-8 -*-
"""输入来源: 一个表格文件 + 可选的命令行补充。

这是唯一一条入口路径 —— 要发的一批人必须装在表格里; 命令行上再补一个
用户名/邮箱只适合临时试一个人。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import prompt
from .columns import _blank_cols, locate_header, norm_key, parse_cols_spec
from .console import die
from .constants import LOGIN_RE
from .records import Record, build_records, clean_email, clean_login
from .tableio import read_table


def records_from_tokens(tokens: Iterable[str]) -> list[Record]:
    """把命令行的 'alice' / 'a@b.com' / '@alice' 变成 Record。

    命令行上的东西是人当场敲的, 所以认不出来就直接报错 —— 不像表格里的脏数据
    那样只给提醒, 免得把一个笔误静默地当成用户名发出去。
    """
    records: list[Record] = []
    for index, raw in enumerate(tokens, start=1):
        text = str(raw).strip()
        if not text:
            continue
        email, _ = clean_email(text)
        if email:
            records.append(Record(row=index, email=email))
            continue
        login = clean_login(text)
        if not login:
            die(f"看不懂这个输入: {text}\n"
                f"要么是邮箱(如 someone@example.com), 要么是 GitHub 用户名(如 @someone)。")
        if not LOGIN_RE.match(login):
            die(f'"{text}" 不是合法的 GitHub 用户名: {login}\n'
                f"GitHub 用户名只能用字母、数字和单个连字符, 且不能以连字符开头或结尾。\n"
                f"(如果这是邮箱, 检查一下是不是漏了 @ 或打错了)")
        records.append(Record(row=index, login=login))
    return records


@dataclass
class InputSource:
    records: list[Record]
    columns: dict[str, int | None]
    header: list[str]
    origin: str
    table_path: Path | None = None
    data_rows: int = 0          # 表里除表头以外的行数
    file_rows: int = 0          # 表里总行数(含表头)


def resolve_input(args: argparse.Namespace) -> InputSource:
    """位置参数 = 必填的表格文件 + 可选的命令行补充输入。

    只有这一条路径: 要发的一批人必须装在表格里。一个人时可以顺手写在命令行上,
    但那只适合临时试一个, 不适合当常规用法。
    """
    targets = [str(t) for t in (args.targets or [])]
    if not targets:
        die(_usage_hint())

    files = [t for t in targets if Path(t.strip('"').strip("'")).is_file()]
    if not files:
        die(f"找不到表格文件: {targets[0]}\n\n{_usage_hint()}")
    if len(files) > 1:
        die("一次只能给一个表格文件。要邀请很多人请放在同一张表里。")
    if len(targets) > 2:
        die("最多给两个输入: 一个表格文件, 加一个可选的用户名/邮箱。\n"
            "要一次邀请很多人, 请把它们放进一张表里。")

    source = _load_table_source(files[0], args)
    extras = [t for t in targets if t not in files]
    if extras:
        # 命令行上额外写的人是当场敲的, 认不出来直接报错, 不给"可疑"提醒
        extra_records = records_from_tokens(extras)
        offset = source.data_rows
        for index, record in enumerate(extra_records, start=1):
            record.row = offset + index
        source.records.extend(extra_records)
        source.data_rows += len(extra_records)
        source.file_rows += len(extra_records)
        source.origin += f" + 命令行上的 {len(extra_records)} 个输入"
    return source


def _load_table_source(target: str, args: argparse.Namespace) -> InputSource:
    all_rows = read_table(target, args.sheet)
    if not all_rows:
        die(f"{target}: 表格是空的。")
    if not any(str(cell).strip() for row in all_rows for cell in row):
        die(f"{target}: 表格里没有任何内容。")

    rows = all_rows
    width = max((len(row) for row in rows), default=0)
    cols = _blank_cols()
    header: list[str] = []
    # --skip-rows 显式给过就用它, 不再自动认表头、也不再提问
    skip = max(0, int(getattr(args, "skip_rows", 0) or 0))
    if skip >= len(rows):
        die(f"--skip-rows {skip} 超过表里的行数(共 {len(rows)} 行), 没数据可处理了。")
    skip_given = skip > 0

    # 表头识别: 在前几行里找最像表头的一行。--no-header 或 --skip-rows 给了就不认。
    if args.no_header or skip_given:
        detected_index, detected_cols = -1, _blank_cols()
    else:
        detected_index, detected_cols = locate_header(rows)

    # ---- 第 1 步: 定列(哪一列是什么) ----
    # 与「数据从第几行开始」是两件独立的事。
    if args.cols:
        cols = dict(parse_cols_spec(args.cols, width))
        if cols.get("email") is None and cols.get("username") is None:
            die(f'--cols "{args.cols}" 里没有一列是 email 或 username, 没东西可发。\n'
                f'每项只能填 username / email / name, 不想用的列写 "-" 占位。\n'
                f'例如: --cols "name,email,username"')
    elif args.no_header or skip_given:
        # 用户明确说了没有表头 / 跳过表头, 就按老约定: 第1列用户名、第2列邮箱。
        # 不提问 —— 参数已经回答了。
        cols["username"], cols["email"] = 0, (1 if width >= 2 else None)
    elif detected_index >= 0:
        header = [str(c) for c in rows[detected_index]]
        cols = dict(detected_cols)
    else:
        # 表头认不出来, 也不该在这里按位置"悄悄兜底" —— 兜底就没人会去问用户了。
        # 留空, 下面统一走提问(或非终端时报错)。
        pass

    if args.email_col:
        cols["email"] = _index_of(header or [str(c) for c in rows[0]], args.email_col, width)
    if args.username_col:
        cols["username"] = _index_of(header or [str(c) for c in rows[0]], args.username_col, width)

    if cols.get("email") is None and cols.get("username") is None:
        # 表头名字认不出、也没给 --cols / --no-header / --email-col / --username-col。
        # 以前这里是直接报错退出; 现在改成问一句 —— 人能回答就不必再翻文档找参数。
        # (如果显式给了 --cols 却一列都没对上, 上面的 --cols 校验已经报过错, 走不到这。)
        if detected_index >= 0:
            die("没认出哪一列是邮箱、哪一列是用户名。\n"
                f"表头是: {' | '.join(str(c) for c in rows[0])}\n"
                '请用 --cols "name,email,username" 按位置指定 '
                "(位置从 1 开始, 不想用的列写 '-'), 或用 --email-col / --username-col 指定列名。")
        # 注意: 这两个提问函数必须走 prompt.<名字> 调用 —— 测试就是替换它们的。
        cols = prompt.ask_columns(rows, width)

    # ---- 第 2 步: 定数据行范围 ----
    if skip_given:
        # 用户明确给了 --skip-rows, 以它为准, 不再提问
        data_start, data_end = skip + 1, len(rows)
    elif args.no_header:
        # --no-header 就是"整张表都是数据"的意思, 不必再问
        data_start, data_end = 1, len(rows)
    elif detected_index >= 0:
        # 表头认得出: 表头下面就是数据(表头可能不在第 1 行, 上面有标题行也一并跳过)
        data_start, data_end = detected_index + 2, len(rows)      # 1-based, 含两端
    else:
        # 认不出表头 —— 第 1 行是表头还是数据, 脚本猜不了, 问人。
        # 猜错会把表头当成人发出去, 所以这里宁愿打断一下。
        data_start, data_end = prompt.ask_data_rows(rows)

    data_rows = rows[data_start - 1:data_end]
    records = build_records(data_rows, cols)
    for record in records:
        record.row += data_start - 1   # 换算回表格里的真实行号

    return InputSource(
        records=records,
        columns=cols,
        header=header,
        origin=f"表格 {Path(target).name}",
        table_path=Path(target),
        data_rows=len(data_rows),
        file_rows=len(all_rows),
    )


def _index_of(header: Sequence[str], name: str, width: int) -> int:
    target = norm_key(name)
    for index, column in enumerate(header):
        if norm_key(str(column)) == target:
            return index
    if name.strip().isdigit():
        index = int(name.strip()) - 1
        if width and index >= width:
            die(f"指定的列位置 {name} 超出表宽(这张表只有 {width} 列)。")
        return index
    die(f"表头里找不到列: {name}\n现有表头: {' | '.join(str(c) for c in header)}")


def _usage_hint() -> str:
    return ("必须给一个表格文件:\n"
            "  python github_inviter.py --org my-org \"收集表.xlsx\"\n"
            "  python github_inviter.py --org my-org \"收集表.csv\"\n"
            "表里至少有「邮箱」或「GitHub用户名」的一列。示例见 docs/examples/")
