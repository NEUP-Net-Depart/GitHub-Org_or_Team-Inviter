#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量邀请人加入 GitHub 组织, 可选顺手加入指定 Team。

一个脚本, 一份输入, 两档动作:

  第 1 档(默认)      组织邀请  POST /orgs/{org}/invitations
  第 2 档(加 --team)  Team      PUT /orgs/{org}/teams/{slug}/memberships/{username}

输入只有一种: 一张表(.xlsx / .csv)。表里至少要有「邮箱」或「GitHub用户名」一列 ——
有用户名就用用户名邀请(更可靠), 没有才退回邮箱。

默认是 dry-run: 不加 --execute 绝不发出任何邀请, 只解析 + 预检 + 打印计划。

实测出来的几条硬限制(详见 ARCHITECTURE.md):
  * 组织邀请可以只用邮箱, 但只有「对方已验证的邮箱」才点得动邀请链接;
    未验证时接口照样回 201。所以只有邮箱的行, 事后要用 --status 对一遍。
  * 已经是成员的人, 接口回 422 并明确说明, 且不会给对方发邮件。
  * Team 成员接口只收 GitHub 用户名, 不认邮箱; 且对方必须先成为组织成员。

本文件不出现任何真实姓名/邮箱/用户名/token。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

APP_NAME = "github-inviter"
APP_VERSION = "2.0"
DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

DEFAULT_DELAY = 2.0          # 每条之间的间隔秒数
DEFAULT_PAGE = 100           # 分页接口每页条数
MAX_RETRIES = 4              # 限流/网络类错误的重试次数

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,.]+(?:\.[^@\s,.]+)+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
GITHUB_URL_RE = re.compile(r"^\s*(?:https?://)?(?:www\.)?github\.com/", re.I)

TABLE_SUFFIXES = (".csv", ".xlsx", ".xlsm")


# ===========================================================================
# 1. 通用小工具
# ===========================================================================

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


def norm_key(value: str) -> str:
    """表头归一化: 去空白/下划线/连字符/括号/星号, 转小写。"""
    text = (value or "").strip().lower()
    return re.sub(r"[\s_\-/\\()（）\[\]:：*·]+", "", text)


# ===========================================================================
# 2. 表格读取 (csv / tsv / xlsx) —— 零第三方依赖
# ===========================================================================

def _local(tag: str) -> str:
    """{namespace}row -> row"""
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str) -> int:
    """A1 -> 0, C2 -> 2"""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return max(0, index - 1)


def _decode_bytes(raw: bytes) -> str:
    """按中文环境的现实情况依次尝试编码。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp936", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _guess_delimiter(text: str) -> str:
    """csv 可能是逗号/分号/制表符, 按第一行里出现次数最多的算。"""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    counts = {sep: first_line.count(sep) for sep in (",", ";", "\t")}
    best = max(counts, key=lambda sep: counts[sep])
    return best if counts[best] > 0 else ","


def _read_delimited(text: str, delimiter: str | None = None) -> list[list[str]]:
    sep = delimiter or _guess_delimiter(text)
    rows = [list(row) for row in csv.reader(io.StringIO(text), delimiter=sep)]
    cleaned = [[("" if cell is None else str(cell)).strip() for cell in row] for row in rows]
    # 完全空白的行(含末尾空行)直接丢掉: 它们不是数据, 报"排除了几行"只会让人困惑
    return [row for row in cleaned if any(cell for cell in row)]


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    out: list[str] = []
    for node in root:
        if _local(node.tag) != "si":
            continue
        out.append("".join(t.text or "" for t in node.iter() if _local(t.tag) == "t"))
    return out


def _xlsx_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回 [(sheet 名, zip 内路径)]。"""
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return [("Sheet1", "xl/worksheets/sheet1.xml")]

    rel_map: dict[str, str] = {}
    for rel in rels:
        rid = rel.get("Id") or ""
        target = (rel.get("Target") or "").lstrip("/")
        if not target:
            continue
        rel_map[rid] = target if target.startswith("xl/") else f"xl/{target}"

    out: list[tuple[str, str]] = []
    for node in workbook.iter():
        if _local(node.tag) != "sheet":
            continue
        rid = next((v for k, v in node.attrib.items() if _local(k) == "id"), "")
        out.append((node.get("name") or "", rel_map.get(rid or "", "xl/worksheets/sheet1.xml")))
    return out or [("Sheet1", "xl/worksheets/sheet1.xml")]


def _read_xlsx_minimal(path: Path, sheet_name: str = "") -> list[list[str]]:
    """不依赖 openpyxl 的最小 xlsx 读取器 —— xlsx 本质就是装了 XML 的 zip。"""
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _xlsx_shared_strings(zf)
            sheets = _xlsx_sheets(zf)
            if sheet_name:
                target = next((t for name, t in sheets if name == sheet_name), None)
                if target is None:
                    die(f"工作表不存在: {sheet_name}\n"
                        f"现有工作表: {' | '.join(name for name, _ in sheets)}")
            else:
                target = sheets[0][1]
            try:
                root = ET.fromstring(zf.read(target))
            except KeyError:
                die(f"xlsx 内部缺少工作表数据: {target} (文件可能损坏)")
    except zipfile.BadZipFile:
        die(f"读不了这个 xlsx: {path.name} (不是有效的 xlsx, 或文件已损坏)")

    rows: list[list[str]] = []
    for row_node in root.iter():
        if _local(row_node.tag) != "row":
            continue
        values: dict[int, str] = {}
        highest = -1
        fallback = 0
        for cell in row_node:
            if _local(cell.tag) != "c":
                continue
            ref = cell.get("r") or ""
            ctype = cell.get("t") or "n"
            col = _col_index(ref) if ref else fallback
            fallback = col + 1
            text = ""
            if ctype == "inlineStr":
                text = "".join(n.text or "" for n in cell.iter() if _local(n.tag) == "t")
            else:
                vnode = next((c for c in cell if _local(c.tag) == "v"), None)
                if vnode is not None and vnode.text is not None:
                    if ctype == "s":
                        try:
                            text = shared[int(vnode.text)]
                        except (ValueError, IndexError):
                            text = vnode.text
                    elif ctype == "b":
                        text = "TRUE" if vnode.text == "1" else "FALSE"
                    else:
                        text = vnode.text
            values[col] = text.strip()
            highest = max(highest, col)
        rows.append([values.get(i, "") for i in range(highest + 1)] if highest >= 0 else [])
    # 和 csv 一样: 完全空白的行不是数据
    return [row for row in rows if any(cell for cell in row)]


def _read_xlsx(path: Path, sheet_name: str = "") -> list[list[str]]:
    """有 openpyxl 就用它(对日期等格式更稳), 没有就用自带的最小解析器。"""
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError:
        return _read_xlsx_minimal(path, sheet_name)
    try:
        book = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 解析失败就回退到最小解析器
        log(f"  ! openpyxl 读不了这个文件({type(exc).__name__}), 改用内置解析器")
        return _read_xlsx_minimal(path, sheet_name)
    try:
        if sheet_name:
            if sheet_name not in book.sheetnames:
                die(f"工作表不存在: {sheet_name}\n"
                    f"现有工作表: {' | '.join(book.sheetnames)}")
            sheet = book[sheet_name]
        else:
            sheet = book.active
        return [["" if cell is None else str(cell).strip() for cell in row]
                for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()


def read_table(path: str, sheet_name: str = "") -> list[list[str]]:
    """读 csv / xlsx。返回逐行的字符串矩阵。

    只支持表格文件: 一个纯文本清单每行只有一格, 装不下「邮箱 + 用户名」,
    与其为它单独维护一套解析语义, 不如要求用户给表格。
    """
    file_path = Path(path)
    if not file_path.is_file():
        die(f"找不到文件: {file_path}")
    suffix = file_path.suffix.lower()

    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(file_path, sheet_name)
    if suffix in (".csv", ".tsv"):
        # .tsv 不算正式支持, 但它是分隔符明确的表格, 顺手认了
        text = _decode_bytes(file_path.read_bytes())
        return _read_delimited(text, "\t" if suffix == ".tsv" else None)
    if suffix == ".txt":
        die(f".txt 名单不再支持了: {file_path.name}\n"
            f"纯文本每行只有一格, 装不下「邮箱 + 用户名」两项, 也就分不出该用哪个邀请。\n"
            f"请改用表格(.csv 或 .xlsx), 列名写 邮箱 / GitHub用户名 即可。\n"
            f"示例见 docs/examples/")
    die(f"不支持的文件格式: {suffix or '(无扩展名)'}\n"
        f"支持: {', '.join(TABLE_SUFFIXES)}")


# ===========================================================================
# 3. 列识别
# ===========================================================================

EMAIL_HEADERS = {
    "邮箱", "电子邮箱", "邮箱地址", "邮件", "电子邮件", "邮件地址", "电邮", "邮箱号",
    "email", "emailaddress", "mail", "mailaddress", "e-mail", "electronicmail",
}
USERNAME_HEADERS = {
    "github", "githubusername", "githubname", "githubaccount", "githubid", "github用户名",
    "github账号", "github账户", "githublogin", "ghusername",
    "用户名", "账号", "账户", "昵称账号", "username", "login", "user", "handle",
}
NAME_HEADERS = {
    "姓名", "名字", "真实姓名", "昵称", "备注姓名", "联系人",
    "name", "fullname", "realname", "nickname", "displayname",
}
EMAIL_HINTS = ("邮箱", "email", "mail", "邮件", "电邮")
USERNAME_HINTS = ("github", "username", "login", "用户名", "账号", "账户", "gh")
NAME_HINTS = ("姓名", "名字", "name", "昵称", "nickname")

COLUMN_ROLES: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "email": (EMAIL_HEADERS, EMAIL_HINTS),
    "username": (USERNAME_HEADERS, USERNAME_HINTS),
    "name": (NAME_HEADERS, NAME_HINTS),
}
ROLE_ALIASES = {
    "email": "email", "mail": "email", "邮箱": "email", "电子邮箱": "email",
    "username": "username", "user": "username", "login": "username", "gh": "username",
    "用户名": "username", "github": "username", "github用户名": "username", "账号": "username",
    "name": "name", "姓名": "name", "名字": "name",
}
SKIP_TOKENS = {"-", "_", "skip", "忽略", "无", "x", "×"}


def _exact_role_keys(role: str) -> set[str]:
    keys, _ = COLUMN_ROLES[role]
    return {norm_key(x) for x in keys}


def match_role(header_cell: str, role: str) -> bool:
    """判断某个表头单元格是否属于某个角色。先精确匹配, 再退到包含匹配。"""
    key = norm_key(header_cell)
    if not key:
        return False
    # 别的角色的精确名不能被抢走(比如 "姓名" 同时含 "名" 和 "name")
    for other in COLUMN_ROLES:
        if other != role and key in _exact_role_keys(other):
            return False
    if key in _exact_role_keys(role):
        return True
    _, hints = COLUMN_ROLES[role]
    return any(norm_key(hint) in key for hint in hints)


def detect_columns(header: Sequence[str]) -> dict[str, int | None]:
    """从表头行认出 邮箱/用户名/姓名 三列各在哪。认不出就是 None。"""
    return {
        role: next((idx for idx, cell in enumerate(header) if match_role(str(cell), role)), None)
        for role in COLUMN_ROLES
    }


def locate_header(rows: Sequence[Sequence[str]],
                  max_scan: int = 5) -> tuple[int, dict[str, int | None]]:
    """在前几行里找最像表头的一行。返回 (行下标, 列映射); 找不到返回 (-1, 空映射)。"""
    blank = {"email": None, "username": None, "name": None}
    best_index, best_cols, best_score = -1, blank, 0
    for index, row in enumerate(rows[:max_scan]):
        cols = detect_columns([str(c) for c in row])
        score = sum(1 for value in cols.values() if value is not None)
        if score > best_score:
            best_index, best_cols, best_score = index, cols, score
    return (best_index, best_cols) if best_score > 0 else (-1, blank)


def parse_cols_spec(spec: str, table_width: int) -> dict[str, int]:
    """解析 --cols, 按位置(从 1 开始)规定第几列是什么。

    "username,email" = 第1列用户名、第2列邮箱; 不想用的列写 "-" 占位。
    """
    out: dict[str, int] = {}
    for position, token in enumerate(str(spec or "").split(","), start=1):
        # 跳过占位符要先看原文: norm_key("-") 会把连字符整条去掉变成空串
        if not token.strip() or token.strip() in SKIP_TOKENS or norm_key(token) in SKIP_TOKENS:
            continue
        role = ROLE_ALIASES.get(norm_key(token))
        if role is None:
            die(f'--cols 第 {position} 项 "{token.strip()}" 看不懂。\n'
                f'每项只能填 username / email / name (或中文 用户名 / 邮箱 / 姓名), '
                f'不想用的列写 "-" 占位。\n例如: --cols "name,email,username"')
        out[role] = position - 1
    if table_width:
        for role, index in sorted(out.items(), key=lambda kv: kv[1]):
            if index >= table_width:
                die(f'--cols 里 {role} 指的是第 {index + 1} 列, 但这张表只有 {table_width} 列。\n'
                    f'(位置从 1 开始数, "username,email" = 第1列用户名、第2列邮箱)')
    return out


# ===========================================================================
# 4. 记录构造: 清洗 + 归类
# ===========================================================================

@dataclass
class Record:
    row: int
    email: str = ""
    login: str = ""
    name: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ident(self) -> str:
        """唯一标识, 用来去重和续跑。"""
        return (self.email or self.login).lower()

    @property
    def label(self) -> str:
        """报告里显示谁 —— 优先姓名(需求要求按姓名汇报), 其次是用户名/邮箱。"""
        return self.name or self.login or self.email


def clean_email(value: str) -> tuple[str, str]:
    """返回 (邮箱, 备注)。一格写了好几个邮箱时只取第一个。"""
    text = (value or "").strip()
    if not text:
        return "", ""
    text = re.sub(r"^mailto:", "", text.replace("\u3000", " "), flags=re.I)
    parts = [p.strip().strip("<>").strip() for p in re.split(r"[,;、/|\s]+", text) if p.strip()]
    valid = [p for p in parts if EMAIL_RE.match(p)]
    if not valid:
        return "", ""
    note = f"单元格里有 {len(valid)} 个邮箱, 只取了第一个" if len(valid) > 1 else ""
    return valid[0].lower(), note


def clean_login(value: str) -> str:
    """把 https://github.com/x、github.com/x、@x 都归一成 x。"""
    text = (value or "").strip()
    if not text:
        return ""
    text = GITHUB_URL_RE.sub("", text)
    text = text.strip().strip("@").strip("/").strip()
    return text.split("/")[0].split("?")[0].strip()


def classify_cell(value: str) -> tuple[str, str]:
    """一个单元格判成 (邮箱, 用户名), 最多填一个 —— 靠内容, 不靠列名。

    注意: 这里不返回备注。要拿「一格多邮箱」这类提醒, 用 email_note_of()。
    """
    text = (value or "").strip()
    if not text:
        return "", ""
    email, _ = clean_email(text)
    if email:
        return email, ""
    login = clean_login(text)
    if not login:
        return "", ""
    if EMAIL_RE.match(login):
        return login.lower(), ""
    return "", login


def email_note_of(value: str) -> str:
    """单独取 clean_email 的备注(比如「一格里有 2 个邮箱, 只取第一个」)。"""
    return clean_email(value)[1]


def build_records(rows: Sequence[Sequence[str]], cols: dict[str, int | None]) -> list[Record]:
    """按列映射把数据行变成 Record。

    各列按内容归类, 但**各自优先信本职列**: 邮箱列里的垃圾值(恰好长得像用户名)
    不能盖掉用户名列里的真值。
    """
    def cell(row: Sequence[str], index: int | None) -> str:
        if index is None or index >= len(row):
            return ""
        return str(row[index]).strip()

    records: list[Record] = []
    for offset, row in enumerate(rows):
        email_raw = cell(row, cols.get("email"))
        login_raw = cell(row, cols.get("username"))
        name = cell(row, cols.get("name"))

        # classify_cell 返回 (邮箱, 用户名) —— 注意别把两个位置绑错名字
        email_from_email_col, _ = classify_cell(email_raw)
        email_from_login_col, login_from_login_col = classify_cell(login_raw)
        login_from_email_col = classify_cell(email_raw)[1]
        email_note = email_note_of(email_raw)

        email = email_from_email_col or (email_from_login_col if not login_from_login_col else "")
        login = login_from_login_col or (login_from_email_col if not email_from_email_col else "")

        notes: list[str] = []
        if email_note:
            notes.append(email_note)
        if email_raw and not email_from_email_col and login_from_email_col:
            notes.append(f"邮箱列里写的不是邮箱, 当用户名用了: {clip(email_raw, 40)}")
        if login_raw and not login_from_login_col and not email_from_login_col:
            notes.append(f"用户名列内容看不懂, 已忽略: {clip(login_raw, 40)}")
        if login and not LOGIN_RE.match(login):
            notes.append(f"用户名格式可疑(GitHub 用户名只允许字母数字和单个连字符): {clip(login, 40)}")

        if not email and not login and not name:
            continue
        records.append(Record(row=offset + 1, email=email, login=login, name=name, notes=notes))
    return records


# ===========================================================================
# 5. 输入来源解析 (表格文件 + 可选的命令行补充)
# ===========================================================================

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


def _blank_cols() -> dict[str, int | None]:
    return {"email": None, "username": None, "name": None}


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
        cols = ask_columns(rows, width)

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
        data_start, data_end = ask_data_rows(rows)

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


# ===========================================================================
# 6. GitHub API 客户端
# ===========================================================================

class ApiError(Exception):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


def describe_error(status: int, payload: Any) -> str:
    """把 GitHub 的错误响应压成一句人话。"""
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts: list[str] = []
            for item in errors:
                if isinstance(item, dict):
                    parts.append(str(item.get("message") or item.get("field") or item))
                else:
                    parts.append(str(item))
            joined = "; ".join(parts)
            message = f"{message} ({joined})" if message else joined
        if message:
            return message
    return f"HTTP {status}"


class GitHub:
    """GitHub REST 客户端。每个响应都是 (status, payload), 4xx 也照常返回。"""

    def __init__(self, token: str, api_url: str = DEFAULT_API, timeout: int = 30):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _once(self, method: str, path: str, body: Any = None) -> tuple[int, Any, dict]:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                payload = json.loads(raw) if raw.strip() else {}
                return response.status, payload, dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                payload = {"message": raw[:400]}
            return exc.code, payload, dict(exc.headers or {})
        except urllib.error.URLError as exc:
            raise ApiError(0, f"网络错误: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(0, "请求超时") from exc

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """带限流/网络退避的请求。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                status, payload, headers = self._once(method, path, body)
            except ApiError:
                if attempt > MAX_RETRIES:
                    raise
                time.sleep(min(60.0, 2.0 ** attempt))
                continue

            if status < 400 or status not in (403, 429, 500, 502, 503, 504):
                return status, payload

            message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            lowered = message.lower()
            wait: float | None = None

            retry_after = headers.get("Retry-After")
            if retry_after and str(retry_after).isdigit():
                wait = float(retry_after)

            if status == 403 and "secondary rate limit" in lowered:
                wait = wait or min(120.0, 60.0 * attempt)
                log(f"  ! 触发二级限流, 等 {wait:.0f}s 再试 ({attempt}/{MAX_RETRIES})")
            elif status == 403 and headers.get("X-RateLimit-Remaining") == "0":
                reset = headers.get("X-RateLimit-Reset")
                wait = max(wait or 0.0, (float(reset) - time.time() + 2) if reset else 60.0)
                wait = max(wait, 1.0)
                log(f"  ! 主限流已用尽, 等 {wait:.0f}s 再试 ({attempt}/{MAX_RETRIES})")
            elif status in (500, 502, 503, 504):
                wait = wait or min(30.0, 3.0 * attempt)
            elif status == 429:
                wait = wait or 30.0

            if wait is None or attempt > MAX_RETRIES:
                return status, payload
            time.sleep(min(wait, 900.0))

    def get_paged(self, path: str) -> list[Any]:
        items: list[Any] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            status, payload = self.request(
                "GET", f"{path}{separator}per_page={DEFAULT_PAGE}&page={page}")
            if status >= 400:
                raise ApiError(status, describe_error(status, payload), payload)
            if not isinstance(payload, list) or not payload:
                break
            items.extend(payload)
            if len(payload) < DEFAULT_PAGE:
                break
            page += 1
        return items

    # ---- 具体接口 ----

    def user_id(self, login: str, cache: dict[str, int | None]) -> int | None:
        """登录名 -> 数字 id。取不到(拼错/不存在)返回 None。"""
        key = login.lower()
        if key in cache:
            return cache[key]
        status, payload = self.request("GET", f"/users/{urllib.parse.quote(login)}")
        value = None
        if status < 400 and isinstance(payload, dict) and payload.get("id"):
            value = int(payload["id"])
        cache[key] = value
        return value

    def org_members(self, org: str) -> set[str]:
        return {
            str(item.get("login") or "").lower()
            for item in self.get_paged(f"/orgs/{org}/members")
            if item.get("login")
        }

    def user_exists(self, login: str) -> bool:
        """这个 GitHub 用户名到底存不存在。不存在 -> 后面什么邀请都做不了。"""
        status, _ = self.request("GET", f"/users/{urllib.parse.quote(login)}")
        return status < 400

    def org_pending(self, org: str) -> tuple[set[str], set[str]]:
        """返回 (待处理邀请的邮箱集合, 待处理邀请的用户名集合)。

        注意: 按邮箱发出的邀请在 API 里 login 恒为 null, 所以两个集合都要看。
        """
        emails: set[str] = set()
        logins: set[str] = set()
        for item in self.get_paged(f"/orgs/{org}/invitations"):
            if item.get("email"):
                emails.add(str(item["email"]).lower())
            if item.get("login"):
                logins.add(str(item["login"]).lower())
        return emails, logins

    def failed_invitations(self, org: str) -> list[dict]:
        return [item for item in self.get_paged(f"/orgs/{org}/failed_invitations")
                if isinstance(item, dict)]

    def org_teams(self, org: str) -> list[dict]:
        return [item for item in self.get_paged(f"/orgs/{org}/teams") if isinstance(item, dict)]

    def team_members(self, org: str, slug: str) -> set[str]:
        return {
            str(item.get("login") or "").lower()
            for item in self.get_paged(f"/orgs/{org}/teams/{urllib.parse.quote(slug)}/members")
            if item.get("login")
        }

    def search_login_by_email(self, email: str) -> str:
        """用搜索接口把邮箱反查成登录名, 命中就赚(能走 invitee_id, 还能精确认成员)。

        只对「在 GitHub 上公开了邮箱」的账号有效, 所以命中率有限。
        """
        query = urllib.parse.quote(f"{email} in:email", safe="")
        status, payload = self.request("GET", f"/search/users?q={query}")
        if status >= 400 or not isinstance(payload, dict):
            return ""
        for item in (payload.get("items") or [])[:3]:
            login = str(item.get("login") or "")
            if not login:
                continue
            check, profile = self.request("GET", f"/users/{urllib.parse.quote(login)}")
            if check < 400 and isinstance(profile, dict) \
                    and str(profile.get("email") or "").strip().lower() == email.lower():
                return login
        return ""


# ===========================================================================
# 7. token 解析
# ===========================================================================

TOKEN_FILE_NAMES = ("token.txt", ".env")
TOKEN_KEYS = ("GITHUB_TOKEN", "GH_TOKEN", "TOKEN")


def _read_token_file(path: Path) -> str:
    """支持 KEY=VALUE, 也支持整份文件只写一个 token。"""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            if key.strip().upper() not in TOKEN_KEYS:
                continue
        else:
            value = line
        value = value.strip().strip('"').strip("'").strip()
        if value:
            return value
    return ""


def resolve_token(args: argparse.Namespace, required: bool) -> str:
    """优先级: --token > 环境变量 > 当前目录/脚本目录的 token.txt。只打印来源, 不打印内容。"""
    token = (getattr(args, "token", "") or "").strip()
    source = "--token 参数"
    if not token:
        token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
        source = "环境变量"
    if not token:
        for directory in (Path.cwd(), Path(__file__).resolve().parent):
            for name in TOKEN_FILE_NAMES:
                candidate = directory / name
                if candidate.is_file():
                    token = _read_token_file(candidate)
                    if token:
                        source = f"配置文件 {candidate.name}"
                        break
            if token:
                break
    if not token and required:
        die("没找到 token。三选一:\n"
            "  1) 在脚本目录建 token.txt, 写一行 GITHUB_TOKEN=你的token  (推荐, 不会被提交)\n"
            "  2) 设环境变量: PowerShell  $env:GITHUB_TOKEN = \"你的token\"\n"
            "  3) 临时用 --token 传(会留在命令历史里, 不推荐)\n"
            "想先看表格解析结果, 加 --no-preflight 就能免 token 跑 dry-run。")
    if token:
        log(f"token 来源: {source}")
    return token


# ===========================================================================
# 8. 结果模型与状态码
# ===========================================================================

# 组织邀请这一档
S_PLANNED = "planned"
S_INVITED = "invited"
S_ALREADY_MEMBER = "already_member"
S_ALREADY_INVITED = "already_invited"
S_NO_CONTACT = "skipped_no_contact"
S_DUPLICATE = "skipped_duplicate"
S_RESUMED = "skipped_resumed"
S_UNPROCESSED = "skipped_unprocessed"
S_FAILED = "failed"

ORG_OK = {S_INVITED, S_ALREADY_MEMBER}
ORG_SKIP = {S_ALREADY_INVITED, S_NO_CONTACT, S_DUPLICATE, S_RESUMED, S_PLANNED, S_UNPROCESSED}

# Team 这一档
#
# 注意: 组织档和 team 档的状态值是两套独立的字符串, 不能重名。
# 之前 T_FAILED 也叫 "failed", 和 S_FAILED 撞成了同一个字典键, 于是
# REMEDY 里 team 那条把组织那条悄悄覆盖掉了 —— 组织邀请失败的行会拿到
# 一段讲 team 的建议。所有状态值必须两两不同。
T_ALREADY = "already_in_team"
T_ADDED = "team_added"
T_PENDING_ACCEPT = "pending_org_accept"
T_NEEDS_USERNAME = "needs_username"
T_USERNAME_MISSING = "username_missing"
T_FAILED = "team_failed"
T_NA = "not_requested"      # 本次没要求做 team

ORG_LABEL = {
    S_PLANNED: "计划邀请(dry-run)",
    S_INVITED: "组织邀请已发出",
    S_ALREADY_MEMBER: "已是组织成员",
    S_ALREADY_INVITED: "已有待处理邀请",
    S_NO_CONTACT: "跳过(没有可用的邮箱/用户名)",
    S_DUPLICATE: "跳过(表里重复)",
    S_RESUMED: "跳过(上次已成功)",
    S_UNPROCESSED: "跳过(--limit 截断)",
    S_FAILED: "邀请失败",
}
TEAM_LABEL = {
    T_ALREADY: "已在 team",
    T_ADDED: "已加入 team",
    T_PENDING_ACCEPT: "待对方接受组织邀请",
    T_NEEDS_USERNAME: "缺用户名, 加不了 team",
    T_USERNAME_MISSING: "用户名不存在",
    T_FAILED: "加入 team 失败",
    T_NA: "—",
}

# 状态 -> 处理措施。需求明确要求给出「发送失败的原因和处理措施」。
REMEDY = {
    S_PLANNED: "这是 dry-run 的计划。确认无误后加 --execute 真正发送。",
    S_INVITED: "无需处理。7 天内没接受就过期, 到期前催一下。",
    S_ALREADY_MEMBER: "无需处理, 对方已经在组织里, 接口也不会给他发邮件。",
    S_ALREADY_INVITED: "无需重发, 等对方接受即可; 用 --status 可以看到这条邀请还挂着没有。",
    S_NO_CONTACT: "让对方补一个 GitHub 用户名或邮箱, 再重跑这一条。",
    S_DUPLICATE: "无需处理, 表里同一个人只发一次。",
    S_RESUMED: "无需处理。想强制重发加 --no-resume(会重复发送, 慎用)。",
    S_UNPROCESSED: "本次被 --limit 截断, 不是失败。不带 --limit 再跑一次就会处理。",
    S_FAILED: "照上面的原因修好表格或权限后重跑; 已是成员的人不会收到第二封邮件。",
    T_ALREADY: "无需处理, 已在 team 里, 脚本不会动他的 team 角色。",
    T_ADDED: "无需处理。",
    T_PENDING_ACCEPT: "等对方接受组织邀请(7 天内), 再跑一次同样的命令, 会自动把他补进 team。",
    T_NEEDS_USERNAME: "去 GitHub 搜到他的用户名, 用名单文件或 --cols 指定用户名后重跑。",
    T_USERNAME_MISSING: "GitHub 上查不到这个用户名, 对方接受不了邀请。核对拼写后改表格重跑"
                        "(也可能对方把资料设为私密, 那就让他确认用户名)。",
    T_FAILED: "照上面的原因查: 对方是不是组织成员、用户名拼写对不对、token 有没有该 team 的写权限。",
}


@dataclass
class Outcome:
    record: Record
    org_status: str = ""
    org_detail: str = ""
    team_status: str = T_NA
    team_detail: str = ""
    executed: bool = False

    @property
    def has_failure(self) -> bool:
        return (self.org_status == S_FAILED or self.team_status == T_FAILED
                or self.team_status == T_USERNAME_MISSING)

    @property
    def committed(self) -> bool:
        """这一条是否已实打实落定(成功或已被跳过), 用来写进续跑历史。"""
        return self.org_status in ORG_OK or self.org_status in ORG_SKIP


def parse_conflict_message(message: str) -> str:
    """把 422 的原文归成 already_member / already_invited / 其它。"""
    lowered = message.lower()
    if "already a part of this organization" in lowered or "already a member" in lowered:
        return S_ALREADY_MEMBER
    if "invitation" in lowered and "already" in lowered:
        return S_ALREADY_INVITED
    return ""


# ===========================================================================
# 9. 打印: 逐行表格 + 汇总 + 待处理清单
# ===========================================================================

def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]],
                max_widths: Sequence[int] | None = None) -> None:
    if not rows:
        return
    widths: list[int] = []
    for index, title in enumerate(headers):
        widest = display_width(title)
        for row in rows:
            widest = max(widest, display_width(str(row[index])))
        if max_widths and index < len(max_widths) and max_widths[index] > 0:
            widest = min(widest, max_widths[index])
        widths.append(widest)

    def render(cells: Sequence[str]) -> str:
        parts = [pad(clip(str(cell), widths[i]), widths[i]) for i, cell in enumerate(cells)]
        return "  ".join(part.rstrip() for part in parts)

    log(render(headers))
    log("  ".join("-" * width for width in widths))
    for row in rows:
        log(render(row))


def count_by(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def summarize_org(counts: dict[str, int], mode: str) -> str:
    """组织邀请这一档的汇总。

    dry-run 时说「计划邀请」, 不要把它跟真发出去的混成一句「成功」。
    """
    from_previous = counts.get(S_RESUMED, 0)
    if mode == "execute":
        head = (f"已发出邀请 {counts.get(S_INVITED, 0)} 人 | "
                f"已是成员 {counts.get(S_ALREADY_MEMBER, 0)} 人")
    else:
        head = (f"计划邀请 {counts.get(S_PLANNED, 0)} 人 | "
                f"已是成员 {counts.get(S_ALREADY_MEMBER, 0)} 人")
    if from_previous:
        head += f" | 跳过(上次已成功) {from_previous} 人"
    return (f"{head} | 已有待处理邀请 {counts.get(S_ALREADY_INVITED, 0)} 人 | "
            f"失败 {counts.get(S_FAILED, 0)} 人 | "
            f"跳过 {sum(counts.get(key, 0) for key in ORG_SKIP) - from_previous} 人")


def summarize_team(counts: dict[str, int], mode: str) -> str:
    verb = "已加入" if mode == "execute" else "计划加入"
    parts = [f"{verb} {counts.get(T_ADDED, 0)} 人",
             f"本来就在 {counts.get(T_ALREADY, 0)} 人"]
    if counts.get(T_PENDING_ACCEPT, 0):
        parts.append(f"待对方接受后重跑 {counts[T_PENDING_ACCEPT]} 人")
    if counts.get(T_USERNAME_MISSING, 0):
        parts.append(f"用户名不存在 {counts[T_USERNAME_MISSING]} 人")
    if counts.get(T_NEEDS_USERNAME, 0):
        parts.append(f"缺用户名 {counts[T_NEEDS_USERNAME]} 人")
    parts.append(f"失败 {counts.get(T_FAILED, 0)} 人")
    return " | ".join(parts)


def result_row(out: Outcome, with_team: bool) -> list[str]:
    row = [
        out.record.label or "—",
        out.record.email or "—",
        out.record.login or "—",
        ORG_LABEL.get(out.org_status, out.org_status),
    ]
    if with_team:
        team_text = TEAM_LABEL.get(out.team_status, out.team_status)
        if out.team_status == T_USERNAME_MISSING:
            team_text = "用户名不存在"
        elif out.team_status == T_FAILED and out.team_detail:
            team_text = f"加入失败: {clip(out.team_detail, 12)}"
        row.append(team_text)
    note = out.org_detail
    if out.team_status in (T_FAILED, T_USERNAME_MISSING) and out.team_detail:
        note = f"{note}; team: {out.team_detail}" if note else f"team: {out.team_detail}"
    row.append(note or "—")
    return row


def print_results(outcomes: Sequence[Outcome], with_team: bool) -> None:
    log("")
    log(f"== 逐行结果 ({len(outcomes)} 人) ==")
    headers = ["姓名", "邮箱", "用户名", "组织邀请"]
    if with_team:
        headers.append("Team")
    headers.append("说明")
    rows = [result_row(out, with_team) for out in outcomes]
    widths = [14, 24, 18, 20] + ([22] if with_team else []) + [38]
    print_table(headers, rows, widths)


def print_summary(outcomes: Sequence[Outcome], with_team: bool, args: argparse.Namespace,
                  elapsed: float, mode: str) -> None:
    org_counts = count_by(out.org_status for out in outcomes)
    log("")
    log("== 汇总 ==")
    log(f"组织邀请: {summarize_org(org_counts, mode)}")
    for status in (S_INVITED, S_PLANNED, S_ALREADY_MEMBER, S_ALREADY_INVITED, S_FAILED,
                   S_DUPLICATE, S_NO_CONTACT, S_RESUMED, S_UNPROCESSED):
        if org_counts.get(status):
            log(f"  - {ORG_LABEL[status]}: {org_counts[status]} 人")
    if with_team:
        team_counts = count_by(out.team_status for out in outcomes if out.team_status != T_NA)
        log(f"Team 加入: {summarize_team(team_counts, mode)}")
        log(f"  目标 team: {args.team}")
    log(f"耗时 {elapsed:.1f} 秒")


def print_issues(outcomes: Sequence[Outcome]) -> None:
    """把失败按「原因」归类, 每条给出处理措施 —— 需求里的硬性要求。"""
    groups: dict[str, list[Outcome]] = {}
    for out in outcomes:
        problem = ""
        if out.org_status == S_FAILED:
            problem = out.org_detail or "接口拒绝, 未给出原因"
        elif out.team_status == T_FAILED:
            problem = f"team: {out.team_detail or '接口拒绝, 未给出原因'}"
        if problem:
            groups.setdefault(problem, []).append(out)
    if not groups:
        return
    log("")
    log("== 失败明细(按原因归类) ==")
    for problem, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        names = "、".join(out.record.label for out in items)
        log(f"【{problem}】({len(items)} 人)")
        log(f"    涉及: {clip(names, 100)}")
        remedy = REMEDY[T_FAILED] if problem.startswith("team:") else REMEDY[S_FAILED]
        log(f"    处理: {remedy}")


def print_retry_list(outcomes: Sequence[Outcome], rerun_hint: str) -> None:
    """把「还需要人动手」的情况按性质分开列。"""
    pending_accept = [out for out in outcomes if out.team_status == T_PENDING_ACCEPT]
    missing = [out for out in outcomes if out.team_status == T_USERNAME_MISSING]
    needs_name = [out for out in outcomes if out.team_status == T_NEEDS_USERNAME]

    if pending_accept:
        names = "、".join(
            f"{out.record.label}({out.record.login or out.record.email})"
            for out in pending_accept[:20])
        log("")
        log(f"== 待重跑名单: 对方接受组织邀请后, 再跑一次即自动进 team "
            f"({len(pending_accept)} 人) ==")
        log(f"    {clip(names, 140)}" + (" …" if len(pending_accept) > 20 else ""))
        log("    如果对方早就说已经进组织了, 多半是表里的用户名填错 —— 让他确认一下。")
        log(f"    命令: {rerun_hint}")

    if missing:
        names = "、".join(
            f"{out.record.label}({out.record.login})" for out in missing[:20])
        log("")
        log(f"== 用户名不存在, 什么也做不了 ({len(missing)} 人) ==")
        log(f"    {clip(names, 140)}" + (" …" if len(missing) > 20 else ""))
        log("    这几行填的用户名在 GitHub 上查不到, 邀请永远发不成功。")
        log("    找本人核对拼写(常见是相邻字母打反), 改表格后重跑。")

    if needs_name:
        names = "、".join(f"{out.record.label}({out.record.email or '—'})"
                          for out in needs_name[:20])
        log("")
        log(f"== 待补用户名: 只有邮箱, 加不了 team ({len(needs_name)} 人) ==")
        log(f"    {clip(names, 140)}" + (" …" if len(needs_name) > 20 else ""))


def print_status_report(gh: GitHub, org: str) -> None:
    """--status: 只读地看一眼组织当前状态, 不需要表格。"""
    log(f"== 组织 {org} 的当前状态 ==")
    try:
        members = gh.org_members(org)
        pending_emails, pending_logins = gh.org_pending(org)
        failed = gh.failed_invitations(org)
    except ApiError as exc:
        die(f"读组织数据失败: {exc.message}\n"
            "检查: 组织名对不对、token 有没有过期、有没有该组织的 owner 权限。", 3)

    log(f"成员 {len(members)} 人 | 待处理邀请 {len(pending_emails) + len(pending_logins)} 个 | "
        f"失败邀请 {len(failed)} 个")

    if failed:
        log("")
        log("-- 失败的邀请(含历史遗留) --")
        rows = []
        for item in failed:
            who = str(item.get("email") or item.get("login") or "(未知)")
            reason = str(item.get("failed_reason") or "未给出原因")
            if "expired" in reason.lower():
                remedy = "组织邀请 7 天过期, 需要重新发一次。"
            else:
                remedy = "让对方把该邮箱加到 GitHub 账号并验证, 或改用用户名邀请。"
            rows.append([clip(who, 30), clip(reason, 44), clip(remedy, 40)])
        print_table(["对象", "失败原因", "处理"], rows, [30, 44, 40])

    if pending_emails:
        log("")
        log(f"-- 待处理邀请 {len(pending_emails)} 个(等对方接受, 7 天有效) --")
        for email in sorted(pending_emails)[:20]:
            log(f"    {email}")
        log("    提示: 按邮箱发的邀请在 API 里没有用户名(login 恒为 null), 只能看邮箱。")


def print_preflight(members: set[str], pending_emails: set[str], pending_logins: set[str],
                    team_members: set[str] | None, team_name: str) -> None:
    log("")
    log("== 预检 ==")
    log(f"组织已有成员 {len(members)} 人")
    log(f"已有待处理邀请 {len(pending_emails | pending_logins)} 个")
    if team_members is not None:
        log(f"team「{team_name}」现有成员 {len(team_members)} 人")


# ===========================================================================
# 10. 续跑历史 (读历史 results-*.jsonl)
# ===========================================================================

def load_history(out_dir: Path) -> dict[str, dict]:
    """记住哪些人上次已经成功处理过。

    用 utf-8-sig 读: 结果文件带 BOM 时, 用 utf-8 读会让第一行 JSON 解析失败被静默丢掉。
    """
    history: dict[str, dict] = {}
    for path in sorted(out_dir.glob("results-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key_name in ("email", "login"):
                key = str(item.get(key_name) or "").strip().lower()
                if key:
                    history[key] = item  # 越晚的记录越新, 覆盖旧的
    return history


# ===========================================================================
# 11. 主流程
# ===========================================================================

def normalize_argv(argv: Sequence[str]) -> list[str]:
    """把 `--cols -,email,username` 这种写法接住。

    argparse 碰到以 '-' 开头的值会以为那是另一个选项, 于是报
    "expected one argument"。但「跳过第一列」的写法天生就长这样, 所以先把
    `--cols 值` 合并成 `--cols=值` —— 两种写法完全等价。

    (这跟输入是 csv 还是 xlsx 无关, 只要用得上 --cols 就需要它。)
    """
    out: list[str] = []
    index = 0
    items = list(argv)
    while index < len(items):
        item = items[index]
        if item == "--cols" and index + 1 < len(items):
            out.append(f"--cols={items[index + 1]}")
            index += 2
            continue
        out.append(item)
        index += 1
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="批量邀请人加入 GitHub 组织, 可选顺手加入指定 Team(默认 dry-run, 不发任何东西)",
        epilog=(
            "输入: 一个表格文件(.xlsx / .csv)\n"
            "  python github_inviter.py --org my-org \"收集表.xlsx\"\n"
            "\n"
            "表里至少有「邮箱」或「GitHub用户名」一列; 列顺序随便, 靠表头名字认:\n"
            "  姓名 / 邮箱 / GitHub用户名\n"
            "示例文件见 docs/examples/\n"
            "\n"
            "表头名字认不出来时, 脚本会列出前几行、问一句数据从第几行开始;\n"
            "也可以用参数直接说清楚:\n"
            "  --cols \"-,username,email,name\"  按位置指定列(位置从 1 开始, 不用的写 -)\n"
            "  --skip-rows 1                    跳过第 1 行(比如认不出的表头)\n"
            "  --no-header                      表里没有表头, 第一行就是数据\n"
        ),
    )
    parser.add_argument("targets", nargs="*", metavar="表格",
                        help="表格文件路径(.xlsx / .csv); 可选再跟一个用户名/邮箱临时补一个")
    parser.add_argument("--org", required=True, help="组织名(slug), 例如 my-org")

    team = parser.add_argument_group("team(可选)")
    team.add_argument("--team", default="", help='team 名称或 slug, 例如 "NEUP 2026"')
    team.add_argument("--team-role", default="member", choices=["member", "maintainer"],
                      help="新增 team 成员的角色, 默认 member(不会改动已有成员的角色)")

    table = parser.add_argument_group("表格解析")
    table.add_argument("--cols", default="",
                       help='按位置指定列, 例如 "name,email,username"; 不想用的列写 "-"')
    table.add_argument("--email-col", default="", help="手动指定邮箱列的列名或列号")
    table.add_argument("--username-col", default="", help="手动指定用户名列的列名或列号")
    table.add_argument("--sheet", default="", help="xlsx 的工作表名, 默认第一个")
    table.add_argument("--no-header", action="store_true",
                       help="表里没有表头(第一行就是数据): 不做表头识别, 按第1列用户名、第2列邮箱")
    table.add_argument("--skip-rows", type=int, default=0, metavar="N",
                       help="跳过表格开头的 N 行再解析(表头认不出时用 --skip-rows 1 跳掉它)")

    run = parser.add_argument_group("执行")
    run.add_argument("--execute", action="store_true",
                     help="真正发送邀请。不加这个参数就是 dry-run, 不会发出任何邀请")
    run.add_argument("--limit", type=int, default=0,
                     help="本次最多处理多少条(0=不限), 便于小批量试跑")
    run.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                     help=f"每条之间的间隔秒数, 默认 {DEFAULT_DELAY}(别调太小, 会触发限流)")
    run.add_argument("--role", default="direct_member",
                     choices=["direct_member", "admin", "billing_manager"],
                     help="组织邀请的角色, 默认 direct_member")
    run.add_argument("--prefer-email", action="store_true",
                     help="强制只按邮箱邀请(默认是: 有用户名就优先按用户名邀请)")
    run.add_argument("--resolve-emails", action="store_true",
                     help="先用搜索接口把邮箱反查成用户名(命中就能改走用户名邀请, 更可靠)")
    run.add_argument("--no-preflight", action="store_true",
                     help="跳过预检(也不用 token), 只看表格解析结果")
    run.add_argument("--no-resume", action="store_true",
                     help="忽略历史结果重新处理全部(会重复发送, 慎用)")
    run.add_argument("--status", action="store_true",
                     help="只读: 看组织当前成员/待处理邀请/失败邀请, 不需要表格, 不改任何东西")

    other = parser.add_argument_group("其它")
    other.add_argument("--out-dir", default="", help="结果输出目录, 默认脚本旁的 output/")
    other.add_argument("--token", default="", help="PAT; 默认读 token.txt 或环境变量")
    other.add_argument("--api-url", default=DEFAULT_API, help="GitHub API 地址")
    other.add_argument("--verbose", action="store_true", help="输出更多细节")
    raw = list(sys.argv[1:] if argv is None else argv)
    return parser.parse_args(normalize_argv(raw))


def select_team(gh: GitHub, org: str, wanted: str) -> dict:
    """按 slug 优先、其次按名称精确匹配。找不到就把现有 team 列出来。"""
    key = wanted.strip().lower()
    teams = gh.org_teams(org)
    if not teams:
        die(f"组织 {org} 里没读到任何 team —— token 权限不足? "
            f"(需要能读该组织的 team, 组织 owner 权限最稳)", 3)
    for item in teams:
        if str(item.get("slug") or "").lower() == key:
            return item
    for item in teams:
        if str(item.get("name") or "").strip().lower() == key:
            return item
    listing = "、".join(f"{t.get('name')}({t.get('slug')})" for t in teams)
    die(f"找不到 team: {wanted}\n现有 team: {listing}")


def deduplicate(records: Sequence[Record]) -> tuple[list[Record], list[Record], list[Outcome]]:
    """去重 + 排除没有联系方式的行。

    返回 (去重后的记录, 被丢掉的重复行, 没有联系方式的行的 Outcome)。
    重复行要单独返回: --limit 是「表里的前 N 行」, 判断某一行在不在前 N 行时
    必须把它算进去, 否则 --limit 会静默跳过更多人, 而不是老老实实只处理前 N 个。
    """
    valid: list[Record] = []
    duplicates: list[Record] = []
    excluded: list[Outcome] = []
    seen: dict[str, int] = {}
    for record in records:
        if not record.email and not record.login:
            detail = "既没有邮箱也没有用户名"
            if record.notes:
                detail += f"({record.notes[0]})"
            excluded.append(Outcome(record, S_NO_CONTACT, detail))
            continue
        key = record.ident
        if key in seen:
            duplicates.append(record)
            excluded.append(Outcome(record, S_DUPLICATE, f"与第 {seen[key]} 行是同一个人"))
            continue
        seen[key] = record.row
        valid.append(record)
    return valid, duplicates, excluded


def seen_in_history(record: Record, history: dict[str, dict]) -> bool:
    """这个人是不是在以前某次运行里已经处理成功过(续跑的依据)。"""
    if not history:
        return False
    return bool((record.email and record.email.lower() in history)
                or (record.login and record.login.lower() in history))


def resolve_emails_to_logins(gh: GitHub, records: Sequence[Record]) -> int:
    """把只有邮箱的行反查成用户名, 就地写回。返回命中数。"""
    targets = [r for r in records if r.email and not r.login]
    if not targets:
        log("反查邮箱: 没有需要反查的行(都已经有用户名了)。")
        return 0
    log("")
    log(f"反查邮箱: 尝试把 {len(targets)} 个邮箱换成 GitHub 用户名 ...")
    if len(targets) > 30:
        log(f"  注意: 搜索接口限速 30 次/分钟, {len(targets)} 条约需 {len(targets) * 2 // 60} 分钟。")
    found = 0
    for record in targets:
        login = gh.search_login_by_email(record.email)
        if login:
            record.login = login
            record.notes.append("由邮箱反查得到用户名")
            found += 1
            log(f"  {record.email} -> {login}")
        time.sleep(2)  # 搜索接口限速 30 次/分钟
    log(f"  反查到 {found} / {len(targets)} 个。"
        + ("" if found else "(多数人没在 GitHub 上公开邮箱, 反查不到是正常的)"))
    return found


def process_org_invites(gh: GitHub | None, args: argparse.Namespace, todo: Sequence[Record],
                        members: set[str], pending_emails: set[str], pending_logins: set[str],
                        history: dict[str, dict], id_cache: dict[str, int | None],
                        mode: str) -> list[Outcome]:
    """第 1 档: 组织邀请。

    mode: "execute" 真发; "plan" 预检过的 dry-run; "unknown" 没预检, 状态不明。
    """
    outcomes: list[Outcome] = []
    total = len(todo)
    for index, record in enumerate(todo, start=1):
        outcome = Outcome(record)
        key_email = record.email.lower()
        key_login = record.login.lower()

        if record.login and key_login in members:
            outcome.org_status, outcome.org_detail = S_ALREADY_MEMBER, "在组织成员列表里"
        elif record.email and key_email in pending_emails:
            outcome.org_status, outcome.org_detail = S_ALREADY_INVITED, "该邮箱已有待处理邀请"
        elif record.login and key_login in pending_logins:
            outcome.org_status, outcome.org_detail = S_ALREADY_INVITED, "该用户名已有待处理邀请"
        elif seen_in_history(record, history):
            outcome.org_status = S_RESUMED
            outcome.org_detail = "上次已成功处理过"
        elif mode == "unknown":
            outcome.org_status = S_PLANNED
            outcome.org_detail = "没有预检, 不知道对方现在在不在组织里"
        elif mode == "plan":
            outcome.org_status = S_PLANNED
            outcome.org_detail = "dry-run 计划邀请"
        else:
            assert gh is not None
            body: dict[str, Any] = {"role": args.role}
            used_login = ""
            if record.login and not args.prefer_email:
                invitee = gh.user_id(record.login, id_cache)
                if invitee:
                    body["invitee_id"] = invitee
                    used_login = record.login
                elif record.email:
                    body["email"] = record.email
            if "invitee_id" not in body and "email" not in body:
                if record.email:
                    body["email"] = record.email
                else:
                    outcome.org_status = S_FAILED
                    outcome.org_detail = f"GitHub 上找不到用户名 {record.login}, 这一行又没有邮箱"
                    outcomes.append(outcome)
                    log(f"[{index}/{total}] ✗ {record.label} -> 找不到该用户名, 且没有邮箱")
                    continue

            try:
                status, payload = gh.request("POST", f"/orgs/{args.org}/invitations", body)
            except ApiError as exc:
                outcome.org_status, outcome.org_detail = S_FAILED, exc.message
                outcomes.append(outcome)
                log(f"[{index}/{total}] ✗ {record.label} -> {exc.message}")
                continue

            outcome.executed = True
            if 200 <= status < 300:
                outcome.org_status = S_INVITED
                if used_login:
                    outcome.org_detail = f"已按用户名邀请({used_login})"
                else:
                    outcome.org_detail = "已按邮箱邀请"
                    outcome.org_detail += "; ⚠ 该邮箱若不是对方已验证的邮箱, 对方点不动邀请"
            elif status == 422:
                message = describe_error(status, payload)
                verdict = parse_conflict_message(message)
                if verdict == S_ALREADY_MEMBER:
                    outcome.org_status, outcome.org_detail = S_ALREADY_MEMBER, "接口明确回复已是成员"
                elif verdict == S_ALREADY_INVITED:
                    outcome.org_status, outcome.org_detail = S_ALREADY_INVITED, "接口明确回复已有邀请"
                else:
                    outcome.org_status = S_FAILED
                    outcome.org_detail = f"422 校验失败: {clip(message, 70)}"
            elif status == 404:
                outcome.org_status = S_FAILED
                outcome.org_detail = "404: 组织名写错, 或 token 不是这个组织的 owner"
            elif status == 403:
                outcome.org_status = S_FAILED
                outcome.org_detail = ("403 权限不足: 细粒度 token 要 Members 写权限且组织已批准; "
                                      "经典 token 要 admin:org")
            else:
                outcome.org_status = S_FAILED
                outcome.org_detail = f"{status}: {clip(describe_error(status, payload), 70)}"

            mark = ("✓" if outcome.org_status in ORG_OK
                    else ("=" if outcome.org_status in ORG_SKIP else "✗"))
            log(f"[{index}/{total}] {mark} {record.label} -> {ORG_LABEL[outcome.org_status]}"
                + (f"  ({outcome.org_detail})" if outcome.org_status != S_INVITED else ""))

        outcomes.append(outcome)
        if mode == "execute" and args.delay > 0 and index < total:
            time.sleep(args.delay)
    return outcomes


def process_team(gh: GitHub | None, args: argparse.Namespace, outcomes: Sequence[Outcome],
                 members: set[str], current_team: set[str], team_slug: str,
                 mode: str, username_exists: dict[str, bool] | None = None) -> None:
    """第 2 档: 把已确认在组织里的人加进 team。

    顺序严格是「先查后写」: 已在 team 里的绝不重设角色(那会把 maintainer 降级)。
    """
    if username_exists is None:
        username_exists = {}

    for outcome in outcomes:
        record = outcome.record
        login_key = record.login.lower()

        if login_key and login_key in current_team:
            outcome.team_status, outcome.team_detail = T_ALREADY, "已在 team 里, 未改动其角色"
            continue
        if not record.login:
            outcome.team_status = T_NEEDS_USERNAME
            outcome.team_detail = "只有邮箱 —— team 接口不认邮箱, 需要 GitHub 用户名"
            continue

        if login_key not in members:
            # 快照里没有他, 两种可能:
            #  * 本次才刚给他发出邀请 -> 他确实还没接受, 现在 PUT 必然失败, 留到下次;
            #  * 这个用户名在 GitHub 上压根不存在 -> 他永远接受不了邀请, 必须明说,
            #    否则「等对方接受」是一句永远等不到的建议(实测就是这么被误导的)。
            if login_key not in username_exists:
                assert gh is not None
                username_exists[login_key] = gh.user_exists(record.login)
            if not username_exists[login_key]:
                outcome.team_status = T_USERNAME_MISSING
                outcome.team_detail = (f"GitHub 上查不到 {record.login} 这个用户名"
                                       f"(多半是拼错了; 也可能是对方把资料设为私密)")
                continue
            outcome.team_status = T_PENDING_ACCEPT
            outcome.team_detail = "还没成为组织成员(邀请待接受)"
            continue

        if mode != "execute":
            outcome.team_status = T_ADDED
            outcome.team_detail = "dry-run 计划加入"
            continue

        assert gh is not None
        try:
            status, payload = gh.request(
                "PUT",
                f"/orgs/{args.org}/teams/{urllib.parse.quote(team_slug)}"
                f"/memberships/{urllib.parse.quote(record.login)}",
                {"role": args.team_role},
            )
        except ApiError as exc:
            outcome.team_status, outcome.team_detail = T_FAILED, exc.message
            continue
        outcome.executed = True
        if 200 <= status < 300:
            outcome.team_status = T_ADDED
            outcome.team_detail = f"角色 {args.team_role}"
        elif status == 404:
            outcome.team_status = T_FAILED
            outcome.team_detail = "404: 用户名或 team 不存在, 或 token 无权改这个 team"
        else:
            outcome.team_status = T_FAILED
            outcome.team_detail = f"{status}: {clip(describe_error(status, payload), 60)}"


def write_outputs(out_dir: Path, stamp: str, outcomes: Sequence[Outcome],
                  execute: bool) -> tuple[Path, ...]:
    """落盘。

    execute=True  -> 书面结果: results-*.csv(给人看) + results-*.jsonl(续跑靠它)
    execute=False -> 计划: plan-*.csv(将要邀请谁、排除了谁、为什么)
    """
    if not execute:
        plan_path = out_dir / f"plan-{stamp}.csv"
        with plan_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["行号", "姓名", "邮箱", "用户名", "本次动作", "team动作", "说明"])
            for out in outcomes:
                writer.writerow([
                    out.record.row, out.record.label, out.record.email, out.record.login,
                    ORG_LABEL.get(out.org_status, out.org_status),
                    TEAM_LABEL.get(out.team_status, out.team_status),
                    out.org_detail or out.team_detail,
                ])
        return (plan_path,)

    csv_path = out_dir / f"results-{stamp}.csv"
    jsonl_path = out_dir / f"results-{stamp}.jsonl"

    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["行号", "姓名", "邮箱", "用户名", "组织邀请状态", "组织邀请说明",
                         "team状态", "team说明", "失败原因", "处理措施", "时间"])
        for out in outcomes:
            problem = ""
            if out.org_status == S_FAILED:
                problem = out.org_detail
            elif out.team_status in (T_FAILED, T_USERNAME_MISSING):
                problem = f"team: {out.team_detail}"
            # 处理措施要跟着真正的失败原因走。组织这头失败了, 就必须给组织这头的
            # 措施, 别被 team 状态抢走 —— 否则建议和失败原因对不上, 反而误导人。
            if out.org_status == S_FAILED:
                remedy = REMEDY[S_FAILED]
            elif out.team_status in (T_FAILED, T_USERNAME_MISSING, T_PENDING_ACCEPT,
                                     T_NEEDS_USERNAME):
                remedy = REMEDY[out.team_status]
            else:
                remedy = REMEDY.get(out.org_status, "")
            writer.writerow([
                out.record.row, out.record.label, out.record.email, out.record.login,
                ORG_LABEL.get(out.org_status, out.org_status), out.org_detail,
                TEAM_LABEL.get(out.team_status, out.team_status), out.team_detail,
                problem, remedy, now_text(),
            ])

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for out in outcomes:
            handle.write(json.dumps({
                "row": out.record.row,
                "name": out.record.name,
                "email": out.record.email,
                "login": out.record.login,
                "org_status": out.org_status,
                "org_detail": out.org_detail,
                "team_status": out.team_status,
                "team_detail": out.team_detail,
                "committed": out.committed,
                "time": now_text(),
            }, ensure_ascii=False) + "\n")
    return csv_path, jsonl_path


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    started = time.time()
    args = parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent / "output"

    # ---- 只读状态查询 ----
    if args.status:
        if args.targets:
            die("--status 是只读查询, 不接受表格或用户名参数。\n"
                f"用法: python {APP_NAME}.py --org {args.org} --status")
        token = resolve_token(args, required=True)
        print_status_report(GitHub(token, args.api_url), args.org)
        return 0

    if args.limit < 0:
        die("--limit 不能是负数。")
    if args.delay < 0:
        die("--delay 不能是负数。")

    # ---- 输入 ----
    source = resolve_input(args)
    log("")
    log(f"输入来源: {source.origin}")
    if source.header:
        log(f"表头: {' | '.join(str(c) for c in source.header)}")
    log(f"列映射: 邮箱=<{_name_of(source.header, source.columns.get('email'))}>  "
        f"用户名=<{_name_of(source.header, source.columns.get('username'))}>  "
        f"姓名=<{_name_of(source.header, source.columns.get('name'))}>")

    valid, duplicates, excluded = deduplicate(source.records)
    blank = max(0, source.data_rows - len(source.records))
    log(f"解析到 {len(source.records)} 行数据"
        + (f"(另有 {blank} 行空行/注释行已忽略)" if blank else "")
        + f", 排除 {len(excluded)} 行, 待处理 {len(valid)} 条。")
    for out in excluded:
        log(f"  - 排除第 {out.record.row} 行: {out.org_detail}")
    warned = 0
    for record in source.records:
        for note in record.notes:
            if note:
                warned += 1
                log(f"  ! 第 {record.row} 行: {note}")
    if warned:
        log(f"  ({warned} 条提醒, 不影响处理)")

    if not valid and not args.no_preflight:
        die("没有可处理的记录。")

    # ---- token / 客户端 ----
    needs_api = not args.no_preflight
    mode = "unknown" if args.no_preflight else ("execute" if args.execute else "plan")
    token = resolve_token(args, required=needs_api)
    gh = GitHub(token, args.api_url) if token else None

    if needs_api and gh is None:
        die("预检需要 token。要么配好 token, 要么加 --no-preflight 只看解析结果。")

    log("")
    if mode == "execute":
        log("!! 已开启 --execute, 会真正发出邀请 !!")
    elif mode == "plan":
        log("dry-run: 不会发出任何邀请(加 --execute 才真正发送)")
    else:
        log("未预检模式: 只解析表格, 不会发任何请求, 也不知道谁已经在组织里")

    # ---- 预检 ----
    members: set[str] = set()
    pending_emails: set[str] = set()
    pending_logins: set[str] = set()
    current_team: set[str] | None = None
    team: dict = {}
    if needs_api:
        assert gh is not None
        if args.verbose:
            log("")
            log("预检: 读取组织成员与待处理邀请 ...")
        try:
            members = gh.org_members(args.org)
            pending_emails, pending_logins = gh.org_pending(args.org)
        except ApiError as exc:
            die(f"预检失败: {exc.message}\n"
                f"先确认组织名, 以及 token 权限(细粒度要 Members 写权限且组织已批准; "
                f"经典 token 要 admin:org)。\n"
                f"如果只是想看看表格解析结果, 加 --no-preflight。", 3)

        if args.team:
            team = select_team(gh, args.org, args.team)
            current_team = gh.team_members(args.org, str(team.get("slug")))
        print_preflight(members, pending_emails, pending_logins, current_team,
                        str(team.get("name") or args.team) if args.team else "")

    # ---- 邮箱反查用户名(可选) ----
    if args.resolve_emails and needs_api:
        assert gh is not None
        resolve_emails_to_logins(gh, valid)

    # ---- 续跑历史 ----
    history: dict[str, dict] = {}
    if not args.no_resume:
        history = load_history(out_dir)
        if history:
            log("")
            log(f"续跑: 读到 {len(history)} 条历史记录, 上次已成功的人会自动跳过"
                f"(要全部重跑加 --no-resume)")

    # ---- 本次范围: --limit 是「表里的前 N 行」 ----
    #
    # 判断某一行在不在前 N 行时, 重复行和没有联系方式的空行都要算进去, 否则
    # --limit 会静默跳过更多人, 而不是老老实实只处理表里前 N 行。
    limit = args.limit
    if limit:
        scope_duplicates = [out for out in excluded
                            if out.org_status == S_DUPLICATE and out.record.row <= limit]
        scope_no_contact = [out for out in excluded
                            if out.org_status == S_NO_CONTACT and out.record.row <= limit]
        in_scope = [r for r in valid if r.row <= limit]
        skipped_by_limit = [Outcome(record, S_UNPROCESSED,
                                    f"--limit {limit}, 本次没轮到, 下次不带 --limit 会处理")
                            for record in valid if record.row > limit]
        log("")
        log(f"--limit {limit}: 本次只看表里第 1~{limit} 行 —— 待处理 {len(in_scope)} 条, "
            f"留到下次 {len(skipped_by_limit)} 条。")
    else:
        scope_duplicates = [out for out in excluded if out.org_status == S_DUPLICATE]
        scope_no_contact = [out for out in excluded if out.org_status == S_NO_CONTACT]
        in_scope = list(valid)
        skipped_by_limit = []

    # ---- 第 1 档: 组织邀请 ----
    stamp = timestamp()
    log("")
    log(f"== 组织邀请: {args.org}(角色 {args.role}) ==")
    org_outcomes = process_org_invites(gh, args, in_scope, members, pending_emails,
                                       pending_logins, history, {}, mode)

    # ---- 第 2 档: team ----
    if args.team:
        if not needs_api:
            log("")
            log("== Team 加入: 已跳过(未预检模式不知道谁在组织里) ==")
        else:
            slug = str(team.get("slug"))
            log("")
            log(f"== Team 加入: {team.get('name')}(角色 {args.team_role}) ==")
            process_team(gh, args, org_outcomes, members, current_team or set(), slug, mode, {})
            for out in org_outcomes:
                if out.team_status == T_USERNAME_MISSING:
                    log(f"  ✗ {out.record.label}: {TEAM_LABEL[out.team_status]} —— {out.team_detail}")
                elif out.team_status == T_FAILED:
                    log(f"  ✗ {out.record.label}: {TEAM_LABEL[out.team_status]} —— {out.team_detail}")
                elif out.team_status in (T_PENDING_ACCEPT, T_NEEDS_USERNAME):
                    log(f"  · {out.record.label}: {TEAM_LABEL[out.team_status]}")

    outcomes = org_outcomes + scope_duplicates + scope_no_contact + skipped_by_limit

    # ---- 打印 ----
    print_results(outcomes, bool(args.team))
    print_summary(outcomes, bool(args.team), args, time.time() - started, mode)
    print_issues(outcomes)
    if args.team and needs_api:
        print_retry_list(outcomes, _rerun_hint(args, source))

    # ---- 落盘 ----
    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_outputs(out_dir, stamp, outcomes, execute=(mode == "execute"))
    log("")
    if mode != "execute":
        log("[DRY-RUN] 没有发出任何邀请。")
        log(f"计划已写入: {written[0]}")
        log("确认无误后加 --execute 真正发送(建议先加 --limit 3 小批量试跑)。")
    else:
        log(f"结果 CSV : {written[0]}")
        log(f"结果 JSONL: {written[1]}")
        log("重跑同一条命令会自动跳过这次已经成功的人(想全部重发加 --no-resume)。")
        if not needs_api:
            log("注意: 本次带了 --no-preflight, 没有真的发出邀请(状态不明), 别拿这份当结果。")
        if any(out.org_status == S_INVITED and out.record.email and not out.record.login
               for out in outcomes):
            log("提示: 只有邮箱的那些行, 若对方没把该邮箱验证到自己的 GitHub 账号上, "
                "他点不动邀请链接。")
            log(f"      过几分钟对一遍: python {APP_NAME}.py --org {args.org} --status")

    failures = sum(1 for out in outcomes if out.has_failure)
    return 1 if failures else 0


def _rerun_hint(args: argparse.Namespace, source: InputSource) -> str:
    if source.table_path is not None:
        target = f'"{source.table_path.name}"'
    else:
        target = " ".join(str(t) for t in (args.targets or []))
    return (f"python {APP_NAME}.py --org {args.org} {target} "
            f'--team "{args.team}" --execute')


def _name_of(header: Sequence[str], index: int | None) -> str:
    if index is None:
        return "(未使用)"
    if index < len(header):
        return str(header[index])
    return f"第 {index + 1} 列"


if __name__ == "__main__":
    raise SystemExit(main())
