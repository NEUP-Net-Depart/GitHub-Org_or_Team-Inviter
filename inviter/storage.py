# -*- coding: utf-8 -*-
"""结果文件的读写: 落盘(`results-*.csv|jsonl`、`plan-*.csv`)与续跑历史。

读写放在同一个模块里, 是因为「写出去的 jsonl」和「读回来的历史」必须成对理解:
改一边就得想另一边(比如 BOM 那个坑)。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence

from .console import now_text
from .outcomes import (
    ORG_LABEL,
    REMEDY,
    S_FAILED,
    T_FAILED,
    T_NEEDS_USERNAME,
    T_PENDING_ACCEPT,
    T_USERNAME_MISSING,
    TEAM_LABEL,
    Outcome,
)


def load_history(out_dir: Path) -> dict[str, dict]:
    """记住哪些人上次已经成功处理过。

    用 utf-8-sig 读: 结果文件带 BOM 时, 用 utf-8 读会让第一行 JSON 解析失败被静默丢掉。
    """
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
            for key_name in ("email", "login"):
                key = str(item.get(key_name) or "").strip().lower()
                if key:
                    history[key] = item  # 越晚的记录越新, 覆盖旧的
    return history


def write_outputs(out_dir: Path, stamp: str, outcomes: Sequence[Outcome],
                  execute: bool) -> tuple[Path, ...]:
    """落盘。

    execute=True  -> 书面结果: results-*.csv(给人看) + results-*.jsonl(续跑靠它)
    execute=False -> 计划: plan-*.csv(将要邀请谁、排除了谁、为什么)
    """
    if not execute:
        plan_path = out_dir / f"plan-{stamp}.csv"
        with plan_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["行号", "姓名", "邮箱", "用户名", "本次动作", "team动作", "说明"])
            for out in outcomes:
                writer.writerow([
                    out.record.row, out.record.label, out.record.email, out.record.login,
                    ORG_LABEL.get(out.org_status, out.org_status),
                    TEAM_LABEL.get(out.team_status, out.team_status),
                    out.org_detail or out.team_detail,
                ])
        return (plan_path,)

    csv_path = out_dir / f"results-{stamp}.csv"
    jsonl_path = out_dir / f"results-{stamp}.jsonl"

    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["行号", "姓名", "邮箱", "用户名", "组织邀请状态", "组织邀请说明",
                         "team状态", "team说明", "失败原因", "处理措施", "时间"])
        for out in outcomes:
            problem = ""
            if out.org_status == S_FAILED:
                problem = out.org_detail
            elif out.team_status in (T_FAILED, T_USERNAME_MISSING):
                problem = f"team: {out.team_detail}"
            # 处理措施要跟着真正的失败原因走。组织这头失败了, 就必须给组织这头的
            # 措施, 别被 team 状态抢走 —— 否则建议和失败原因对不上, 反而误导人。
            if out.org_status == S_FAILED:
                remedy = REMEDY[S_FAILED]
            elif out.team_status in (T_FAILED, T_USERNAME_MISSING, T_PENDING_ACCEPT,
                                     T_NEEDS_USERNAME):
                remedy = REMEDY[out.team_status]
            else:
                remedy = REMEDY.get(out.org_status, "")
            writer.writerow([
                out.record.row, out.record.label, out.record.email, out.record.login,
                ORG_LABEL.get(out.org_status, out.org_status), out.org_detail,
                TEAM_LABEL.get(out.team_status, out.team_status), out.team_detail,
                problem, remedy, now_text(),
            ])

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for out in outcomes:
            handle.write(json.dumps({
                "row": out.record.row,
                "name": out.record.name,
                "email": out.record.email,
                "login": out.record.login,
                "org_status": out.org_status,
                "org_detail": out.org_detail,
                "team_status": out.team_status,
                "team_detail": out.team_detail,
                "committed": out.committed,
                "time": now_text(),
            }, ensure_ascii=False) + "\n")
    return csv_path, jsonl_path
