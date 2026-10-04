#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 docs/examples/ 下的每个示例都能被脚本正确解析。

README 里指向这些文件, 所以它们必须真的能用 —— 这个脚本就是那道保险。
只跑 --no-preflight, 不联网。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "github_inviter.py"

CASES = [
    ("收集表示例.csv", ["docs/examples/收集表示例.csv"], "待处理 4 条"),
    ("收集表示例.xlsx", ["docs/examples/收集表示例.xlsx"], "待处理 4 条"),
    ("只有姓名和邮箱.csv", ["docs/examples/只有姓名和邮箱.csv"], "待处理 2 条"),
    # 列顺序不一样: 靠表头名字认列, 不需要 --cols
    ("列顺序不一样.csv", ["docs/examples/列顺序不一样.csv"], "待处理 2 条"),
    # 无表头的表才需要 --cols 按位置说明
    ("无表头 + --cols",
     ["--no-header", "docs/examples/收集表示例.csv", "--cols", "name,email,username"],
     "待处理 5 条"),
]

failed = []
for title, args, expect in CASES:
    proc = subprocess.run([sys.executable, str(SCRIPT), "--org", "demo-org",
                           "--no-preflight", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and expect in out
    print(f"[{'PASS' if ok else 'FAIL'}] {title}")
    if not ok:
        failed.append(title)
        print(f"         退出码={proc.returncode}, 期望包含 {expect!r}")
        for line in out.splitlines()[:12]:
            print(f"         {line}")

print(f"\n===== 示例文件验证: 通过 {len(CASES) - len(failed)} / 失败 {len(failed)} =====")
raise SystemExit(1 if failed else 0)
