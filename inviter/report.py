# -*- coding: utf-8 -*-
"""控制台输出: 逐行表格 + 汇总 + 失败明细 + 待重跑名单 + `--status` 只读报告。

对齐靠 console.display_width/pad/clip(中文按 2 列宽)。
"""
from __future__ import annotations

import argparse
from typing import Iterable, Sequence

from .console import clip, die, display_width, log, pad
from .github_api import ApiError, GitHub
from .outcomes import (
    ORG_LABEL,
    REMEDY,
    S_ALREADY_INVITED,
    S_ALREADY_MEMBER,
    S_DUPLICATE,
    S_FAILED,
    S_INVITED,
    S_NO_CONTACT,
    S_PLANNED,
    S_RESUMED,
    S_UNKNOWN,
    S_UNPROCESSED,
    T_ADDED,
    T_ALREADY,
    T_FAILED,
    T_NA,
    T_NEEDS_USERNAME,
    T_PENDING_ACCEPT,
    T_USERNAME_MISSING,
    TEAM_LABEL,
    Outcome,
)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]],
                max_widths: Sequence[int] | None = None) -> None:
    if not rows:
        return
    widths: list[int] = []
    for index, title in enumerate(headers):
        widest = display_width(title)
        for row in rows:
            widest = max(widest, display_width(str(row[index])))
        if max_widths and index < len(max_widths) and max_widths[index] > 0:
            widest = min(widest, max_widths[index])
        widths.append(widest)

    def render(cells: Sequence[str]) -> str:
        parts = [pad(clip(str(cell), widths[i]), widths[i]) for i, cell in enumerate(cells)]
        return "  ".join(part.rstrip() for part in parts)

    log(render(headers))
    log("  ".join("-" * width for width in widths))
    for row in rows:
        log(render(row))


def count_by(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def summarize_org(counts: dict[str, int], mode: str) -> str:
    """组织邀请这一档的汇总。

    这一行是**一个分区**: 每个状态只进一个桶, 末尾的「合计」= 下面逐行结果的总人数。
    打印出来的具名桶相加和合计之间的差额, 一定来自 `ORG_UNTOUCHED`(没联系方式 /
    表里重复 / `--limit` 没轮到), 而那三类在明细行里逐条列着, 所以这行能当账对。

    这里踩过坑, 所以规矩写死:

      * `planned` 和 `already_invited` 曾经既被单独列出来、又被末尾一个总数数了一遍 ——
        4 行的表读出「计划邀请 4 人 … 跳过 4 人」, 字面像 8 个人(12 行的表读出 22 人)。
        末尾现在只放「合计」, 不放任何会重复计数的桶。
      * 末尾那个桶一度叫「本次没动」, 结果被一眼看穿: 措辞像在描述**整次运行**
        (dry-run 本来就什么都没发), 而它其实只描述一类行; 恒为 0 时更是废话。

    dry-run 时说「计划邀请」, 不要把它跟真发出去的混成一句「成功」;
    没预检 (`mode="unknown"`) 时连「计划」都算不上 —— 那只是「状态未知」。
    具名桶的措辞一律取自 ORG_LABEL, 免得汇总和明细两处叫法漂移。
    """
    total = sum(counts.values())          # count_by 的结果, 等于逐行结果的总人数
    from_previous = counts.get(S_RESUMED, 0)
    if mode == "execute":
        head = f"已发出邀请 {counts.get(S_INVITED, 0)} 人"
    elif mode == "unknown":
        head = f"状态未知(未预检) {counts.get(S_UNKNOWN, 0)} 人"
    else:
        head = f"计划邀请 {counts.get(S_PLANNED, 0)} 人"
    head += f" | {ORG_LABEL[S_ALREADY_MEMBER]} {counts.get(S_ALREADY_MEMBER, 0)} 人"
    if from_previous:
        # 续跑跳过的人数单独说一句, 让人一眼看到「记忆生效了」
        head += f" | 跳过(上次已成功) {from_previous} 人"
    return (f"{head} | {ORG_LABEL[S_ALREADY_INVITED]} {counts.get(S_ALREADY_INVITED, 0)} 人 | "
            f"{ORG_LABEL[S_FAILED]} {counts.get(S_FAILED, 0)} 人 | 合计 {total} 人")


def summarize_team(counts: dict[str, int], mode: str) -> str:
    verb = "已加入" if mode == "execute" else "计划加入"
    parts = [f"{verb} {counts.get(T_ADDED, 0)} 人",
             f"本来就在 {counts.get(T_ALREADY, 0)} 人"]
    if counts.get(T_PENDING_ACCEPT, 0):
        parts.append(f"待对方接受后重跑 {counts[T_PENDING_ACCEPT]} 人")
    if counts.get(T_USERNAME_MISSING, 0):
        parts.append(f"用户名不存在 {counts[T_USERNAME_MISSING]} 人")
    if counts.get(T_NEEDS_USERNAME, 0):
        parts.append(f"缺用户名 {counts[T_NEEDS_USERNAME]} 人")
    parts.append(f"失败 {counts.get(T_FAILED, 0)} 人")
    return " | ".join(parts)


def result_row(out: Outcome, with_team: bool) -> list[str]:
    row = [
        out.record.label or "—",
        out.record.email or "—",
        out.record.login or "—",
        ORG_LABEL.get(out.org_status, out.org_status),
    ]
    if with_team:
        team_text = TEAM_LABEL.get(out.team_status, out.team_status)
        if out.team_status == T_USERNAME_MISSING:
            team_text = "用户名不存在"
        elif out.team_status == T_FAILED and out.team_detail:
            team_text = f"加入失败: {clip(out.team_detail, 12)}"
        row.append(team_text)
    note = out.org_detail
    if out.team_status in (T_FAILED, T_USERNAME_MISSING) and out.team_detail:
        note = f"{note}; team: {out.team_detail}" if note else f"team: {out.team_detail}"
    row.append(note or "—")
    return row


def print_results(outcomes: Sequence[Outcome], with_team: bool) -> None:
    log("")
    log(f"== 逐行结果 ({len(outcomes)} 人) ==")
    headers = ["姓名", "邮箱", "用户名", "组织邀请"]
    if with_team:
        headers.append("Team")
    headers.append("说明")
    rows = [result_row(out, with_team) for out in outcomes]
    widths = [14, 24, 18, 20] + ([22] if with_team else []) + [38]
    print_table(headers, rows, widths)


def print_summary(outcomes: Sequence[Outcome], with_team: bool, args: argparse.Namespace,
                  elapsed: float, mode: str) -> None:
    org_counts = count_by(out.org_status for out in outcomes)
    log("")
    log("== 汇总 ==")
    log(f"组织邀请: {summarize_org(org_counts, mode)}")
    for status in (S_INVITED, S_PLANNED, S_UNKNOWN, S_ALREADY_MEMBER, S_ALREADY_INVITED,
                   S_FAILED, S_DUPLICATE, S_NO_CONTACT, S_RESUMED, S_UNPROCESSED):
        if org_counts.get(status):
            log(f"  - {ORG_LABEL[status]}: {org_counts[status]} 人")
    if with_team:
        team_counts = count_by(out.team_status for out in outcomes if out.team_status != T_NA)
        log(f"Team 加入: {summarize_team(team_counts, mode)}")
        log(f"  目标 team: {args.team}")
    log(f"耗时 {elapsed:.1f} 秒")


def print_issues(outcomes: Sequence[Outcome]) -> None:
    """把失败按「原因」归类, 每条给出处理措施 —— 需求里的硬性要求。"""
    groups: dict[str, list[Outcome]] = {}
    for out in outcomes:
        problem = ""
        if out.org_status == S_FAILED:
            problem = out.org_detail or "接口拒绝, 未给出原因"
        elif out.team_status == T_FAILED:
            problem = f"team: {out.team_detail or '接口拒绝, 未给出原因'}"
        if problem:
            groups.setdefault(problem, []).append(out)
    if not groups:
        return
    log("")
    log("== 失败明细(按原因归类) ==")
    for problem, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        names = "、".join(out.record.label for out in items)
        log(f"【{problem}】({len(items)} 人)")
        log(f"    涉及: {clip(names, 100)}")
        remedy = REMEDY[T_FAILED] if problem.startswith("team:") else REMEDY[S_FAILED]
        log(f"    处理: {remedy}")


def print_retry_list(outcomes: Sequence[Outcome], rerun_hint: str) -> None:
    """把「还需要人动手」的情况按性质分开列。"""
    pending_accept = [out for out in outcomes if out.team_status == T_PENDING_ACCEPT]
    missing = [out for out in outcomes if out.team_status == T_USERNAME_MISSING]
    needs_name = [out for out in outcomes if out.team_status == T_NEEDS_USERNAME]

    if pending_accept:
        names = "、".join(
            f"{out.record.label}({out.record.login or out.record.email})"
            for out in pending_accept[:20])
        log("")
        log(f"== 待重跑名单: 对方接受组织邀请后, 再跑一次即自动进 team "
            f"({len(pending_accept)} 人) ==")
        log(f"    {clip(names, 140)}" + (" …" if len(pending_accept) > 20 else ""))
        log("    如果对方早就说已经进组织了, 多半是表里的用户名填错 —— 让他确认一下。")
        log(f"    命令: {rerun_hint}")

    if missing:
        names = "、".join(
            f"{out.record.label}({out.record.login})" for out in missing[:20])
        log("")
        log(f"== 用户名不存在, 什么也做不了 ({len(missing)} 人) ==")
        log(f"    {clip(names, 140)}" + (" …" if len(missing) > 20 else ""))
        log("    这几行填的用户名在 GitHub 上查不到, 邀请永远发不成功。")
        log("    找本人核对拼写(常见是相邻字母打反), 改表格后重跑。")

    if needs_name:
        names = "、".join(f"{out.record.label}({out.record.email or '—'})"
                          for out in needs_name[:20])
        log("")
        log(f"== 待补用户名: 只有邮箱, 加不了 team ({len(needs_name)} 人) ==")
        log(f"    {clip(names, 140)}" + (" …" if len(needs_name) > 20 else ""))


def print_status_report(gh: GitHub, org: str) -> None:
    """--status: 只读地看一眼组织当前状态, 不需要表格。"""
    log(f"== 组织 {org} 的当前状态 ==")
    try:
        members = gh.org_members(org)
        pending_emails, pending_logins = gh.org_pending(org)
        failed = gh.failed_invitations(org)
    except ApiError as exc:
        die(f"读组织数据失败: {exc.message}\n"
            "检查: 组织名对不对、token 有没有过期、有没有该组织的 owner 权限。", 3)

    log(f"成员 {len(members)} 人 | 待处理邀请 {len(pending_emails) + len(pending_logins)} 个 | "
        f"失败邀请 {len(failed)} 个")

    if failed:
        log("")
        log("-- 失败的邀请(含历史遗留) --")
        rows = []
        for item in failed:
            who = str(item.get("email") or item.get("login") or "(未知)")
            reason = str(item.get("failed_reason") or "未给出原因")
            if "expired" in reason.lower():
                remedy = "组织邀请 7 天过期, 需要重新发一次。"
            else:
                remedy = "让对方把该邮箱加到 GitHub 账号并验证, 或改用用户名邀请。"
            rows.append([clip(who, 30), clip(reason, 44), clip(remedy, 40)])
        print_table(["对象", "失败原因", "处理"], rows, [30, 44, 40])

    if pending_emails:
        log("")
        log(f"-- 待处理邀请 {len(pending_emails)} 个(等对方接受, 7 天有效) --")
        for email in sorted(pending_emails)[:20]:
            log(f"    {email}")
        log("    提示: 按邮箱发的邀请在 API 里没有用户名(login 恒为 null), 只能看邮箱。")


def print_preflight(members: set[str], pending_emails: set[str], pending_logins: set[str],
                    team_members: set[str] | None, team_name: str) -> None:
    log("")
    log("== 预检 ==")
    log(f"组织已有成员 {len(members)} 人")
    log(f"已有待处理邀请 {len(pending_emails | pending_logins)} 个")
    if team_members is not None:
        log(f"team「{team_name}」现有成员 {len(team_members)} 人")
