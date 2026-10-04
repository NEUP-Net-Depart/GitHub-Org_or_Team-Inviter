#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用假客户端驱动真实的 process_org_invites / process_team。

目的: 把「邀请真的发出去(2xx)」这条路径验证掉 —— 真实实跑里拿不到样本
(试跑的人恰好都已是成员), 所以这里用一个假 GitHub 客户端喂各种响应,
检查**脚本真实发出的请求体**和**产出的 Outcome**。

不联网, 不发任何东西。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import github_inviter as g  # noqa: E402

PASS, FAIL = [], []


def report(title: str, problems: list[str]) -> None:
    (PASS if not problems else FAIL).append(title)
    print(f"[{'PASS' if not problems else 'FAIL'}] {title}")
    for problem in problems:
        print(f"         {problem}")


class FakeGitHub:
    """只实现 process_* 真正用到的那两个方法, 并记录每一次请求。"""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, Any]],
                 user_ids: dict[str, int | None] | None = None,
                 existing_users: tuple[str, ...] = ()):
        self.responses = responses
        self.user_ids = user_ids or {}
        self.existing_users = {u.lower() for u in existing_users}
        self.calls: list[dict[str, Any]] = []

    def user_id(self, login: str, cache: dict[str, int | None]) -> int | None:
        key = login.lower()
        if key in cache:
            return cache[key]
        value = self.user_ids.get(key)
        cache[key] = value
        self.calls.append({"kind": "user_id", "login": login, "result": value})
        return value

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        self.calls.append({"kind": "request", "method": method, "path": path, "body": body})
        for (m, prefix), response in self.responses.items():
            if m == method and path.startswith(prefix):
                return response
        return 404, {"message": "Not Found"}

    def user_exists(self, login: str) -> bool:
        """默认不存在 —— 只有显式列进 existing_users 的才算存在。

        默认「不存在」是故意的: 免得某个测试忘了建模存在性, 就悄悄走了
        「用户名不存在」那条分支还以为在测别的。
        """
        self.calls.append({"kind": "user_exists", "login": login})
        return login.lower() in self.existing_users

    # 辅助: 找出对某个路径的 POST 调用
    def posts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == "request" and c["method"] == "POST"]

    def puts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == "request" and c["method"] == "PUT"]


def make_args(**overrides: Any) -> argparse.Namespace:
    base = dict(org="test-org", role="direct_member", team_role="member",
                prefer_email=False, delay=0.0, limit=0, verbose=False)
    base.update(overrides)
    return argparse.Namespace(**base)


INVITE_PATH = "/orgs/test-org/invitations"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="inviter-201-"))
    try:
        # ---------- 1. 有用户名: 必须走 invitee_id ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {"id": 999, "login": "alice"})},
                        user_ids={"alice": 12345})
        record = g.Record(row=2, name="甲", email="alice@example.com", login="alice")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        if out.org_status != g.S_INVITED:
            problems.append(f"org_status={out.org_status!r}, 期望 {g.S_INVITED!r}")
        posts = gh.posts()
        if len(posts) != 1:
            problems.append(f"应有 1 次 POST, 实际 {len(posts)}")
        else:
            body = posts[0]["body"] or {}
            if body.get("invitee_id") != 12345:
                problems.append(f"没有用 invitee_id 邀请: {body}")
            if "email" in body:
                problems.append(f"有用户名时不该再带 email: {body}")
            if body.get("role") != "direct_member":
                problems.append(f"role 不对: {body}")
        if "已按用户名邀请(alice)" not in out.org_detail:
            problems.append(f"说明不对: {out.org_detail!r}")
        if not out.committed:
            problems.append("成功的行应该算 committed")
        report("有用户名 -> 走 invitee_id 邀请(2xx)", problems)

        # ---------- 2. 只有邮箱: 走 email, 并给出未验证提醒 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {"id": 1000})})
        record = g.Record(row=3, name="乙", email="bob@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        posts = gh.posts()
        if out.org_status != g.S_INVITED:
            problems.append(f"org_status={out.org_status!r}")
        if len(posts) != 1 or (posts[0]["body"] or {}).get("email") != "bob@example.com":
            problems.append(f"没有按邮箱邀请: {posts}")
        if "点不动" not in out.org_detail:
            problems.append(f"缺少邮箱未验证的提醒: {out.org_detail!r}")
        report("只有邮箱 -> 按邮箱邀请并提醒未验证", problems)

        # ---------- 3. 用户名解析不到但没邮箱 -> 失败, 且不发请求 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {})}, user_ids={"ghost": None})
        record = g.Record(row=4, name="丙", login="ghost")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        if out.org_status != g.S_FAILED:
            problems.append(f"org_status={out.org_status!r}, 期望 failed")
        if gh.posts():
            problems.append(f"不该发 POST: {gh.posts()}")
        if "找不到用户名" not in out.org_detail:
            problems.append(f"说明不对: {out.org_detail!r}")
        report("用户名不存在且无邮箱 -> 失败且不发请求", problems)

        # ---------- 4. 用户名解析不到但有邮箱 -> 退回邮箱邀请 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {})}, user_ids={"typo": None})
        record = g.Record(row=5, name="丁", email="ding@example.com", login="typo")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        posts = gh.posts()
        if out.org_status != g.S_INVITED:
            problems.append(f"org_status={out.org_status!r}")
        if len(posts) != 1 or (posts[0]["body"] or {}).get("email") != "ding@example.com":
            problems.append(f"没有退回邮箱邀请: {posts}")
        report("用户名拼错但有邮箱 -> 退回邮箱邀请", problems)

        # ---------- 5. 已存在于组织成员列表 -> 一个请求都不发 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {})})
        record = g.Record(row=6, name="戊", login="member1", email="m@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], {"member1"}, set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        if out.org_status != g.S_ALREADY_MEMBER:
            problems.append(f"org_status={out.org_status!r}")
        if gh.calls:
            problems.append(f"已是成员不该发任何请求: {gh.calls}")
        report("已在组织里 -> 零请求", problems)

        # ---------- 6. 已有待处理邀请(按邮箱) -> 零请求 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {})})
        record = g.Record(row=7, name="己", email="pending@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(),
                                         {"pending@example.com"}, set(), {}, {}, "execute")
        problems = []
        if outcomes[0].org_status != g.S_ALREADY_INVITED:
            problems.append(f"org_status={outcomes[0].org_status!r}")
        if gh.calls:
            problems.append(f"已有待处理邀请不该发请求: {gh.calls}")
        report("已有待处理邀请 -> 零请求", problems)

        # ---------- 7. 422 already a part of this organization ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (
            422, {"message": "Validation Failed",
                  "errors": [{"message": "A user with this email address is already "
                                         "a part of this organization"}]})})
        record = g.Record(row=8, name="庚", email="g@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        if out.org_status != g.S_ALREADY_MEMBER:
            problems.append(f"org_status={out.org_status!r}, 期望 already_member")
        if not out.committed:
            problems.append("已是成员应算 committed")
        report("422 已是成员 -> already_member", problems)

        # ---------- 8. 别的 422(不是已存在) -> 必须判失败 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (422, {"message": "Validation Failed",
                                                      "errors": [{"message":
                                                                  "Name already exists"}]})})
        record = g.Record(row=9, name="辛", email="x@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        if out.org_status != g.S_FAILED:
            problems.append(f"org_status={out.org_status!r}, 期望 failed(不能被当成已存在)")
        if out.committed:
            problems.append("失败的行不该算 committed")
        report("无关的 422 -> 判失败(不误判成已存在)", problems)

        # ---------- 9. 403 权限不足 ----------
        # 必须给邮箱: 否则会在「用户名解析不到且没邮箱」那道保护上提前返回, 根本发不出 POST
        gh = FakeGitHub({("POST", INVITE_PATH): (403, {"message": "Resource not accessible"})})
        record = g.Record(row=10, name="壬", login="someone", email="ren@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        out = outcomes[0]
        if out.org_status != g.S_FAILED or "403" not in out.org_detail:
            problems.append(f"403 应该判失败并说明: {out.org_status!r} / {out.org_detail!r}")
        if not gh.posts():
            problems.append("这一条本该真的发出 POST")
        report("403 -> 失败并给出权限提示", problems)

        # ---------- 10. 404 组织名/token ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (404, {"message": "Not Found"})})
        record = g.Record(row=11, name="癸", login="someone2", email="gui@example.com")
        outcomes = g.process_org_invites(gh, make_args(), [record], set(), set(), set(),
                                         {}, {}, "execute")
        problems = []
        if "404" not in outcomes[0].org_detail:
            problems.append(f"404 说明不对: {outcomes[0].org_detail!r}")
        if not gh.posts():
            problems.append("这一条本该真的发出 POST")
        report("404 -> 提示组织名或权限", problems)

        # ---------- 11. dry-run 绝不能发请求 ----------
        gh = FakeGitHub({("POST", INVITE_PATH): (201, {})})
        records = [g.Record(row=2, name="甲", login="alice"),
                   g.Record(row=3, name="乙", email="b@example.com")]
        outcomes = g.process_org_invites(gh, make_args(), records, set(), set(), set(),
                                         {}, {}, "plan")
        problems = []
        if gh.calls:
            problems.append(f"dry-run 不该发任何请求: {gh.calls}")
        if any(o.org_status != g.S_PLANNED for o in outcomes):
            problems.append(f"dry-run 状态应为 planned: {[o.org_status for o in outcomes]}")
        report("dry-run -> 零请求且状态为 planned", problems)

        # ---------- 12. team: 已在 team -> 不动角色 ----------
        gh = FakeGitHub({})
        out = g.Outcome(g.Record(row=2, name="甲", login="alice"), g.S_ALREADY_MEMBER)
        g.process_team(gh, make_args(), [out], {"alice"}, {"alice"}, "team-slug", "execute")
        problems = []
        if out.team_status != g.T_ALREADY:
            problems.append(f"team_status={out.team_status!r}")
        if gh.calls:
            problems.append(f"已在 team 绝不该重设角色: {gh.calls}")
        report("team: 已在 team -> 零请求不降级", problems)

        # ---------- 13. team: 组织成员 + 有用户名 -> PUT ----------
        gh = FakeGitHub({("PUT", "/orgs/test-org/teams/team-slug/memberships/"):
                         (200, {"role": "member"})})
        out = g.Outcome(g.Record(row=3, name="乙", login="bob"), g.S_ALREADY_MEMBER)
        g.process_team(gh, make_args(), [out], {"bob"}, set(), "team-slug", "execute")
        problems = []
        puts = gh.puts()
        if out.team_status != g.T_ADDED:
            problems.append(f"team_status={out.team_status!r}")
        if len(puts) != 1:
            problems.append(f"应有 1 次 PUT: {puts}")
        elif (puts[0]["body"] or {}).get("role") != "member":
            problems.append(f"role 不对: {puts[0]['body']}")
        report("team: 组织成员 -> PUT 加入", problems)

        # ---------- 14. team: 不是组织成员 -> 不发请求, 记为待重跑 ----------
        gh = FakeGitHub({}, existing_users=("carol",))
        out = g.Outcome(g.Record(row=4, name="丙", login="carol"), g.S_INVITED)
        g.process_team(gh, make_args(), [out], set(), set(), "team-slug", "execute")
        problems = []
        if out.team_status != g.T_PENDING_ACCEPT:
            problems.append(f"team_status={out.team_status!r}")
        if gh.puts():
            problems.append(f"不该发 PUT: {gh.puts()}")
        if gh.posts():
            problems.append(f"不该发 POST: {gh.posts()}")
        report("team: 不是组织成员 -> 待重跑, 不发写请求", problems)

        # ---------- 15. team: 只有邮箱 -> 缺用户名 ----------
        gh = FakeGitHub({})
        out = g.Outcome(g.Record(row=5, name="丁", email="d@example.com"), g.S_INVITED)
        g.process_team(gh, make_args(), [out], set(), set(), "team-slug", "execute")
        problems = []
        if out.team_status != g.T_NEEDS_USERNAME:
            problems.append(f"team_status={out.team_status!r}")
        if gh.calls:
            problems.append(f"不该发请求: {gh.calls}")
        report("team: 只有邮箱 -> 缺用户名, 零请求", problems)

        # ---------- 15.1 用户名存在但还不是成员 -> 待重跑, 不发 PUT ----------
        # (曾经这里还有一次「活查成员关系」的兜底, 因为怀疑成员列表会滞后。
        #  但那个假设始终没有实测证据, 属于自加的多余复杂度, 已按用户判断砍掉。
        #  现在只信预检那一份成员快照。)
        gh = FakeGitHub({}, existing_users=("notyet",))
        out = g.Outcome(g.Record(row=8, name="辛", login="notyet"), g.S_RESUMED)
        g.process_team(gh, make_args(), [out], set(), set(), "team-slug", "execute", {})
        problems = []
        if out.team_status != g.T_PENDING_ACCEPT:
            problems.append(f"team_status={out.team_status!r}")
        if gh.puts():
            problems.append(f"还不是成员就不该发 PUT: {gh.puts()}")
        if [c for c in gh.calls if c.get("kind") == "request"]:
            problems.append(f"不该发任何 HTTP 请求(只该查一次用户名是否存在): {gh.calls}")
        report("team: 还不是成员 -> 待重跑, 零 PUT", problems)

        # ---------- 15.2 本次才刚发邀请的人 -> 直接待重跑 ----------
        gh = FakeGitHub({}, existing_users=("fresh",))
        out = g.Outcome(g.Record(row=9, name="壬", login="fresh"), g.S_INVITED)
        g.process_team(gh, make_args(), [out], set(), set(), "team-slug", "execute", {})
        problems = []
        if out.team_status != g.T_PENDING_ACCEPT:
            problems.append(f"team_status={out.team_status!r}")
        if [c for c in gh.calls if c.get("kind") == "request"]:
            problems.append(f"不该发任何 HTTP 请求: {gh.calls}")
        report("team: 本次刚发邀请 -> 直接待重跑", problems)

        # ---------- 15.3 同一个不存在的人只查一次(缓存生效) ----------
        gh = FakeGitHub({})
        outs = [g.Outcome(g.Record(row=i, name=f"人{i}", login="ghost-zz9"), g.S_INVITED)
                for i in (10, 11)]
        g.process_team(gh, make_args(), outs, set(), set(), "team-slug", "plan", {})
        lookups = [c for c in gh.calls if c.get("kind") == "user_exists"]
        problems = []
        if len(lookups) != 1:
            problems.append(f"同一个人应只查一次用户名是否存在, 实际 {len(lookups)} 次")
        if any(o.team_status != g.T_USERNAME_MISSING for o in outs):
            problems.append(f"两条都该判用户名不存在: {[o.team_status for o in outs]}")
        report("team: 同一人的存在性查询只发一次", problems)

        # ---------- 15.5 用户名在 GitHub 上不存在 -> 明说, 不给「等对方接受」的误导建议 ----------
        gh = FakeGitHub({})          # 任何 GET /users/... 都回 404
        out = g.Outcome(g.Record(row=12, name="癸", login="no-such-user-zz9",
                                 email="k@example.com"), g.S_RESUMED)
        g.process_team(gh, make_args(), [out], set(), set(), "team-slug", "execute", {})
        problems = []
        if out.team_status != g.T_USERNAME_MISSING:
            problems.append(f"team_status={out.team_status!r}, 期望 {g.T_USERNAME_MISSING!r}")
        if gh.puts():
            problems.append(f"用户名不存在就不该发 PUT: {gh.puts()}")
        if not out.has_failure:
            problems.append("用户名不存在应该算失败, 不能悄悄咽掉")
        if g.T_USERNAME_MISSING not in g.TEAM_LABEL or not g.REMEDY.get(g.T_USERNAME_MISSING):
            problems.append("缺标签或处理措施")
        report("team: 用户名不存在 -> 明确报错, 不给误导建议", problems)

        # ---------- 15.6 新状态要能在 CSV 里正确落盘 ----------
        bad = g.Outcome(g.Record(row=13, name="子", login="no-such-user-zz9"),
                        g.S_RESUMED, "上次已成功处理过",
                        g.T_USERNAME_MISSING, "GitHub 上没有这个用户名")
        bad_csv, _ = g.write_outputs(work, "missinguser", [bad], execute=True)
        with bad_csv.open(encoding="utf-8-sig", newline="") as handle:
            bad_row = list(csv.reader(handle))[1]
        problems = []
        if bad_row[6] != "用户名不存在":
            problems.append(f"CSV team 状态不对: {bad_row[6]!r}")
        if "查不到" not in bad_row[9]:
            problems.append(f"CSV 处理措施没说清: {bad_row[9]!r}")
        if "用户名" not in bad_row[8]:
            problems.append(f"CSV 失败原因没填: {bad_row[8]!r}")
        report("team: 用户名不存在 -> CSV 状态/原因/措施齐备", problems)

        # ---------- 16. team 失败不改动已落定的组织结果 ----------
        gh = FakeGitHub({("PUT", "/orgs/test-org/teams/team-slug/memberships/"):
                         (404, {"message": "Not Found"})})
        out = g.Outcome(g.Record(row=6, name="戊", login="erin"), g.S_INVITED,
                        "已按用户名邀请(erin)")
        g.process_team(gh, make_args(), [out], {"erin"}, set(), "team-slug", "execute")
        problems = []
        if out.team_status != g.T_FAILED:
            problems.append(f"team_status={out.team_status!r}")
        if out.org_status != g.S_INVITED or "erin" not in out.org_detail:
            problems.append(f"team 失败把组织结果冲掉了: {out.org_status!r} / {out.org_detail!r}")
        report("team 失败不冲掉组织邀请结果", problems)

        # ---------- 17. 真实 201 之后落盘: CSV 里有「已发出邀请」 ----------
        invited = g.Outcome(g.Record(row=2, name="甲", email="alice@example.com", login="alice"),
                            g.S_INVITED, "已按用户名邀请(alice)", g.T_ADDED, "角色 member")
        csv_path, jsonl_path = g.write_outputs(work, "201test", [invited], execute=True)
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            row = list(csv.reader(handle))[1]
        items = [json.loads(line) for line in
                 jsonl_path.read_text(encoding="utf-8").splitlines() if line]
        problems = []
        if "组织邀请已发出" not in row[4]:
            problems.append(f"CSV 状态不对: {row[4]!r}")
        if "无需处理" not in row[9]:
            problems.append(f"CSV 处理措施不对: {row[9]!r}")
        if items[0]["org_status"] != g.S_INVITED or not items[0]["committed"]:
            problems.append(f"JSONL 不对: {items[0]}")
        # 续跑必须能认出这个人, 重跑时跳过
        history = g.load_history(work)
        if "alice" not in history or "alice@example.com" not in history:
            problems.append("续跑索引不到刚邀请成功的人")
        report("201 之后落盘 + 续跑可识别", problems)

        print(f"\n===== 假客户端路径测试: 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
        for title in FAIL:
            print(f"  失败: {title}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
