# -*- coding: utf-8 -*-
"""列识别: 三步定列 (显式参数 / 表头关键字 / 位置兜底)。

「哪一列是什么」和「哪几行是数据」是**两件独立的事**, 这里只管前者
(后者见 input_source.py)。
"""
from __future__ import annotations

import re
from typing import Sequence

from .console import die


def norm_key(value: str) -> str:
    """表头归一化: 去空白/下划线/连字符/括号/星号, 转小写。"""
    text = (value or "").strip().lower()
    return re.sub(r"[\s_\-/\\()（）\[\]:：*·]+", "", text)


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


def _blank_cols() -> dict[str, int | None]:
    """三列都没认出来时的空映射。放在这里, 因为 prompt 和 input_source 都要用它。"""
    return {"email": None, "username": None, "name": None}


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
