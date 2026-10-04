#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描**整个 git 历史**(不只是当前这一版)里的敏感信息。

`_test/scan_pii.py` 只看 `git ls-files`, 也就是"工作区里现在这一版"。
可是**历史是跟着 push 一起走的**: 一个曾经提交过的真实姓名, 哪怕后来删掉了,
`git log -p` 照样翻得出来。所以推送前应该把历史也扫一遍。

两套判断分开报, 因为可信度差得远:

  * **精确命中(算失败, 退出码 1)** —— `.pii-terms` 里的词(真实姓名/用户名/邮箱),
    以及本地 `token.txt` 里那个 token 的**真实值**。命中就是真泄露。
  * **形态命中(只提示, 不算失败)** —— `ghp_` / `github_pat_` 这类 token 长相。
    测试和文档里到处是占位符, 一定会命中, 所以只能当参考, 不能当结论。

实现: `git rev-list --objects --all` 列出所有**可达**对象, 再用 `git cat-file --batch`
一次性把 blob 倒出来比对。不可达的本地垃圾不会被 push, 所以不扫。

**只报文件名和行号, 绝不回显命中的内容** —— 免得这个扫描器自己变成新的泄露点(踩过这个坑)。
token 的比对在 Python 内存里做, 不进命令行参数, 也不打印。

用法:
    python _test/scan_pii_history.py            # 有 .pii-terms 就按它扫
    python _test/scan_pii_history.py 张三 李四   # 也可以临时在命令行给词(会留在命令历史里)

退出码: 0 = 没有精确命中, 1 = 有精确命中, 2 = 一个词都没有(既没词表也没 token.txt)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / ".pii-terms"
TOKEN_FILE = ROOT / "token.txt"

SHAPES = [
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_ 经典 token 形态"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_ 细粒度 token 形态"),
]


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"git {' '.join(args)} 失败: {proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def token_value() -> str:
    """从 token.txt 取出 token 本身(裸 token 和 KEY=VALUE 都认)。取不到返回空串。"""
    if not TOKEN_FILE.is_file():
        return ""
    for line in TOKEN_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        value = line.split("=", 1)[1] if "=" in line else line
        value = value.strip().strip('"').strip("'")
        return value if len(value) >= 12 else ""
    return ""


def load_entries() -> tuple[list[tuple[str, str]], int]:
    """返回 ([(要找的串, 命中后显示的名字)], 用户真正提供的词数)。

    显示名不含串本身。provided 为 0 时说明一个真词都没有, 那时应报"没有词表"而不是"干净"。
    """
    entries: list[tuple[str, str]] = []
    for word in sys.argv[1:]:
        if word.strip():
            entries.append((word, f"命令行第 {len(entries) + 1} 个词"))
    provided = len(entries)
    if TERMS_FILE.is_file():
        for line in TERMS_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append((line, f"词表第 {len(entries) + 1} 行"))
                provided += 1
    token = token_value()
    if token:
        entries.append((token, "token.txt 里的 token"))
        provided += 1
    return entries, provided


def reachable_blobs() -> dict[str, set[str]]:
    """blob sha -> 它在这个仓库里出现过的路径(同一个 blob 可能在多个路径下)。"""
    pairs: dict[str, set[str]] = {}
    for line in git("rev-list", "--objects", "--all").splitlines():
        sha, _, path = line.partition(" ")
        if sha:
            pairs.setdefault(sha, set())
            if path:
                pairs[sha].add(path)
    return pairs


def main() -> int:
    entries, provided = load_entries()
    if provided == 0:
        print(f"没有词表。请把要查的姓名/用户名/邮箱每行一个写进 {TERMS_FILE.name}"
              f"(已被 .gitignore 挡住), 或在命令行给出。")
        return 2

    commits = len(git("rev-list", "--all").split())
    pairs = reachable_blobs()
    shas = sorted(pairs)
    dump = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=str(ROOT),
        input=("\n".join(shas) + "\n").encode(), capture_output=True,
    ).stdout

    hits: dict[str, list[tuple[int, str]]] = {}
    shapes: dict[str, list[tuple[int, str]]] = {}
    scanned = 0
    pos = 0
    while pos < len(dump):
        nl = dump.index(b"\n", pos)
        header = dump[pos:nl].decode("utf-8", "replace")
        if header.endswith(" missing"):
            pos = nl + 1
            continue
        sha, kind, size = header.split()
        size = int(size)
        body = dump[nl + 1: nl + 1 + size]
        pos = nl + 1 + size + 1
        if kind != "blob":
            continue
        scanned += 1
        paths = sorted(pairs.get(sha, {"<无路径>"}))
        for lineno, line in enumerate(body.decode("utf-8", "replace").splitlines(), start=1):
            for needle, label in entries:
                if needle and needle in line:
                    for path in paths:
                        hits.setdefault(path, []).append((lineno, label))
            for pattern, label in SHAPES:
                if pattern.search(line):
                    for path in paths:
                        shapes.setdefault(path, []).append((lineno, label))

    print(f"扫了 {commits} 个提交 / {scanned} 个 blob / {len(entries)} 个精确待查串")

    if not hits:
        print("精确命中: 无 —— 历史里没有真实姓名/邮箱/用户名, 也没有本地这个 token")
    else:
        print("\n!! 精确命中, 历史里有敏感信息:\n")
        for path, items in sorted(hits.items()):
            labels = sorted({label for _, label in items})
            lines = sorted({n for n, _ in items})
            print(f"  {path}")
            print(f"      命中: {', '.join(labels)}")
            print(f"      行号: {', '.join(str(n) for n in lines)}")
        print(f"\n共 {len(hits)} 个文件需要处理。")
        print("注意: 只是把文件删掉再提交一次是不够的, 旧提交里的内容依然在 ——")
        print("      要用 git filter-repo / rebase 重写历史, 然后强推。")

    if shapes:
        print("\n参考(不影响退出码): 下面这些位置有 token 长相的串, 多半是测试/文档里的占位符:")
        for path, items in sorted(shapes.items()):
            lines = sorted({n for n, _ in items})
            print(f"  {path}  行 {', '.join(str(n) for n in lines)}")

    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
