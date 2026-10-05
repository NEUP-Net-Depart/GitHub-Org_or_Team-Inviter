# -*- coding: utf-8 -*-
"""批量邀请人加入 GitHub 组织, 可选顺手加入指定 Team。

实现按职责拆成这个包里的若干模块(见 ARCHITECTURE.md 的模块地图)。
对外入口仍然是仓库根的 `github_inviter.py` —— 它只是一个转发用的兼容层。

分层规则: 依赖只能自上而下, 不许成环。
    constants / console
      -> columns / records / tokens
        -> tableio / prompt / github_api / outcomes
          -> input_source / report / storage
            -> workflow
              -> cli
"""
from __future__ import annotations

from .constants import APP_NAME, APP_VERSION, PROJECT_ROOT

__version__ = APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION", "PROJECT_ROOT", "__version__"]
