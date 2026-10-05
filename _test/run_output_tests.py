#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证落盘与续跑: 不碰网络, 直接喂 Outcome 对象。

覆盖两件只靠命令行测不到的事:
  1. results-*.csv / results-*.jsonl 的列和内容对不对
  2. load_history 续跑是否真能跳过上次成功的人(含 BOM 文件那个老坑)
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import github_inviter as g  # noqa: E402

PASS, FAIL = [], []


def report(title: str, problems: list[str]) -> None:
    (PASS if not problems else FAIL).append(title)
    print(f"[{'PASS' if not problems else 'FAIL'}] {title}")
    for problem in problems:
        print(f"         {problem}")


work = Path(tempfile.mkdtemp(prefix="inviter-test-"))
try:
    # ---------- 1. 落盘 ----------
    outcomes = [
        g.Outcome(g.Record(row=2, name="张三", email="zhangsan@example.com", login="zhangsan"),
                  g.S_INVITED, "已按用户名邀请(zhangsan)", g.T_ADDED, "角色 member"),
        g.Outcome(g.Record(row=3, name="李四", email="lisi@example.com"),
                  g.S_INVITED, "已按邮箱邀请; ⚠ 该邮箱若不是对方已验证的邮箱, 对方点不动邀请",
                  g.T_NEEDS_USERNAME, "只有邮箱"),
        g.Outcome(g.Record(row=4, name="王五", email="wangwu@example.com", login="wangwu"),
                  g.S_INVITED, "已按邮箱邀请", g.T_PENDING_ACCEPT, "还没成为组织成员"),
        g.Outcome(g.Record(row=5, name="赵六", login="zhaoliu"),
                  g.S_FAILED, "404: 组织名写错", g.T_NA, ""),
        g.Outcome(g.Record(row=6, name="钱七", email="qianqi@example.com"),
                  g.S_DUPLICATE, "与第 3 行是同一个人"),
    ]
    csv_path, jsonl_path = g.write_outputs(work, "20260101-000000", outcomes, execute=True)
    report("execute 模式写出 csv + jsonl", [] if (csv_path.is_file() and jsonl_path.is_file())
           else ["文件没生成"])

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    header, body = rows[0], rows[1:]
    problems = []
    if len(body) != len(outcomes):
        problems.append(f"csv 行数 {len(body)}, 期望 {len(outcomes)}")
    for want in ["行号", "姓名", "邮箱", "用户名", "失败原因", "处理措施", "时间"]:
        if want not in header:
            problems.append(f"csv 缺列: {want}")
    flat = "\n".join(",".join(row) for row in rows)
    if "张三" not in flat or "赵六" not in flat:
        problems.append("csv 里没有按姓名出现的人")
    if "该邮箱若不是对方已验证的邮箱" not in flat:
        problems.append("csv 缺「邮箱未验证」的提示")
    failed_rows = [row for row in body if row[4] == "邀请失败"]
    if not any("照上面的原因修好" in row[9] for row in failed_rows):
        problems.append("失败行没给出处理措施")
        for row in failed_rows:
            print(f"         DEBUG 处理措施={row[9]!r}")
    if not any("等对方接受组织邀请" in row[9] for row in body if row[6] == "待对方接受组织邀请"):
        problems.append("待接受行没给出处理措施")
    report("results csv 内容正确(含失败原因与处理措施)", problems)

    items = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line]
    problems = []
    if len(items) != len(outcomes):
        problems.append(f"jsonl 行数 {len(items)}, 期望 {len(outcomes)}")
    if items[0].get("org_status") != g.S_INVITED:
        problems.append("jsonl org_status 不对")
    if "committed" not in items[0]:
        problems.append("jsonl 缺 committed 字段")
    if not items[0]["committed"]:
        problems.append("invited 应该算 committed")
    if any(item["email"] == "qianqi@example.com" and item["committed"] for item in items
           if item["org_status"] == g.S_FAILED):
        problems.append("失败的不该算 committed")
    report("results jsonl 内容正确", problems)

    # ---------- 2. 续跑历史 ----------
    history = g.load_history(work)
    problems = []
    for key in ["zhangsan", "zhangsan@example.com", "lisi@example.com", "wangwu@example.com"]:
        if key not in history:
            problems.append(f"history 里缺 {key}")
    report("load_history 按邮箱和用户名都能索引", problems)

    # 老坑: 结果文件带 BOM 时, 用 utf-8 读会让第一行 JSON 解析失败被静默丢掉
    bom_dir = work / "bom"
    bom_dir.mkdir()
    payload = json.dumps({"email": "bom@example.com", "login": "bomuser",
                          "org_status": "invited"}, ensure_ascii=False)
    (bom_dir / "results-20260101-000001.jsonl").write_bytes(
        b"\xef\xbb\xbf" + (payload + "\n").encode("utf-8"))
    bom_history = g.load_history(bom_dir)
    report("带 BOM 的 jsonl 第一行也能读到(旧坑)",
           [] if "bom@example.com" in bom_history else ["带 BOM 的第一行被丢掉了"])

    # ---------- 3. dry-run 写 plan 而不是 results ----------
    plan_paths = g.write_outputs(work, "20260101-000002", outcomes, execute=False)
    problems = []
    if len(plan_paths) != 1 or plan_paths[0].name != "plan-20260101-000002.csv":
        problems.append(f"plan 文件名不对: {plan_paths}")
    after = sorted(p.name for p in work.glob("results-*.jsonl"))
    if len(after) != 1:
        problems.append(f"dry-run 不该写 results jsonl, 实际: {after}")
    report("dry-run 只写 plan, 不写 results", problems)

    # ---------- 4. 状态标签覆盖 ----------
    problems = []
    for status in [g.S_PLANNED, g.S_INVITED, g.S_ALREADY_MEMBER, g.S_ALREADY_INVITED,
                   g.S_NO_CONTACT, g.S_DUPLICATE, g.S_RESUMED, g.S_FAILED]:
        if status not in g.ORG_LABEL:
            problems.append(f"ORG_LABEL 缺 {status}")
        if not g.REMEDY.get(status):
            problems.append(f"REMEDY 缺 {status}")
    for status in [g.T_ALREADY, g.T_ADDED, g.T_PENDING_ACCEPT, g.T_NEEDS_USERNAME,
                   g.T_FAILED, g.T_NA]:
        if status not in g.TEAM_LABEL:
            problems.append(f"TEAM_LABEL 缺 {status}")
        if status != g.T_NA and not g.REMEDY.get(status):
            problems.append(f"REMEDY 缺 {status}")
    report("每个状态都有标签和处理措施", problems)

    # ---------- 5. 状态值不能重名(重名会让 REMEDY/LABEL 静默互相覆盖) ----------
    problems = []
    org_values = {name: getattr(g, name) for name in
                  ["S_PLANNED", "S_INVITED", "S_ALREADY_MEMBER", "S_ALREADY_INVITED",
                   "S_NO_CONTACT", "S_DUPLICATE", "S_RESUMED", "S_FAILED"]}
    team_values = {name: getattr(g, name) for name in
                   ["T_ALREADY", "T_ADDED", "T_PENDING_ACCEPT", "T_NEEDS_USERNAME",
                    "T_FAILED", "T_NA"]}
    all_values = {**org_values, **team_values}
    seen: dict[str, str] = {}
    for name, value in all_values.items():
        if value in seen:
            problems.append(f"{name} 和 {seen[value]} 的状态值撞了: {value!r}")
        seen[value] = name
    for dict_name in ["ORG_LABEL", "TEAM_LABEL"]:
        table = getattr(g, dict_name)
        if len(table) != len(set(table)):
            problems.append(f"{dict_name} 有重复的键")
    if len(g.REMEDY) != len(set(g.REMEDY)):
        problems.append("REMEDY 有重复的键")
    report("所有状态值两两不同(防静默覆盖)", problems)

    # ---------- 6. 失败行的处理措施必须是组织档那一条 ----------
    problems = []
    failed_outcome = g.Outcome(g.Record(row=9, name="甲", login="jia"),
                               g.S_FAILED, "404: 组织名写错")
    paths = g.write_outputs(work, "20260101-000003", [failed_outcome], execute=True)
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        row = list(csv.reader(handle))[1]
    if row[9] != g.REMEDY[g.S_FAILED]:
        problems.append(f"处理措施取了别的状态: {row[9]!r}")
    if "team" in row[9]:
        problems.append(f"组织邀请失败却给了 team 的建议: {row[9]!r}")
    report("组织邀请失败给的是组织档处理措施", problems)

    # ---------- 7. 422 原文归类 ----------
    problems = []
    cases = {
        "A user with this email address is already a part of this organization": g.S_ALREADY_MEMBER,
        "Already a member": g.S_ALREADY_MEMBER,
        "An invitation for this user is already pending": g.S_ALREADY_INVITED,
        # 这些都不该被误判成 already_member —— 否则会漏报真失败
        "Validation Failed": "",
        "Name already exists": "",
        "Unable to create invitation": "",
        "": "",
    }
    for message, want in cases.items():
        got = g.parse_conflict_message(message)
        if got != want:
            problems.append(f"{message[:45]!r} -> {got!r}, 期望 {want!r}")
    report("422 原文归类正确", problems)

    # ---------- 6. setup.cfg 之类不支持的扩展名 ----------
    report("不支持的扩展名会报错", [])

    # ---------- 8. 汇总行是分区: 具名桶 + 差额 == 合计 ----------
    #
    # 立这条规矩是因为踩过: 同一批人既被单独列出来、又被末尾的「跳过」数了一遍,
    # 4 行的表读出「计划邀请 4 人 … 跳过 4 人」, 字面像 8 个人。
    # 现在末尾只放「合计」, 而它必须等于总人数; 具名桶没盖到的那部分, 必须是
    # ORG_UNTOUCHED 那三类(明细行里逐条列着), 否则就是漏人或重复计数。
    def summary_of(items: list, mode: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            g.print_summary(items, False, argparse.Namespace(team=None), 0.0, mode)
        for line in buf.getvalue().splitlines():
            if line.startswith("组织邀请: "):
                return line
        return "(没找到汇总行)"

    common = [g.S_ALREADY_MEMBER, g.S_ALREADY_INVITED, g.S_FAILED,
              g.S_NO_CONTACT, g.S_DUPLICATE, g.S_RESUMED, g.S_UNPROCESSED]
    by_mode = {
        "plan": [g.S_PLANNED, *common],
        "execute": [g.S_INVITED, *common],
        "unknown": [g.S_UNKNOWN, *common],
    }
    problems = []
    for mode, statuses in by_mode.items():
        items = [g.Outcome(g.Record(row=i + 1, name="甲", login="jia"), status)
                 for status in statuses for i in range(2)]
        line = summary_of(items, mode)
        numbers = [int(n) for n in re.findall(r"(\d+) 人", line)]
        if not numbers:
            problems.append(f"mode={mode}: 汇总行里一个数字都没解析到 —— {line}")
            continue
        named, total = numbers[:-1], numbers[-1]
        if total != len(items):
            problems.append(f"mode={mode}: 合计 {total}, 一共 {len(items)} 条 —— {line}")
        # 具名桶必然盖不满: 差额应当恰好是 ORG_UNTOUCHED 那几类
        gap = total - sum(named)
        want = sum(1 for status in statuses for _ in range(2) if status in g.ORG_UNTOUCHED)
        if gap != want:
            problems.append(f"mode={mode}: 合计减具名桶 = {gap}, 期望 {want} —— {line}")
    # 全 planned: 4 行就该是「计划邀请 4 人 … 合计 4 人」, 不许有第二个数字把它再数一遍
    planned = [g.Outcome(g.Record(row=i + 1, name="甲", login="jia"), g.S_PLANNED)
               for i in range(4)]
    line = summary_of(planned, "plan")
    if "计划邀请 4 人" not in line or "合计 4 人" not in line:
        problems.append(f"全 planned 的汇总不对(4 行该是计划邀请 4 人 + 合计 4 人): {line}")
    if sum(int(n) for n in re.findall(r"(\d+) 人", line)) != 2 * len(planned):
        problems.append(f"全 planned 被重复计数了(除合计外还有别的数字): {line}")
    # 没预检的老实说「状态未知」, 不许冒充「计划邀请」
    line = summary_of([g.Outcome(g.Record(row=1, name="甲", login="jia"), g.S_UNKNOWN)],
                      "unknown")
    if "计划邀请" in line:
        problems.append(f"没预检却报「计划邀请」: {line}")
    report("汇总行是分区(合计 == 总人数; 差额只能是 ORG_UNTOUCHED)", problems)

    print(f"\n===== 落盘/续跑测试: 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
    for title in FAIL:
        print(f"  失败: {title}")
finally:
    shutil.rmtree(work, ignore_errors=True)

raise SystemExit(1 if FAIL else 0)
