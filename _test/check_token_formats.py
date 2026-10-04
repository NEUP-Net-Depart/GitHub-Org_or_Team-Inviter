#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认 token.txt 的两种写法都能读: 裸 token / GITHUB_TOKEN=xxx。"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import github_inviter as g  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="token-test-"))
cases = {
    "裸 token(README 推荐的写法)": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n",
    "KEY=VALUE": "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n",
    "带注释 + 裸 token": "# 我的 token\nghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n",
    "前后有空白": "  ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789  \n",
    "带引号": '"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n',
    "空文件": "\n\n",
}
for title, content in cases.items():
    path = work / "token.txt"
    path.write_text(content, encoding="utf-8")
    got = g._read_token_file(path)
    if "空文件" in title:
        print(f"{'PASS' if got == '' else 'FAIL'}  {title}: -> {got!r}")
    else:
        ok = got == "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        print(f"{'PASS' if ok else 'FAIL'}  {title}: -> {'(读到正确 token)' if ok else got!r}")
