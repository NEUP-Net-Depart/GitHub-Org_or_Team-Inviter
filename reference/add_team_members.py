#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把组织成员批量加入指定的 GitHub Team。

接口: PUT /orgs/{org}/teams/{team_slug}/memberships/{username}
      body: {"role": "member"}
      调用者需为组织 owner 或该 team 的 maintainer。

依赖同目录的 invite_org_members.py —— 复用它的 HTTP 客户端(含限流重试)、
token 解析、表格读取等, 避免两份实现走偏。

默认 dry-run, 加 --execute 才真正执行。

安全设计:
  * 已经在 team 里的人一律跳过, 绝不重设角色 —— 否则会把 maintainer 降级成 member
  * 自动检测同系列的往届 team(如 NEUP 2025/2021/2018/2017), 报告会误伤多少人
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from invite_org_members import (
        ApiError,
        GitHub,
        apply_email_resolution,
        die,
        load_records,
        log,
        now_iso,
        resolve_token,
    )
except ImportError:  # pragma: no cover
    print("错误: 找不到 invite_org_members.py。本脚本依赖它, 请和它放在同一目录。",
          file=sys.stderr)
    raise SystemExit(2)

YEAR_TEAM = re.compile(r"^(.*?)\s*(\d{4})$")


# ---------------------------------------------------------------------------
# team 解析
# ---------------------------------------------------------------------------

def find_team(teams: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """按 slug 优先、其次按名称精确匹配。"""
    key = wanted.strip().lower()
    for team in teams:
        if str(team.get("slug") or "").lower() == key:
            return team
    for team in teams:
        if str(team.get("name") or "").strip().lower() == key:
            return team
    names = ", ".join(f"{t.get('name')}({t.get('slug')})" for t in teams)
    die(f"找不到 team: {wanted}\n现有 team: {names}")


def sibling_year_teams(teams: list[dict[str, Any]], target: dict[str, Any]) -> list[dict[str, Any]]:
    """找出同系列的年份 team, 例如目标是 NEUP 2026 时返回 NEUP 2025/2021/2018/2017。"""
    matched = YEAR_TEAM.match(str(target.get("name") or "").strip())
    if not matched:
        return []
    prefix = matched.group(1).strip().lower()
    out = []
    for team in teams:
        if str(team.get("slug")) == str(target.get("slug")):
            continue
        other = YEAR_TEAM.match(str(team.get("name") or "").strip())
        if other and other.group(1).strip().lower() == prefix:
            out.append(team)
    return out


def team_members(gh: GitHub, org: str, slug: str) -> dict[str, str]:
    """返回 {login小写: 实际login}。"""
    out: dict[str, str] = {}
    for item in gh.get_paged(f"/orgs/{org}/teams/{slug}/members"):
        login = str(item.get("login") or "")
        if login:
            out[login.lower()] = login
    return out


# ---------------------------------------------------------------------------
# 逐行状态报告
# ---------------------------------------------------------------------------

def display_width(text: str) -> int:
    """中文按 2 列宽计算, 好让表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def fetch_invitation_state(gh: GitHub, org: str) -> tuple[dict, dict]:
    """返回 (待接受邀请, 失败邀请), 都以邮箱小写为键。这两个接口都带 email 字段。"""
    pending: dict[str, dict] = {}
    failed: dict[str, dict] = {}
    for inv in gh.get_paged(f"/orgs/{org}/invitations"):
        email = str(inv.get("email") or "").lower()
        if email:
            pending[email] = inv
    for inv in gh.get_paged(f"/orgs/{org}/failed_invitations"):
        email = str(inv.get("email") or "").lower()
        if email:
            failed[email] = inv
    return pending, failed


def load_invite_history(out_dir: Path) -> dict[str, dict]:
    """读邀请脚本留下的 results-*.jsonl, 按邮箱索引历史结论。"""
    history: dict[str, dict] = {}
    for path in sorted(out_dir.glob("results-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            email = str(item.get("email") or "").lower()
            if email:
                history[email] = item
    return history


def describe_person(rec, org_map, team_map, pending, failed, history) -> tuple[str, str]:
    """判断一个人当前的组织状态和 team 状态。"""
    email = (rec.email or "").lower()
    login = (rec.login or "").lower()

    if login and login in org_map:
        org_state = "✅ 已是组织成员"
    elif email and email in history and history[email].get("status") == "already_member":
        org_state = "✅ 已是组织成员"
    elif email and email in pending:
        org_state = "⏳ 邀请已发出, 待接受"
    elif email and email in failed:
        reason = str(failed[email].get("failed_reason") or "")
        if "expired" in reason.lower():
            org_state = "❌ 邀请已过期(7天未接受)"
        else:
            org_state = f"❌ 邀请失败: {reason[:40]}"
    elif email and email in history:
        status = str(history[email].get("status") or "")
        if status == "invited":
            # 发出过邀请, 现在既不在待处理、也不在失败清单 —— 对方已经接受了。
            # (还挂着的话上面 pending 分支就拦住了)
            org_state = "✅ 已加入组织(邀请已被接受)"
        elif status == "failed":
            org_state = "❌ 邀请失败"
        else:
            org_state = f"❓ {status or '未知'}"
    elif login:
        org_state = "❌ 还不在组织里"
    else:
        org_state = "❓ 无法判断(邮箱未反查到账号)"

    if login and login in team_map:
        team_state = "✅ 已在 team"
    elif login:
        team_state = "❌ 未加入 team"
    else:
        team_state = "❓ 未知(没有用户名)"

    return org_state, team_state


def print_status_report(records, org_map, team_map, pending, failed, history,
                        team_name: str, actions: dict[str, str]) -> None:
    log(f"\n=== 逐行状态 ({len(records)} 行) ===")
    header = ["行", "姓名", "邮箱", "用户名", "组织状态", "team 状态", "本次动作"]
    rows = []
    for rec in records:
        org_state, team_state = describe_person(
            rec, org_map, team_map, pending, failed, history)
        login_display = rec.login or "—"
        action = actions.get((rec.login or "").lower(), "")
        rows.append([
            str(rec.row), rec.name or "—", rec.email or "—", login_display,
            org_state, team_state, action,
        ])

    widths = []
    for index, title in enumerate(header):
        widest = display_width(title)
        for row in rows:
            widest = max(widest, display_width(row[index]))
        widths.append(min(widest, 42))

    def line(cells):
        return "  ".join(pad(cell, widths[i]) for i, cell in enumerate(cells))

    log(line(header))
    log("  ".join("-" * w for w in widths))
    for row in rows:
        log(line(row))

    ok_rows = [r for r in rows if "✅" in r[4]]
    not_in = [r for r in rows if "❌" in r[4] and "还不在" in r[4]]
    failed_rows = [r for r in rows if "❌" in r[4] and ("失败" in r[4] or "过期" in r[4])]
    waiting = [r for r in rows if "⏳" in r[4]]
    unknown = [r for r in rows if "❓" in r[4]]
    log(f"\n小结: 已是组织成员 {len(ok_rows)} 人 | 邀请待接受 {len(waiting)} 人"
        f" | 邀请失败/过期 {len(failed_rows)} 人 | 还不在组织 {len(not_in)} 人"
        f" | 无法判断 {len(unknown)} 人")
    if unknown:
        log("  无法判断的这几行, 邮箱没能在 GitHub 上反查到账号, 只能请本人确认用户名。")


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把组织成员批量加入 GitHub Team(默认 dry-run)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--org", required=True, help="组织名(slug)")
    parser.add_argument("--team", required=True, help="team 名称或 slug, 例如 'NEUP 2026'")
    parser.add_argument("--execute", action="store_true", help="真正执行, 不加就是 dry-run")
    parser.add_argument("--role", default="member", choices=["member", "maintainer"],
                        help="新增成员的角色, 默认 member。注意: 不会改动已有成员的角色")
    parser.add_argument("--users", default="",
                        help="只加这些人: 逗号分隔的用户名, 或一个名单文件路径(txt/csv, 每行一个)")
    parser.add_argument("--from-table", default="", help="只加这张数据表里的人(按用户名, 邮箱会自动反查)")
    parser.add_argument("--exclude-teams", default="",
                        help="排除这些 team 的成员(逗号分隔名称或 slug), 例如 'NEUP 2025,NEUP 2021'")
    parser.add_argument("--limit", type=int, default=0, help="本次最多加多少人(0=不限)")
    parser.add_argument("--delay", type=float, default=1.0, help="每次调用间隔秒数")
    parser.add_argument("--no-year-check", action="store_true", help="跳过往届 team 误伤检测")
    parser.add_argument("--year-check", action="store_true",
                        help="强制开启往届检测(默认: 用 --from-table 时自动关闭, 表格即准绳)")
    parser.add_argument("--out-dir", default="", help="结果目录, 默认脚本旁的 output/")
    parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--token", default="")
    parser.add_argument("--sheet", default="", help="表格的 sheet 名")
    parser.add_argument("--email-col", default="")
    parser.add_argument("--username-col", default="")
    parser.add_argument("--cols", default="",
                        help='按位置规定每列是什么, 例如 "username,email" '
                             '= 第1列用户名、第2列邮箱(不想用的列写 "-")')
    return parser.parse_args(argv)


def split_names(value: str) -> list[str]:
    """拆出用户名。既支持 'a,b,c', 也支持直接给一个 txt/csv 文件路径。

    作为文件读取时, 每行取第一个逗号/空白分隔的字段, 跳过空行和 # 注释,
    所以 'login,邮箱,备注' 这种一行多列的名单也能直接用。
    """
    text = value or ""
    candidate = Path(text.strip().strip('"').strip("'"))
    if text.strip() and candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            die(f"读取名单文件失败: {candidate} ({exc})")
        log(f"从文件读取名单: {candidate}")
        out = []
        for raw in text.splitlines():
            line = raw.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            first = re.split(r"[,\t;]|\s{2,}", line)[0].strip().strip('"').strip("'")
            if first:
                out.append(first)
        return out
    return [x.strip() for x in re.split(r"[,\n]+", text) if x.strip()]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    args = parse_args(argv)
    token = resolve_token(args, required=True)
    gh = GitHub(token, args.api_url)

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 定位 team ----
    teams = gh.get_paged(f"/orgs/{args.org}/teams")
    if not isinstance(teams, list) or not teams:
        die(f"组织 {args.org} 里没有读到任何 team(token 权限不足?)")
    target = find_team(teams, args.team)
    log(f"目标 team: {target.get('name')}  (slug={target.get('slug')}, id={target.get('id')})")
    if args.role == "maintainer":
        log("  ⚠️  你选择了 maintainer 角色, 新成员将获得 team 管理权限")

    slug = str(target.get("slug"))
    current = team_members(gh, args.org, slug)
    log(f"team 现有成员: {len(current)} 人")

    # ---- 组织成员 ----
    org_map: dict[str, str] = {}
    for item in gh.get_paged(f"/orgs/{args.org}/members"):
        login = str(item.get("login") or "")
        if login:
            org_map[login.lower()] = login
    log(f"组织成员: {len(org_map)} 人")

    # ---- 候选池 ----
    pool = dict(org_map)
    if args.users:
        wanted = {u.lower() for u in split_names(args.users)}
        unknown = wanted - set(org_map)
        if unknown:
            log(f"  ⚠️  这些人不在组织里, 无法加入 team, 已忽略: {', '.join(sorted(unknown))}")
        pool = {k: v for k, v in pool.items() if k in wanted}
        log(f"按 --users 筛选后: {len(pool)} 人")

    table_records: list = []
    if args.from_table:
        table_args = argparse.Namespace(
            input=args.from_table, sheet=args.sheet, email_col=args.email_col,
            username_col=args.username_col, cols=args.cols,
        )
        records, _header, _cols, path, _total = load_records(table_args)
        table_records = records
        if any(r.email and not r.login for r in records):
            apply_email_resolution(gh, records)
        wanted = {r.login.lower() for r in records if r.login}
        log(f"表格 {path.name} 解析出 {len(wanted)} 个用户名")
        missing = wanted - set(org_map)
        if missing:
            log(f"  ⚠️  这些人还不在组织里(邀请可能还没接受), 无法加入 team: "
                f"{', '.join(sorted(missing))}")
        pool = {k: v for k, v in pool.items() if k in wanted}
        log(f"按 --from-table 筛选后: {len(pool)} 人")

    if args.exclude_teams:
        for name in split_names(args.exclude_teams):
            excluded_team = find_team(teams, name)
            members = team_members(gh, args.org, str(excluded_team.get("slug")))
            removed = set(pool) & set(members)
            pool = {k: v for k, v in pool.items() if k not in members}
            log(f"排除 {excluded_team.get('name')} 的 {len(members)} 人, 实际排除 {len(removed)} 人")

    # ---- 去掉已在 team 里的(绝不重设角色) ----
    already = [pool[k] for k in pool if k in current]
    todo_keys = [k for k in pool if k not in current]
    todo_keys.sort()
    log(f"\n候选 {len(pool)} 人, 其中 {len(already)} 人已在 team 里(跳过, 不改其角色), "
        f"待新增 {len(todo_keys)} 人")

    # ---- 往届 team 误伤检测(纯提示, 从不剔除任何人) ----
    if args.no_year_check:
        do_year_check = False
    elif args.year_check:
        do_year_check = True
    else:
        # 指定了表格就以表格为准, 不再拿往届 team 来"劝退"
        do_year_check = not bool(args.from_table)
    siblings = sibling_year_teams(teams, target) if do_year_check else []
    if siblings:
        log("\n检查同系列往届 team(防止把学长学姐拉进本届):")
        hit: dict[str, list[str]] = {}
        for team in siblings:
            members = team_members(gh, args.org, str(team.get("slug")))
            overlap = [k for k in todo_keys if k in members]
            if overlap:
                hit[str(team.get("name"))] = overlap
            log(f"  {team.get('name'):<12} {len(members):>4} 人, 与待新增名单重叠 {len(overlap)} 人")
        if hit:
            total = sum(len(v) for v in hit.values())
            log(f"\n  ⚠️  待新增的人里有 {total} 人次同时属于往届 team, 很可能不该拉进本届:")
            shown = 0
            for name, logins in hit.items():
                for login in sorted(logins):
                    if shown >= 15:
                        break
                    log(f"       {login}  (在 {name})")
                    shown += 1
            log("     如果要避开他们, 加上: --exclude-teams \""
                + ",".join(hit.keys()) + "\"")

    if args.limit and len(todo_keys) > args.limit:
        log(f"\n--limit {args.limit}: 本次只处理前 {args.limit} 人")
        todo_keys = todo_keys[: args.limit]

    def emit_status(actions: dict[str, str]) -> None:
        """打印逐行状态: 谁进来了、谁还在等、谁邀请失败了。"""
        if not table_records:
            return
        try:
            pending, failed = fetch_invitation_state(gh, args.org)
        except ApiError as exc:
            log(f"  (逐行状态跳过: 读取邀请列表失败 — {exc.message})")
            return
        history = load_invite_history(out_dir)
        print_status_report(table_records, org_map, current, pending, failed,
                            history, str(target.get("name")), actions)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if not args.execute:
        plan = out_dir / f"team-plan-{stamp}.csv"
        with plan.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["login", "action", "note"])
            for key in todo_keys:
                writer.writerow([pool[key], "will_add", ""])
            for login in sorted(already):
                writer.writerow([login, "already_in_team", "跳过, 不改角色"])
        log(f"\n[DRY-RUN] 没有改动任何 team 成员。")
        log(f"计划已写入: {plan}")
        log("确认后用 --execute 执行(建议先 --limit 3 试跑)。")
        emit_status({})
        return 0

    # ---- 执行 ----
    results = out_dir / f"team-results-{stamp}.csv"
    log(f"\n开始添加 (角色 {args.role}, 间隔 {args.delay}s) ...\n")
    counts: dict[str, int] = {}
    actions: dict[str, str] = {}
    with results.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["login", "status", "http_status", "detail", "time"])
        for index, key in enumerate(todo_keys, start=1):
            login = pool[key]
            status, code, detail = "error", 0, ""
            try:
                code, payload = gh.request(
                    "PUT", f"/orgs/{args.org}/teams/{slug}/memberships/{login}",
                    {"role": args.role},
                )
                if 200 <= code < 300:
                    status = "added"
                    detail = f"角色 {payload.get('role') if isinstance(payload, dict) else args.role}"
                elif code == 404:
                    status = "failed"
                    detail = "404: team/用户不存在, 或 token 无权修改该 team"
                elif code == 422:
                    status = "failed"
                    detail = str(payload.get("message") if isinstance(payload, dict) else payload)
                else:
                    status = "failed"
                    detail = str(payload.get("message") if isinstance(payload, dict) else payload)
            except ApiError as exc:
                status, code, detail = "error", exc.status, exc.message

            counts[status] = counts.get(status, 0) + 1
            actions[login.lower()] = "✅ 已加入" if status == "added" else f"❌ {status}"
            if status == "added":
                # 让结尾的状态表反映执行后的真实情况, 而不是执行前的快照
                current[login.lower()] = login
            mark = "✓" if status == "added" else "✗"
            log(f"[{index}/{len(todo_keys)}] {mark} {login} -> {status}"
                + (f"  ({detail})" if status != "added" else ""))
            writer.writerow([login, status, code, detail, now_iso()])
            handle.flush()
            if index < len(todo_keys) and args.delay > 0:
                time.sleep(args.delay)

    log("\n== 汇总 ==")
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        log(f"  {status}: {count}")
    log(f"已跳过 {len(already)} 位原有成员(未改动其角色)。")
    log(f"结果: {results}")
    emit_status(actions)
    log("\n可以在网页确认: https://github.com/orgs/"
        f"{args.org}/teams/{slug}/members")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
