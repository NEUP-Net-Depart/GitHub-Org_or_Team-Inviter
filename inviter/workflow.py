# -*- coding: utf-8 -*-
"""两档动作与管道步骤: 去重 → 第 1 档组织邀请 → 第 2 档 team。

这一层不碰 argparse 的定义, 也不碰打印与落盘 —— 只负责「对谁做什么、得到什么状态」,
所以 `_test/run_path_tests.py` 能用一个假客户端直接驱动它。
"""
from __future__ import annotations

import argparse
import time
import urllib.parse
from typing import Any, Sequence

from .console import clip, die, log
from .github_api import ApiError, GitHub, describe_error
from .outcomes import (
    ORG_LABEL,
    ORG_OK,
    ORG_SKIP,
    S_ALREADY_INVITED,
    S_ALREADY_MEMBER,
    S_DUPLICATE,
    S_FAILED,
    S_INVITED,
    S_NO_CONTACT,
    S_PLANNED,
    S_RESUMED,
    S_UNKNOWN,
    T_ADDED,
    T_ALREADY,
    T_FAILED,
    T_NEEDS_USERNAME,
    T_PENDING_ACCEPT,
    T_USERNAME_MISSING,
    Outcome,
    parse_conflict_message,
)
from .records import Record


def select_team(gh: GitHub, org: str, wanted: str) -> dict:
    """按 slug 优先、其次按名称精确匹配。找不到就把现有 team 列出来。"""
    key = wanted.strip().lower()
    teams = gh.org_teams(org)
    if not teams:
        die(f"组织 {org} 里没读到任何 team —— token 权限不足? "
            f"(需要能读该组织的 team, 组织 owner 权限最稳)", 3)
    for item in teams:
        if str(item.get("slug") or "").lower() == key:
            return item
    for item in teams:
        if str(item.get("name") or "").strip().lower() == key:
            return item
    listing = "、".join(f"{t.get('name')}({t.get('slug')})" for t in teams)
    die(f"找不到 team: {wanted}\n现有 team: {listing}")


def deduplicate(records: Sequence[Record]) -> tuple[list[Record], list[Record], list[Outcome]]:
    """去重 + 排除没有联系方式的行。

    返回 (去重后的记录, 被丢掉的重复行, 没有联系方式的行的 Outcome)。
    重复行要单独返回: --limit 是「表里的前 N 行」, 判断某一行在不在前 N 行时
    必须把它算进去, 否则 --limit 会静默跳过更多人, 而不是老老实实只处理前 N 个。
    """
    valid: list[Record] = []
    duplicates: list[Record] = []
    excluded: list[Outcome] = []
    seen: dict[str, int] = {}
    for record in records:
        if not record.email and not record.login:
            detail = "既没有邮箱也没有用户名"
            if record.notes:
                detail += f"({record.notes[0]})"
            excluded.append(Outcome(record, S_NO_CONTACT, detail))
            continue
        key = record.ident
        if key in seen:
            duplicates.append(record)
            excluded.append(Outcome(record, S_DUPLICATE, f"与第 {seen[key]} 行是同一个人"))
            continue
        seen[key] = record.row
        valid.append(record)
    return valid, duplicates, excluded


def seen_in_history(record: Record, history: dict[str, dict]) -> bool:
    """这个人是不是在以前某次运行里已经处理成功过(续跑的依据)。"""
    if not history:
        return False
    return bool((record.email and record.email.lower() in history)
                or (record.login and record.login.lower() in history))


def resolve_emails_to_logins(gh: GitHub, records: Sequence[Record]) -> int:
    """把只有邮箱的行反查成用户名, 就地写回。返回命中数。"""
    targets = [r for r in records if r.email and not r.login]
    if not targets:
        log("反查邮箱: 没有需要反查的行(都已经有用户名了)。")
        return 0
    log("")
    log(f"反查邮箱: 尝试把 {len(targets)} 个邮箱换成 GitHub 用户名 ...")
    if len(targets) > 30:
        log(f"  注意: 搜索接口限速 30 次/分钟, {len(targets)} 条约需 {len(targets) * 2 // 60} 分钟。")
    found = 0
    for record in targets:
        login = gh.search_login_by_email(record.email)
        if login:
            record.login = login
            record.notes.append("由邮箱反查得到用户名")
            found += 1
            log(f"  {record.email} -> {login}")
        time.sleep(2)  # 搜索接口限速 30 次/分钟
    log(f"  反查到 {found} / {len(targets)} 个。"
        + ("" if found else "(多数人没在 GitHub 上公开邮箱, 反查不到是正常的)"))
    return found


def process_org_invites(gh: GitHub | None, args: argparse.Namespace, todo: Sequence[Record],
                        members: set[str], pending_emails: set[str], pending_logins: set[str],
                        history: dict[str, dict], id_cache: dict[str, int | None],
                        mode: str) -> list[Outcome]:
    """第 1 档: 组织邀请。

    mode: "execute" 真发; "plan" 预检过的 dry-run; "unknown" 没预检, 状态不明。
    """
    outcomes: list[Outcome] = []
    total = len(todo)
    for index, record in enumerate(todo, start=1):
        outcome = Outcome(record)
        key_email = record.email.lower()
        key_login = record.login.lower()

        if record.login and key_login in members:
            outcome.org_status, outcome.org_detail = S_ALREADY_MEMBER, "在组织成员列表里"
        elif record.email and key_email in pending_emails:
            outcome.org_status, outcome.org_detail = S_ALREADY_INVITED, "该邮箱已有待处理邀请"
        elif record.login and key_login in pending_logins:
            outcome.org_status, outcome.org_detail = S_ALREADY_INVITED, "该用户名已有待处理邀请"
        elif seen_in_history(record, history):
            outcome.org_status = S_RESUMED
            outcome.org_detail = "上次已成功处理过"
        elif mode == "unknown":
            outcome.org_status = S_UNKNOWN
            outcome.org_detail = "没有预检, 不知道对方现在在不在组织里"
        elif mode == "plan":
            outcome.org_status = S_PLANNED
            outcome.org_detail = "dry-run 计划邀请"
        else:
            assert gh is not None
            body: dict[str, Any] = {"role": args.role}
            used_login = ""
            if record.login and not args.prefer_email:
                invitee = gh.user_id(record.login, id_cache)
                if invitee:
                    body["invitee_id"] = invitee
                    used_login = record.login
                elif record.email:
                    body["email"] = record.email
            if "invitee_id" not in body and "email" not in body:
                if record.email:
                    body["email"] = record.email
                else:
                    outcome.org_status = S_FAILED
                    outcome.org_detail = f"GitHub 上找不到用户名 {record.login}, 这一行又没有邮箱"
                    outcomes.append(outcome)
                    log(f"[{index}/{total}] ✗ {record.label} -> 找不到该用户名, 且没有邮箱")
                    continue

            try:
                status, payload = gh.request("POST", f"/orgs/{args.org}/invitations", body)
            except ApiError as exc:
                outcome.org_status, outcome.org_detail = S_FAILED, exc.message
                outcomes.append(outcome)
                log(f"[{index}/{total}] ✗ {record.label} -> {exc.message}")
                continue

            outcome.executed = True
            if 200 <= status < 300:
                outcome.org_status = S_INVITED
                if used_login:
                    outcome.org_detail = f"已按用户名邀请({used_login})"
                else:
                    outcome.org_detail = "已按邮箱邀请"
                    outcome.org_detail += "; ⚠ 该邮箱若不是对方已验证的邮箱, 对方点不动邀请"
            elif status == 422:
                message = describe_error(status, payload)
                verdict = parse_conflict_message(message)
                if verdict == S_ALREADY_MEMBER:
                    outcome.org_status, outcome.org_detail = S_ALREADY_MEMBER, "接口明确回复已是成员"
                elif verdict == S_ALREADY_INVITED:
                    outcome.org_status, outcome.org_detail = S_ALREADY_INVITED, "接口明确回复已有邀请"
                else:
                    outcome.org_status = S_FAILED
                    outcome.org_detail = f"422 校验失败: {clip(message, 70)}"
            elif status == 404:
                outcome.org_status = S_FAILED
                outcome.org_detail = "404: 组织名写错, 或 token 不是这个组织的 owner"
            elif status == 403:
                outcome.org_status = S_FAILED
                outcome.org_detail = ("403 权限不足: 细粒度 token 要 Members 写权限且组织已批准; "
                                      "经典 token 要 admin:org")
            else:
                outcome.org_status = S_FAILED
                outcome.org_detail = f"{status}: {clip(describe_error(status, payload), 70)}"

            mark = ("✓" if outcome.org_status in ORG_OK
                    else ("=" if outcome.org_status in ORG_SKIP else "✗"))
            log(f"[{index}/{total}] {mark} {record.label} -> {ORG_LABEL[outcome.org_status]}"
                + (f"  ({outcome.org_detail})" if outcome.org_status != S_INVITED else ""))

        outcomes.append(outcome)
        if mode == "execute" and args.delay > 0 and index < total:
            time.sleep(args.delay)
    return outcomes


def process_team(gh: GitHub | None, args: argparse.Namespace, outcomes: Sequence[Outcome],
                 members: set[str], current_team: set[str], team_slug: str,
                 mode: str, username_exists: dict[str, bool] | None = None) -> None:
    """第 2 档: 把已确认在组织里的人加进 team。

    顺序严格是「先查后写」: 已在 team 里的绝不重设角色(那会把 maintainer 降级)。
    """
    if username_exists is None:
        username_exists = {}

    for outcome in outcomes:
        record = outcome.record
        login_key = record.login.lower()

        if login_key and login_key in current_team:
            outcome.team_status, outcome.team_detail = T_ALREADY, "已在 team 里, 未改动其角色"
            continue
        if not record.login:
            outcome.team_status = T_NEEDS_USERNAME
            outcome.team_detail = "只有邮箱 —— team 接口不认邮箱, 需要 GitHub 用户名"
            continue

        if login_key not in members:
            # 快照里没有他, 两种可能:
            #  * 本次才刚给他发出邀请 -> 他确实还没接受, 现在 PUT 必然失败, 留到下次;
            #  * 这个用户名在 GitHub 上压根不存在 -> 他永远接受不了邀请, 必须明说,
            #    否则「等对方接受」是一句永远等不到的建议(实测就是这么被误导的)。
            if login_key not in username_exists:
                assert gh is not None
                username_exists[login_key] = gh.user_exists(record.login)
            if not username_exists[login_key]:
                outcome.team_status = T_USERNAME_MISSING
                outcome.team_detail = (f"GitHub 上查不到 {record.login} 这个用户名"
                                       f"(多半是拼错了; 也可能是对方把资料设为私密)")
                continue
            outcome.team_status = T_PENDING_ACCEPT
            outcome.team_detail = "还没成为组织成员(邀请待接受)"
            continue

        if mode != "execute":
            outcome.team_status = T_ADDED
            outcome.team_detail = "dry-run 计划加入"
            continue

        assert gh is not None
        try:
            status, payload = gh.request(
                "PUT",
                f"/orgs/{args.org}/teams/{urllib.parse.quote(team_slug)}"
                f"/memberships/{urllib.parse.quote(record.login)}",
                {"role": args.team_role},
            )
        except ApiError as exc:
            outcome.team_status, outcome.team_detail = T_FAILED, exc.message
            continue
        outcome.executed = True
        if 200 <= status < 300:
            outcome.team_status = T_ADDED
            outcome.team_detail = f"角色 {args.team_role}"
        elif status == 404:
            outcome.team_status = T_FAILED
            outcome.team_detail = "404: 用户名或 team 不存在, 或 token 无权改这个 team"
        else:
            outcome.team_status = T_FAILED
            outcome.team_detail = f"{status}: {clip(describe_error(status, payload), 60)}"
