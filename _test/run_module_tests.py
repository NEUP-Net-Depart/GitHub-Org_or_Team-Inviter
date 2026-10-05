#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆分后的**结构**体检: 搬完家有没有把模块边界搞坏。

行为有没有变由其它套件盯着(尤其 run_offline / run_path); 这一套只查结构:
  1. 兼容层 `github_inviter.py` 里不许有 def/class —— 逻辑必须都在 inviter/ 下
  2. `inviter/` 下的模块不许反过来 import 兼容层(那是环, 也让包不能独立使用)
  3. 每个模块都能单独导入(真查循环导入)
  4. PROJECT_ROOT 锚在仓库根 —— token.txt 与默认 output/ 都挂在它上面
  5. 兼容层导出的名字一个不缺; `g.prompt` 就是 `inviter.prompt`(测试的替换点)
  6. 状态常量与三张表一一对上(搬家丢一个状态值会在这里当场现形)
  7. 没有两个模块各定义一份同名顶层函数(搬家的典型翻车方式)
  8. `python -m inviter` 与 `python github_inviter.py` 是同一个入口

不联网, 不发任何邀请, 不写任何文件。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import github_inviter as g  # noqa: E402
import inviter  # noqa: E402

PACKAGE = ROOT / "inviter"
FACADE = ROOT / "github_inviter.py"

PASS, FAIL = [], []

# 这些名字是别的测试 / 使用者直接引用过的, 缺一个就算搬家搬丢了。
REQUIRED_EXPORTS = [
    # run_path_tests / run_output_tests 直接驱动的东西
    "Record", "Outcome", "ORG_LABEL", "TEAM_LABEL", "REMEDY", "ORG_OK", "ORG_SKIP",
    "S_PLANNED", "S_INVITED", "S_ALREADY_MEMBER", "S_ALREADY_INVITED", "S_NO_CONTACT",
    "S_DUPLICATE", "S_RESUMED", "S_UNPROCESSED", "S_FAILED",
    "T_ALREADY", "T_ADDED", "T_PENDING_ACCEPT", "T_NEEDS_USERNAME",
    "T_USERNAME_MISSING", "T_FAILED", "T_NA",
    "parse_conflict_message", "process_org_invites", "process_team",
    "write_outputs", "load_history",
    # run_prompt_tests / check_token_formats 直接引用的
    "parse_row_range", "ask_data_rows", "_load_table_source", "_stdin_is_interactive",
    "_ask_line", "_read_token_file", "prompt",
    # 旧单文件的公开面(使用者可能已经用上了)
    "read_table", "build_records", "classify_cell", "clean_email", "clean_login",
    "detect_columns", "locate_header", "parse_cols_spec", "resolve_input",
    "records_from_tokens", "resolve_token", "GitHub", "ApiError", "describe_error",
    "deduplicate", "seen_in_history", "resolve_emails_to_logins", "select_team",
    "print_results", "print_summary", "print_issues", "print_retry_list",
    "print_status_report", "print_preflight", "print_table", "count_by",
    "result_row", "summarize_org", "summarize_team", "InputSource",
    "normalize_argv", "parse_args", "main", "default_out_dir",
    "log", "die", "clip", "pad", "display_width", "now_text", "timestamp", "norm_key",
    "APP_NAME", "APP_VERSION", "DEFAULT_API", "DEFAULT_DELAY", "PROJECT_ROOT",
]


def report(title: str, problems: list[str]) -> None:
    (PASS if not problems else FAIL).append(title)
    print(f"[{'PASS' if not problems else 'FAIL'}] {title}")
    for problem in problems:
        print(f"         {problem}")


def module_paths() -> list[Path]:
    return sorted(PACKAGE.glob("*.py"))


def top_level_defs(path: Path) -> list[str]:
    """只看模块顶层的 def/class —— 方法(在 ClassDef 里)不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def run_help(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


def str_constants(module, prefix: str) -> dict[str, str]:
    return {name: value for name, value in vars(module).items()
            if name.startswith(prefix) and isinstance(value, str)}


def main() -> int:
    # ---------- 1. 兼容层不含逻辑 ----------
    problems = []
    found = top_level_defs(FACADE)
    if found:
        problems.append(f"兼容层里还留着 {len(found)} 个定义: {', '.join(found[:8])}"
                        "(逻辑该住在 inviter/ 里)")
    report("兼容层只有转发, def/class 数量为 0", problems)

    # ---------- 2. inviter/ 不反向依赖兼容层 ----------
    problems = []
    for path in module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "github_inviter" or name.startswith("github_inviter."):
                    problems.append(f"{path.name} 反向引用了兼容层: {name}")
    report("inviter/ 不反向依赖兼容层(否则会成环)", problems)

    # ---------- 3. 每个模块都能单独导入 ----------
    problems = []
    for path in module_paths():
        name = f"inviter.{path.stem}"
        proc = subprocess.run([sys.executable, "-c", f"import {name}"], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            problems.append(f"{name} 单独导入失败: {tail[-1] if tail else '(无输出)'}")
    report("每个模块都能单独导入(无循环导入)", problems)

    # ---------- 4. PROJECT_ROOT 锚点 ----------
    problems = []
    if FACADE.parent != ROOT:
        problems.append(f"兼容层不在仓库根: {FACADE.parent}")
    if inviter.PROJECT_ROOT != ROOT:
        problems.append(f"PROJECT_ROOT={inviter.PROJECT_ROOT}, 期望 {ROOT}")
    if g.PROJECT_ROOT != ROOT:
        problems.append(f"g.PROJECT_ROOT={g.PROJECT_ROOT}, 期望 {ROOT}")
    if g.default_out_dir() != ROOT / "output":
        problems.append(f"默认输出目录={g.default_out_dir()}, 期望 {ROOT / 'output'}")
    # 换句话说: 「脚本旁边」必须是仓库根, 不能变成 inviter/ 里面
    if inviter.constants.PACKAGE_DIR == inviter.PROJECT_ROOT:
        problems.append("包目录与项目根相同, 说明 inviter/ 没放在仓库根下")
    report("PROJECT_ROOT 指向仓库根(token.txt / 默认 output/ 的锚点)", problems)

    # ---------- 5. 兼容层导出面 ----------
    problems = []
    if len(g.__all__) != len(set(g.__all__)):
        problems.append("__all__ 里有重名项")
    for name in g.__all__:
        if not hasattr(g, name):
            problems.append(f"__all__ 列了 {name!r}, 但取不到")
    for name in REQUIRED_EXPORTS:
        if not hasattr(g, name):
            problems.append(f"必须导出的 {name!r} 不见了")
    if g.prompt is not inviter.prompt:
        problems.append("g.prompt 不是 inviter.prompt —— 测试的替换点会失效")
    for name in ("_stdin_is_interactive", "_ask_line"):
        if getattr(g.prompt, name) is not getattr(inviter.prompt, name):
            problems.append(f"g.prompt.{name} 与 inviter.prompt.{name} 不是同一个对象")
    report("兼容层导出面完整, 且 g.prompt 就是 inviter.prompt", problems)

    # ---------- 6. 状态常量与三张表 ----------
    problems = []
    org_values = set(str_constants(inviter.outcomes, "S_").values())
    team_values = set(str_constants(inviter.outcomes, "T_").values())
    if not org_values or not team_values:
        problems.append("读不到 S_* / T_* 状态常量")
    if org_values & team_values:
        problems.append(f"组织档与 team 档状态值撞车: {sorted(org_values & team_values)}")
    if set(g.ORG_LABEL) != org_values:
        problems.append(f"ORG_LABEL 的键与 S_* 不一致: 缺 {sorted(org_values - set(g.ORG_LABEL))}, "
                        f"多 {sorted(set(g.ORG_LABEL) - org_values)}")
    if set(g.TEAM_LABEL) != team_values:
        problems.append(f"TEAM_LABEL 的键与 T_* 不一致: 缺 {sorted(team_values - set(g.TEAM_LABEL))}, "
                        f"多 {sorted(set(g.TEAM_LABEL) - team_values)}")
    # T_NA 是「本次没要求做 team」, 不是一种需要处理措施的状态, REMEDY 里没有它
    # —— 这条豁免与 run_output_tests.py 里的判定保持一致。
    wanted = (org_values | team_values) - {g.T_NA}
    if set(g.REMEDY) != wanted:
        problems.append(f"REMEDY 的键应覆盖两档全部状态(除 T_NA); 缺 "
                        f"{sorted(wanted - set(g.REMEDY))}, 多 {sorted(set(g.REMEDY) - wanted)}")
    for group in (g.ORG_OK, g.ORG_SKIP):
        if not group <= org_values:
            problems.append(f"ORG_OK/ORG_SKIP 里有不是 S_* 的值: {sorted(group - org_values)}")
    report("状态常量、ORG_LABEL/TEAM_LABEL、REMEDY 互相对得上", problems)

    # ---------- 6b. 状态归类不重不漏 ----------
    #
    # 汇总行之所以能保证「每个状态只数一次」, 靠的就是这几个集合互不重叠。
    # 踩过的坑: planned 曾被放进 ORG_SKIP, 于是 dry-run 的汇总把同一批人数了两遍
    # (4 行的表读出「计划邀请 4 人 … 跳过 4 人」)。所以这里把归类本身钉死。
    problems = []
    groups = {"ORG_OK": g.ORG_OK, "ORG_SKIP": g.ORG_SKIP, "ORG_PENDING": g.ORG_PENDING}
    for name_a, name_b in (("ORG_OK", "ORG_SKIP"), ("ORG_OK", "ORG_PENDING"),
                           ("ORG_SKIP", "ORG_PENDING")):
        overlap = groups[name_a] & groups[name_b]
        if overlap:
            problems.append(f"{name_a} 与 {name_b} 重叠: {sorted(overlap)}")
    if not g.ORG_UNTOUCHED <= g.ORG_SKIP:
        problems.append(f"ORG_UNTOUCHED 应是 ORG_SKIP 的子集, 多出: "
                        f"{sorted(g.ORG_UNTOUCHED - g.ORG_SKIP)}")
    if g.ORG_SETTLED != g.ORG_OK | g.ORG_SKIP:
        problems.append("ORG_SETTLED 应等于 ORG_OK | ORG_SKIP")
    # 每个状态都要有归属(失败单列), 否则汇总行一定会漏人
    accounted = g.ORG_OK | g.ORG_SKIP | g.ORG_PENDING | {g.S_FAILED}
    if accounted != org_values:
        problems.append(f"有状态没被归类: {sorted(org_values - accounted)}; "
                        f"归到了不存在的状态: {sorted(accounted - org_values)}")
    report("状态归类不重不漏(每个状态只属于一个组)", problems)

    # ---------- 7. 没有重复的顶层函数 ----------
    problems = []
    owner: dict[str, str] = {}
    for path in module_paths():
        for name in top_level_defs(path):
            if name in owner:
                problems.append(f"{name!r} 同时定义在 {owner[name]} 和 {path.name}")
            else:
                owner[name] = path.name
    report("没有两个模块各定义一份同名顶层函数", problems)

    # ---------- 8. 两个入口等价 ----------
    problems = []
    facade = run_help([str(FACADE), "--help"])
    module = run_help(["-m", "inviter", "--help"])
    if facade.returncode != 0:
        problems.append(f"python github_inviter.py --help 退出码 {facade.returncode}")
    if module.returncode != 0:
        problems.append(f"python -m inviter --help 退出码 {module.returncode}")
    if facade.stdout != module.stdout:
        problems.append("两个入口的 --help 输出不一致(应完全一样)")
    report("python github_inviter.py 与 python -m inviter 等价", problems)

    print(f"\n===== 模块边界体检: 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
    for title in FAIL:
        print(f"  失败: {title}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
