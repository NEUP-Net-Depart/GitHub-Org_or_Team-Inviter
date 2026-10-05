# -*- coding: utf-8 -*-
"""结果模型与状态码。

`ORG_LABEL` / `TEAM_LABEL` / `REMEDY` 三张表**必须**和状态常量改在一起, 所以
它们住在同一个模块里 —— 拆开就会重演「状态值撞车、REMEDY 被静默覆盖」那个 bug。
"""
from __future__ import annotations

from dataclasses import dataclass

from .records import Record

# 组织邀请这一档
S_PLANNED = "planned"            # 预检过的 dry-run: 确认可以发, 但这次没发
S_UNKNOWN = "unknown"            # --no-preflight: 连"他是不是已经在了"都不知道
S_INVITED = "invited"
S_ALREADY_MEMBER = "already_member"
S_ALREADY_INVITED = "already_invited"
S_NO_CONTACT = "skipped_no_contact"
S_DUPLICATE = "skipped_duplicate"
S_RESUMED = "skipped_resumed"
S_UNPROCESSED = "skipped_unprocessed"
S_FAILED = "failed"

# 这几组是「同一种归类」的唯一定义处, 别在别处再手写一遍状态列表:
#
#   ORG_OK        成功, 或本来就无需动作
#   ORG_SKIP      有意不动作(对方那边已经有邀请 / 这一行本身没什么可做的)
#   ORG_PENDING   悬着: 计划要发、或没预检状态不明 —— 都还不算「办结」
#   ORG_UNTOUCHED 跟对方无关, 纯粹是表格或 --limit 造成的(汇总行的「合计」与那些
#                 具名桶之间的差额就是它, 明细行里逐条列着)
#   ORG_SETTLED   已有终态, 可以写进续跑历史
#
# `planned` / `unknown` **故意不属于上面任何一组**。它们曾经被塞进 ORG_SKIP, 于是
# dry-run 的汇总行把同一批人数了两遍(「计划邀请 4 人 … 跳过 4 人」, 字面像 8 个人)。
# 汇总行必须是一个**分区**: 每个状态只进一个桶, 各桶相加 == 逐行结果的总人数。
# 动这里的集合之前先看 `report.summarize_org`。
ORG_OK = {S_INVITED, S_ALREADY_MEMBER}
ORG_SKIP = {S_ALREADY_INVITED, S_NO_CONTACT, S_DUPLICATE, S_RESUMED, S_UNPROCESSED}
ORG_PENDING = {S_PLANNED, S_UNKNOWN}
ORG_UNTOUCHED = {S_NO_CONTACT, S_DUPLICATE, S_UNPROCESSED}
ORG_SETTLED = ORG_OK | ORG_SKIP

# Team 这一档
#
# 注意: 组织档和 team 档的状态值是两套独立的字符串, 不能重名。
# 之前 T_FAILED 也叫 "failed", 和 S_FAILED 撞成了同一个字典键, 于是
# REMEDY 里 team 那条把组织那条悄悄覆盖掉了 —— 组织邀请失败的行会拿到
# 一段讲 team 的建议。所有状态值必须两两不同。
T_ALREADY = "already_in_team"
T_ADDED = "team_added"
T_PENDING_ACCEPT = "pending_org_accept"
T_NEEDS_USERNAME = "needs_username"
T_USERNAME_MISSING = "username_missing"
T_FAILED = "team_failed"
T_NA = "not_requested"      # 本次没要求做 team

ORG_LABEL = {
    S_PLANNED: "计划邀请(dry-run)",
    S_UNKNOWN: "状态未知(未预检)",
    S_INVITED: "组织邀请已发出",
    S_ALREADY_MEMBER: "已是组织成员",
    S_ALREADY_INVITED: "已有待处理邀请",
    S_NO_CONTACT: "跳过(没有可用的邮箱/用户名)",
    S_DUPLICATE: "跳过(表里重复)",
    S_RESUMED: "跳过(上次已成功)",
    S_UNPROCESSED: "跳过(--limit 截断)",
    S_FAILED: "邀请失败",
}
TEAM_LABEL = {
    T_ALREADY: "已在 team",
    T_ADDED: "已加入 team",
    T_PENDING_ACCEPT: "待对方接受组织邀请",
    T_NEEDS_USERNAME: "缺用户名, 加不了 team",
    T_USERNAME_MISSING: "用户名不存在",
    T_FAILED: "加入 team 失败",
    T_NA: "—",
}

# 状态 -> 处理措施。需求明确要求给出「发送失败的原因和处理措施」。
REMEDY = {
    S_PLANNED: "这是 dry-run 的计划。确认无误后加 --execute 真正发送。",
    S_UNKNOWN: "本次没预检(--no-preflight), 不知道对方在不在组织里。去掉 --no-preflight 重跑一次才能确认。",
    S_INVITED: "无需处理。7 天内没接受就过期, 到期前催一下。",
    S_ALREADY_MEMBER: "无需处理, 对方已经在组织里, 接口也不会给他发邮件。",
    S_ALREADY_INVITED: "无需重发, 等对方接受即可; 用 --status 可以看到这条邀请还挂着没有。",
    S_NO_CONTACT: "让对方补一个 GitHub 用户名或邮箱, 再重跑这一条。",
    S_DUPLICATE: "无需处理, 表里同一个人只发一次。",
    S_RESUMED: "无需处理。想强制重发加 --no-resume(会重复发送, 慎用)。",
    S_UNPROCESSED: "本次被 --limit 截断, 不是失败。不带 --limit 再跑一次就会处理。",
    S_FAILED: "照上面的原因修好表格或权限后重跑; 已是成员的人不会收到第二封邮件。",
    T_ALREADY: "无需处理, 已在 team 里, 脚本不会动他的 team 角色。",
    T_ADDED: "无需处理。",
    T_PENDING_ACCEPT: "等对方接受组织邀请(7 天内), 再跑一次同样的命令, 会自动把他补进 team。",
    T_NEEDS_USERNAME: "去 GitHub 搜到他的用户名, 用名单文件或 --cols 指定用户名后重跑。",
    T_USERNAME_MISSING: "GitHub 上查不到这个用户名, 对方接受不了邀请。核对拼写后改表格重跑"
                        "(也可能对方把资料设为私密, 那就让他确认用户名)。",
    T_FAILED: "照上面的原因查: 对方是不是组织成员、用户名拼写对不对、token 有没有该 team 的写权限。",
}


@dataclass
class Outcome:
    record: Record
    org_status: str = ""
    org_detail: str = ""
    team_status: str = T_NA
    team_detail: str = ""
    executed: bool = False

    @property
    def has_failure(self) -> bool:
        return (self.org_status == S_FAILED or self.team_status == T_FAILED
                or self.team_status == T_USERNAME_MISSING)

    @property
    def committed(self) -> bool:
        """这一条是否已办结(成功或已确定不动), 用来写进续跑历史。

        计划中的(planned / unknown)不算: 它们还没真的做过什么。
        """
        return self.org_status in ORG_SETTLED


def parse_conflict_message(message: str) -> str:
    """把 422 的原文归成 already_member / already_invited / 其它。"""
    lowered = message.lower()
    if "already a part of this organization" in lowered or "already a member" in lowered:
        return S_ALREADY_MEMBER
    if "invitation" in lowered and "already" in lowered:
        return S_ALREADY_INVITED
    return ""
