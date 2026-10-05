# -*- coding: utf-8 -*-
"""命令行入口: 参数表 + 主流程组装。

这一层是**唯一**把各模块串起来的地方。别的模块互相之间不 import 它, 所以这里
可以放心地认识所有人。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

from .console import die, log, timestamp
from .constants import APP_NAME, DEFAULT_API, DEFAULT_DELAY, PROJECT_ROOT
from .github_api import ApiError, GitHub
from .input_source import InputSource, resolve_input
from .outcomes import (
    S_DUPLICATE,
    S_INVITED,
    S_NO_CONTACT,
    S_UNPROCESSED,
    T_FAILED,
    T_NEEDS_USERNAME,
    T_PENDING_ACCEPT,
    T_USERNAME_MISSING,
    TEAM_LABEL,
    Outcome,
)
from .report import (
    print_issues,
    print_preflight,
    print_results,
    print_retry_list,
    print_status_report,
    print_summary,
)
from .storage import load_history, write_outputs
from .tokens import resolve_token
from .workflow import (
    deduplicate,
    process_org_invites,
    process_team,
    resolve_emails_to_logins,
    select_team,
)


def default_out_dir() -> Path:
    """没给 --out-dir 时的默认落点: **仓库根**旁边的 output/。

    锚在 PROJECT_ROOT 而不是 `Path(__file__).parent`: 后者在拆模块后会变成
    `inviter/output/`, 于是结果文件会跑到包里去。
    """
    return PROJECT_ROOT / "output"


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """把 `--cols -,email,username` 这种写法接住。

    argparse 碰到以 '-' 开头的值会以为那是另一个选项, 于是报
    "expected one argument"。但「跳过第一列」的写法天生就长这样, 所以先把
    `--cols 值` 合并成 `--cols=值` —— 两种写法完全等价。

    (这跟输入是 csv 还是 xlsx 无关, 只要用得上 --cols 就需要它。)
    """
    out: list[str] = []
    index = 0
    items = list(argv)
    while index < len(items):
        item = items[index]
        if item == "--cols" and index + 1 < len(items):
            out.append(f"--cols={items[index + 1]}")
            index += 2
            continue
        out.append(item)
        index += 1
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="批量邀请人加入 GitHub 组织, 可选顺手加入指定 Team(默认 dry-run, 不发任何东西)",
        epilog=(
            "输入: 一个表格文件(.xlsx / .csv)\n"
            "  python github_inviter.py --org my-org \"收集表.xlsx\"\n"
            "\n"
            "表里至少有「邮箱」或「GitHub用户名」一列; 列顺序随便, 靠表头名字认:\n"
            "  姓名 / 邮箱 / GitHub用户名\n"
            "示例文件见 docs/examples/\n"
            "\n"
            "表头名字认不出来时, 脚本会列出前几行、问一句数据从第几行开始;\n"
            "也可以用参数直接说清楚:\n"
            "  --cols \"-,username,email,name\"  按位置指定列(位置从 1 开始, 不用的写 -)\n"
            "  --skip-rows 1                    跳过第 1 行(比如认不出的表头)\n"
            "  --no-header                      表里没有表头, 第一行就是数据\n"
        ),
    )
    parser.add_argument("targets", nargs="*", metavar="表格",
                        help="表格文件路径(.xlsx / .csv); 可选再跟一个用户名/邮箱临时补一个")
    parser.add_argument("--org", required=True, help="组织名(slug), 例如 my-org")

    team = parser.add_argument_group("team(可选)")
    team.add_argument("--team", default="", help='team 名称或 slug, 例如 "NEUP 2026"')
    team.add_argument("--team-role", default="member", choices=["member", "maintainer"],
                      help="新增 team 成员的角色, 默认 member(不会改动已有成员的角色)")

    table = parser.add_argument_group("表格解析")
    table.add_argument("--cols", default="",
                       help='按位置指定列, 例如 "name,email,username"; 不想用的列写 "-"')
    table.add_argument("--email-col", default="", help="手动指定邮箱列的列名或列号")
    table.add_argument("--username-col", default="", help="手动指定用户名列的列名或列号")
    table.add_argument("--sheet", default="", help="xlsx 的工作表名, 默认第一个")
    table.add_argument("--no-header", action="store_true",
                       help="表里没有表头(第一行就是数据): 不做表头识别, 按第1列用户名、第2列邮箱")
    table.add_argument("--skip-rows", type=int, default=0, metavar="N",
                       help="跳过表格开头的 N 行再解析(表头认不出时用 --skip-rows 1 跳掉它)")

    run = parser.add_argument_group("执行")
    run.add_argument("--execute", action="store_true",
                     help="真正发送邀请。不加这个参数就是 dry-run, 不会发出任何邀请")
    run.add_argument("--limit", type=int, default=0,
                     help="本次最多处理多少条(0=不限), 便于小批量试跑")
    run.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                     help=f"每条之间的间隔秒数, 默认 {DEFAULT_DELAY}(别调太小, 会触发限流)")
    run.add_argument("--role", default="direct_member",
                     choices=["direct_member", "admin", "billing_manager"],
                     help="组织邀请的角色, 默认 direct_member")
    run.add_argument("--prefer-email", action="store_true",
                     help="强制只按邮箱邀请(默认是: 有用户名就优先按用户名邀请)")
    run.add_argument("--resolve-emails", action="store_true",
                     help="先用搜索接口把邮箱反查成用户名(命中就能改走用户名邀请, 更可靠)")
    run.add_argument("--no-preflight", action="store_true",
                     help="跳过预检(也不用 token), 只看表格解析结果")
    run.add_argument("--no-resume", action="store_true",
                     help="忽略历史结果重新处理全部(会重复发送, 慎用)")
    run.add_argument("--status", action="store_true",
                     help="只读: 看组织当前成员/待处理邀请/失败邀请, 不需要表格, 不改任何东西")

    other = parser.add_argument_group("其它")
    other.add_argument("--out-dir", default="", help="结果输出目录, 默认脚本旁的 output/")
    other.add_argument("--token", default="", help="PAT; 默认读 token.txt 或环境变量")
    other.add_argument("--api-url", default=DEFAULT_API, help="GitHub API 地址")
    other.add_argument("--verbose", action="store_true", help="输出更多细节")
    raw = list(sys.argv[1:] if argv is None else argv)
    return parser.parse_args(normalize_argv(raw))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    started = time.time()
    args = parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir()

    # ---- 只读状态查询 ----
    if args.status:
        if args.targets:
            die("--status 是只读查询, 不接受表格或用户名参数。\n"
                f"用法: python {APP_NAME}.py --org {args.org} --status")
        token = resolve_token(args, required=True)
        print_status_report(GitHub(token, args.api_url), args.org)
        return 0

    if args.limit < 0:
        die("--limit 不能是负数。")
    if args.delay < 0:
        die("--delay 不能是负数。")

    # ---- 输入 ----
    source = resolve_input(args)
    log("")
    log(f"输入来源: {source.origin}")
    if source.header:
        log(f"表头: {' | '.join(str(c) for c in source.header)}")
    log(f"列映射: 邮箱=<{_name_of(source.header, source.columns.get('email'))}>  "
        f"用户名=<{_name_of(source.header, source.columns.get('username'))}>  "
        f"姓名=<{_name_of(source.header, source.columns.get('name'))}>")

    valid, duplicates, excluded = deduplicate(source.records)
    blank = max(0, source.data_rows - len(source.records))
    log(f"解析到 {len(source.records)} 行数据"
        + (f"(另有 {blank} 行空行/注释行已忽略)" if blank else "")
        + f", 排除 {len(excluded)} 行, 待处理 {len(valid)} 条。")
    for out in excluded:
        log(f"  - 排除第 {out.record.row} 行: {out.org_detail}")
    warned = 0
    for record in source.records:
        for note in record.notes:
            if note:
                warned += 1
                log(f"  ! 第 {record.row} 行: {note}")
    if warned:
        log(f"  ({warned} 条提醒, 不影响处理)")

    if not valid and not args.no_preflight:
        die("没有可处理的记录。")

    # ---- token / 客户端 ----
    needs_api = not args.no_preflight
    mode = "unknown" if args.no_preflight else ("execute" if args.execute else "plan")
    token = resolve_token(args, required=needs_api)
    gh = GitHub(token, args.api_url) if token else None

    if needs_api and gh is None:
        die("预检需要 token。要么配好 token, 要么加 --no-preflight 只看解析结果。")

    log("")
    if mode == "execute":
        log("!! 已开启 --execute, 会真正发出邀请 !!")
    elif mode == "plan":
        log("dry-run: 不会发出任何邀请(加 --execute 才真正发送)")
    else:
        log("未预检模式: 只解析表格, 不会发任何请求, 也不知道谁已经在组织里")
        if args.execute:
            # 这个组合很容易被误会: 用户以为"加了 --execute 就发出去了", 其实
            # --no-preflight 优先, 一封都不会发。所以要在这里、在他还看得见的时候说破。
            log("!! 注意: 你同时加了 --execute 和 --no-preflight —— --no-preflight 优先, "
                "本次一封都不会发。要真发请去掉 --no-preflight。")

    # ---- 预检 ----
    members: set[str] = set()
    pending_emails: set[str] = set()
    pending_logins: set[str] = set()
    current_team: set[str] | None = None
    team: dict = {}
    if needs_api:
        assert gh is not None
        if args.verbose:
            log("")
            log("预检: 读取组织成员与待处理邀请 ...")
        try:
            members = gh.org_members(args.org)
            pending_emails, pending_logins = gh.org_pending(args.org)
        except ApiError as exc:
            die(f"预检失败: {exc.message}\n"
                f"先确认组织名, 以及 token 权限(细粒度要 Members 写权限且组织已批准; "
                f"经典 token 要 admin:org)。\n"
                f"如果只是想看看表格解析结果, 加 --no-preflight。", 3)

        if args.team:
            team = select_team(gh, args.org, args.team)
            current_team = gh.team_members(args.org, str(team.get("slug")))
        print_preflight(members, pending_emails, pending_logins, current_team,
                        str(team.get("name") or args.team) if args.team else "")

    # ---- 邮箱反查用户名(可选) ----
    if args.resolve_emails and needs_api:
        assert gh is not None
        resolve_emails_to_logins(gh, valid)

    # ---- 续跑历史 ----
    history: dict[str, dict] = {}
    if not args.no_resume:
        history = load_history(out_dir)
        if history:
            log("")
            log(f"续跑: 读到 {len(history)} 条历史记录, 上次已成功的人会自动跳过"
                f"(要全部重跑加 --no-resume)")

    # ---- 本次范围: --limit 是「表里的前 N 行」 ----
    #
    # 判断某一行在不在前 N 行时, 重复行和没有联系方式的空行都要算进去, 否则
    # --limit 会静默跳过更多人, 而不是老老实实只处理表里前 N 行。
    limit = args.limit
    if limit:
        scope_duplicates = [out for out in excluded
                            if out.org_status == S_DUPLICATE and out.record.row <= limit]
        scope_no_contact = [out for out in excluded
                            if out.org_status == S_NO_CONTACT and out.record.row <= limit]
        in_scope = [r for r in valid if r.row <= limit]
        skipped_by_limit = [Outcome(record, S_UNPROCESSED,
                                    f"--limit {limit}, 本次没轮到, 下次不带 --limit 会处理")
                            for record in valid if record.row > limit]
        log("")
        log(f"--limit {limit}: 本次只看表里第 1~{limit} 行 —— 待处理 {len(in_scope)} 条, "
            f"留到下次 {len(skipped_by_limit)} 条。")
    else:
        scope_duplicates = [out for out in excluded if out.org_status == S_DUPLICATE]
        scope_no_contact = [out for out in excluded if out.org_status == S_NO_CONTACT]
        in_scope = list(valid)
        skipped_by_limit = []

    # ---- 第 1 档: 组织邀请 ----
    stamp = timestamp()
    log("")
    log(f"== 组织邀请: {args.org}(角色 {args.role}) ==")
    org_outcomes = process_org_invites(gh, args, in_scope, members, pending_emails,
                                       pending_logins, history, {}, mode)

    # ---- 第 2 档: team ----
    if args.team:
        if not needs_api:
            log("")
            log("== Team 加入: 已跳过(未预检模式不知道谁在组织里) ==")
        else:
            slug = str(team.get("slug"))
            log("")
            log(f"== Team 加入: {team.get('name')}(角色 {args.team_role}) ==")
            process_team(gh, args, org_outcomes, members, current_team or set(), slug, mode, {})
            for out in org_outcomes:
                if out.team_status == T_USERNAME_MISSING:
                    log(f"  ✗ {out.record.label}: {TEAM_LABEL[out.team_status]} —— {out.team_detail}")
                elif out.team_status == T_FAILED:
                    log(f"  ✗ {out.record.label}: {TEAM_LABEL[out.team_status]} —— {out.team_detail}")
                elif out.team_status in (T_PENDING_ACCEPT, T_NEEDS_USERNAME):
                    log(f"  · {out.record.label}: {TEAM_LABEL[out.team_status]}")

    outcomes = org_outcomes + scope_duplicates + scope_no_contact + skipped_by_limit

    # ---- 打印 ----
    print_results(outcomes, bool(args.team))
    print_summary(outcomes, bool(args.team), args, time.time() - started, mode)
    print_issues(outcomes)
    if args.team and needs_api:
        print_retry_list(outcomes, _rerun_hint(args, source))

    # ---- 落盘 ----
    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_outputs(out_dir, stamp, outcomes, execute=(mode == "execute"))
    log("")
    if mode != "execute":
        log("[DRY-RUN] 没有发出任何邀请。")
        log(f"计划已写入: {written[0]}")
        # 已经加过 --execute 的人不用再被劝一次(那种情况下它被 --no-preflight 盖掉了,
        # 上面已经提示过)。只有真的没加, 才提示下一步。
        if not args.execute:
            log("确认无误后加 --execute 真正发送(建议先加 --limit 3 小批量试跑)。")
    else:
        log(f"结果 CSV : {written[0]}")
        log(f"结果 JSONL: {written[1]}")
        log("重跑同一条命令会自动跳过这次已经成功的人(想全部重发加 --no-resume)。")
        if any(out.org_status == S_INVITED and out.record.email and not out.record.login
               for out in outcomes):
            log("提示: 只有邮箱的那些行, 若对方没把该邮箱验证到自己的 GitHub 账号上, "
                "他点不动邀请链接。")
            log(f"      过几分钟对一遍: python {APP_NAME}.py --org {args.org} --status")

    failures = sum(1 for out in outcomes if out.has_failure)
    return 1 if failures else 0


def _rerun_hint(args: argparse.Namespace, source: InputSource) -> str:
    if source.table_path is not None:
        target = f'"{source.table_path.name}"'
    else:
        target = " ".join(str(t) for t in (args.targets or []))
    return (f"python {APP_NAME}.py --org {args.org} {target} "
            f'--team "{args.team}" --execute')


def _name_of(header: Sequence[str], index: int | None) -> str:
    if index is None:
        return "(未使用)"
    if index < len(header):
        return str(header[index])
    return f"第 {index + 1} 列"
