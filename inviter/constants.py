# -*- coding: utf-8 -*-
"""全局常量与项目根路径。

这里只放常量, 不放行为。`PROJECT_ROOT` 的位置是关键: 它是「脚本旁边的
`token.txt`」和「默认 `output/`」的锚点 —— 见 tokens.py 与 cli.py。
"""
from __future__ import annotations

import re
from pathlib import Path

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

# 包目录与项目根。约定: `inviter/` 必须直接位于仓库根下, 于是
#   PROJECT_ROOT == 仓库根 == github_inviter.py 所在目录。
#
# 拆模块之前, token.txt 与默认 output/ 用的是 `Path(__file__).resolve().parent`;
# 那段代码搬进子模块后, `__file__` 会指向 `inviter/` 而不是仓库根, 二者都会跟着跑偏。
# 所以统一锚在这里, 子模块一律用 PROJECT_ROOT, 不许再各自算 `__file__`。
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
