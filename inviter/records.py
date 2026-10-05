# -*- coding: utf-8 -*-
"""记录构造: 单元格清洗 + 按内容归类。

不靠列名判内容 —— 用户的表格是问卷导出的, 「邮箱列里填了用户名」这种事很常见,
所以两列都过一遍 `classify_cell`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .console import clip
from .constants import EMAIL_RE, GITHUB_URL_RE, LOGIN_RE


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
