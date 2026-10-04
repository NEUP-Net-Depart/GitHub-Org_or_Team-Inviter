#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描所有被跟踪文件, 找出真实个人信息。

**这个脚本本身不能包含真实信息** —— 否则它就成了新的泄露点(踩过这个坑)。
所以敏感词表放在一个**不入库**的文件里, 用 .gitignore 挡住:

    <仓库根>/.pii-terms      每行一个词, # 开头是注释

用法:
    python _test/scan_pii.py            # 有 .pii-terms 就按它扫
    python _test/scan_pii.py 张三 李四   # 也可以临时在命令行给词(但会留在命令历史里)

退出码: 0 = 干净, 1 = 有文件命中, 2 = 没有词表
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / ".pii-terms"

# 不带任何真实信息的内置兜底: 只查 token 和通用邮箱形态
GENERIC = [
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
]


def load_terms() -> list[str]:
    terms = [t for t in sys.argv[1:] if t.strip()]
    if TERMS_FILE.is_file():
        for line in TERMS_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    return terms


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return [line for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    terms = load_terms()
    if not terms:
        print(f"没有词表。请把要查的姓名/用户名/邮箱每行一个写进 {TERMS_FILE.name}"
              f"(已被 .gitignore 挡住), 或在命令行给出。")
        print("注意: 这个文件不能提交进版本库 —— 它本身就是敏感信息。")
        return 2

    hits: dict[str, list[tuple[int, str]]] = {}
    for name in tracked_files():
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for term in terms:
                if term in line:
                    hits.setdefault(name, []).append((lineno, term))

    if not hits:
        print(f"干净: 被跟踪文件里没有命中这 {len(terms)} 个词")
        return 0

    print("!! 以下被跟踪文件中命中了敏感词:\n")
    for name, items in sorted(hits.items()):
        lines = sorted({n for n, _ in items})
        print(f"  {name}")
        print(f"      命中词数: {len({w for _, w in items})}")
        print(f"      行号    : {', '.join(str(n) for n in lines)}")
    print(f"\n共 {len(hits)} 个文件需要脱敏")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
