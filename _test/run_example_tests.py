#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 docs/examples/ 下的每个示例都能被脚本正确解析。

README 里指向这些文件, 所以它们必须真的能用 —— 这个脚本就是那道保险。
只跑 --no-preflight, 不联网。

顺带守住一件事: dry-run 也会写计划文件(`plan-*.csv`), 默认落点是脚本旁的
`output/` —— 那是给人看**真实运行结果**的地方, 测试产物混进去只会让人误判
「我什么时候跑过、跑的是谁」。所以统一 `--out-dir` 到临时目录, 并断言 output/ 没变。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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
out_dir = ROOT / "output"
before = {p.name for p in out_dir.iterdir()} if out_dir.is_dir() else set()
tmp_out = Path(tempfile.mkdtemp(prefix="inviter-example-out-"))
try:
    for title, args, expect in CASES:
        proc = subprocess.run([sys.executable, str(SCRIPT), "--org", "demo-org",
                               "--out-dir", str(tmp_out),
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
finally:
    shutil.rmtree(tmp_out, ignore_errors=True)

after = {p.name for p in out_dir.iterdir()} if out_dir.is_dir() else set()
leaked = sorted(after - before)
print(f"[{'PASS' if not leaked else 'FAIL'}] 计划文件没落到脚本旁的 output/")
if leaked:
    failed.append("计划文件没落到脚本旁的 output/")
    print(f"         多出来的: {leaked}")

total = len(CASES) + 1
print(f"\n===== 示例文件验证: 通过 {total - len(failed)} / 失败 {len(failed)} =====")
raise SystemExit(1 if failed else 0)
