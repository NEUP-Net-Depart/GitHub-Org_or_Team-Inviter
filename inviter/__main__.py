# -*- coding: utf-8 -*-
"""`python -m inviter` 的入口, 与 `python github_inviter.py` 完全等价。"""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
