#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量邀请人加入 GitHub 组织, 可选顺手加入指定 Team。

一个入口, 一份输入, 两档动作:

  第 1 档(默认)      组织邀请  POST /orgs/{org}/invitations
  第 2 档(加 --team)  Team      PUT /orgs/{org}/teams/{slug}/memberships/{username}

输入只有一种: 一张表(.xlsx / .csv)。表里至少要有「邮箱」或「GitHub用户名」一列 ——
有用户名就用用户名邀请(更可靠), 没有才退回邮箱。

默认是 dry-run: 不加 --execute 绝不发出任何邀请, 只解析 + 预检 + 打印计划。

实测出来的几条硬限制(详见 ARCHITECTURE.md):
  * 组织邀请可以只用邮箱, 但只有「对方已验证的邮箱」才点得动邀请链接;
    未验证时接口照样回 201。所以只有邮箱的行, 事后要用 --status 对一遍。
  * 已经是成员的人, 接口回 422 并明确说明, 且不会给对方发邮件。
  * Team 成员接口只收 GitHub 用户名, 不认邮箱; 且对方必须先成为组织成员。

本文件不出现任何真实姓名/邮箱/用户名/token。

--------------------------------------------------------------------------
本文件现在只是个**兼容层**:

  * 真正的实现在 `inviter/` 包里, 按职责分模块(见 ARCHITECTURE.md 的模块地图);
  * 这里保留旧的单文件导入面, 于是 `import github_inviter as g`、
    `g.process_org_invites(...)`、`python github_inviter.py ...` 全都照旧能用;
  * **不许往这个文件里加逻辑**。新功能请加到 `inviter/` 下对应的模块,
    要暴露给外部就往下抄一行 import。

一个必须知道的细节: 下面那些名字都只是对 `inviter.*` 里**同一个函数对象**的引用。
所以 `github_inviter._stdin_is_interactive = ...` 这种替换是**无效的** ——
内部调用点引用的是 `inviter.prompt` 里的那个名字。要替行为请改
`inviter.prompt._stdin_is_interactive`(或等价的 `github_inviter.prompt._stdin_is_interactive`)。
`_test/run_prompt_tests.py` 就是按后者写的。
"""

from __future__ import annotations

# 子模块本身也一起导出: 测试与排查时会直接引用它们(而且 prompt 必须能被替换)
from inviter import (  # noqa: F401
    cli,
    columns,
    console,
    constants,
    github_api,
    input_source,
    outcomes,
    prompt,
    records,
    report,
    storage,
    tableio,
    tokens,
    workflow,
)
from inviter.cli import (  # noqa: F401
    _name_of,
    _rerun_hint,
    default_out_dir,
    main,
    normalize_argv,
    parse_args,
)
from inviter.columns import (  # noqa: F401
    COLUMN_ROLES,
    EMAIL_HEADERS,
    EMAIL_HINTS,
    NAME_HEADERS,
    NAME_HINTS,
    ROLE_ALIASES,
    SKIP_TOKENS,
    USERNAME_HEADERS,
    USERNAME_HINTS,
    _blank_cols,
    detect_columns,
    locate_header,
    match_role,
    norm_key,
    parse_cols_spec,
)
from inviter.console import (  # noqa: F401
    clip,
    die,
    display_width,
    log,
    now_text,
    pad,
    timestamp,
)
from inviter.constants import (  # noqa: F401
    API_VERSION,
    APP_NAME,
    APP_VERSION,
    DEFAULT_API,
    DEFAULT_DELAY,
    DEFAULT_PAGE,
    EMAIL_RE,
    GITHUB_URL_RE,
    LOGIN_RE,
    MAX_RETRIES,
    PACKAGE_DIR,
    PROJECT_ROOT,
    TABLE_SUFFIXES,
    USER_AGENT,
)
from inviter.github_api import ApiError, GitHub, describe_error  # noqa: F401
from inviter.input_source import (  # noqa: F401
    InputSource,
    _index_of,
    _load_table_source,
    _usage_hint,
    records_from_tokens,
    resolve_input,
)
from inviter.outcomes import (  # noqa: F401
    ORG_LABEL,
    ORG_OK,
    ORG_PENDING,
    ORG_SETTLED,
    ORG_SKIP,
    ORG_UNTOUCHED,
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
    parse_conflict_message,
)
from inviter.prompt import (  # noqa: F401
    _ask_line,
    _stdin_is_interactive,
    ask_columns,
    ask_data_rows,
    parse_row_range,
    show_rows,
)
from inviter.records import (  # noqa: F401
    Record,
    build_records,
    classify_cell,
    clean_email,
    clean_login,
    email_note_of,
)
from inviter.report import (  # noqa: F401
    count_by,
    print_issues,
    print_preflight,
    print_results,
    print_retry_list,
    print_status_report,
    print_summary,
    print_table,
    result_row,
    summarize_org,
    summarize_team,
)
from inviter.storage import load_history, write_outputs  # noqa: F401
from inviter.tableio import read_table  # noqa: F401
from inviter.tokens import (  # noqa: F401
    TOKEN_FILE_NAMES,
    TOKEN_KEYS,
    _read_token_file,
    resolve_token,
)
from inviter.workflow import (  # noqa: F401
    deduplicate,
    process_org_invites,
    process_team,
    resolve_emails_to_logins,
    seen_in_history,
    select_team,
)

__all__ = [
    # 子模块
    "cli", "columns", "console", "constants", "github_api", "input_source",
    "outcomes", "prompt", "records", "report", "storage", "tableio", "tokens",
    "workflow",
    # 常量
    "API_VERSION", "APP_NAME", "APP_VERSION", "DEFAULT_API", "DEFAULT_DELAY",
    "DEFAULT_PAGE", "EMAIL_RE", "GITHUB_URL_RE", "LOGIN_RE", "MAX_RETRIES",
    "PACKAGE_DIR", "PROJECT_ROOT", "TABLE_SUFFIXES", "USER_AGENT",
    # 通用
    "clip", "die", "display_width", "log", "now_text", "pad", "timestamp",
    # 表格读取与列识别
    "read_table", "COLUMN_ROLES", "EMAIL_HEADERS", "EMAIL_HINTS", "NAME_HEADERS",
    "NAME_HINTS", "ROLE_ALIASES", "SKIP_TOKENS", "USERNAME_HEADERS",
    "USERNAME_HINTS", "detect_columns", "locate_header", "match_role", "norm_key",
    "parse_cols_spec",
    # 记录
    "Record", "build_records", "classify_cell", "clean_email", "clean_login",
    "email_note_of",
    # 输入来源
    "InputSource", "records_from_tokens", "resolve_input",
    # API 与 token
    "ApiError", "GitHub", "describe_error", "TOKEN_FILE_NAMES", "TOKEN_KEYS",
    "resolve_token",
    # 结果模型
    "ORG_LABEL", "ORG_OK", "ORG_PENDING", "ORG_SETTLED", "ORG_SKIP", "ORG_UNTOUCHED",
    "REMEDY", "S_ALREADY_INVITED",
    "S_ALREADY_MEMBER", "S_DUPLICATE", "S_FAILED", "S_INVITED", "S_NO_CONTACT",
    "S_PLANNED", "S_RESUMED", "S_UNKNOWN", "S_UNPROCESSED", "T_ADDED", "T_ALREADY",
    "T_FAILED",
    "T_NA", "T_NEEDS_USERNAME", "T_PENDING_ACCEPT", "T_USERNAME_MISSING",
    "TEAM_LABEL", "Outcome", "parse_conflict_message",
    # 打印
    "count_by", "print_issues", "print_preflight", "print_results",
    "print_retry_list", "print_status_report", "print_summary", "print_table",
    "result_row", "summarize_org", "summarize_team",
    # 落盘与续跑
    "load_history", "write_outputs",
    # 工作流
    "deduplicate", "process_org_invites", "process_team",
    "resolve_emails_to_logins", "seen_in_history", "select_team",
    # 入口
    "default_out_dir", "main", "normalize_argv", "parse_args",
    # 兼容用的下划线名(旧单文件里就是模块级的, 测试直接引用了它们)
    "_ask_line", "_blank_cols", "_index_of", "_load_table_source", "_name_of",
    "_read_token_file", "_rerun_hint", "_stdin_is_interactive", "_usage_hint",
]


if __name__ == "__main__":
    raise SystemExit(main())
