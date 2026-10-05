# -*- coding: utf-8 -*-
"""token 解析: `--token` > 环境变量 > `token.txt` / `.env`。

只打印 token 的**来源**, 绝不打印内容。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .console import die, log
from .constants import PROJECT_ROOT

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
    """优先级: --token > 环境变量 > 当前目录/脚本目录的 token.txt。只打印来源, 不打印内容。

    注意「脚本目录」用的是 `constants.PROJECT_ROOT`(仓库根), 不是
    `Path(__file__).parent` —— 后者在拆模块后会指向 `inviter/` 而找错地方。
    """
    token = (getattr(args, "token", "") or "").strip()
    source = "--token 参数"
    if not token:
        token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
        source = "环境变量"
    if not token:
        for directory in (Path.cwd(), PROJECT_ROOT):
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
