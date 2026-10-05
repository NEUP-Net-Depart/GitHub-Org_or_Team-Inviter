# 架构与实现说明

> 面向**维护者与 AI**: 记录设计取舍、接口细节、边界情况和踩坑。
> 日常使用请看 [README.md](README.md)。

实现按职责拆成 `inviter/` 包(**零第三方依赖**, Python 3.10+)。仓库根的
`github_inviter.py` 只是转发用的**兼容层**, 但入口仍然是它 —— 旧命令、旧文档、
`import github_inviter as g` 全都照旧能用(见下面「模块地图」)。
脚本本身**不发邮件** —— 邀请邮件由 GitHub 发出。

```
输入解析 ──► 记录构造 ──► 预检 ──► 第1档: 组织邀请 ──► 第2档: team ──► 打印 + 落盘
(read_table)  (build_records) (members/       (POST /orgs/{org}     (PUT .../teams/   (csv+jsonl)
                              invitations)     /invitations)         {slug}/memberships)
```

---

## 一、必须先知道的硬限制

这些全部来自实测(见 [.dsh/logs](.dsh/logs)), 直接决定了上面的设计:

| 事实 | 影响 |
|---|---|
| `POST /orgs/{org}/invitations` 接受 `email` **或** `invitee_id`, 二选一 | 有用户名就换成 `invitee_id` 邀请, 绕开邮箱坑 |
| 只有**已验证**邮箱的邀请对方才点得动; 未验证时接口**照样回 201** | 只有邮箱的行不能只看 201, 必须事后 `--status` 对账 |
| 已是成员时回 `422 A user with this email address is already a part of this organization`, 且**不发邮件** | 靠接口原文判定 `already_member`, 不靠猜 |
| `GET /orgs/{org}/members` 只返回 `login`, **没有任何邮箱** | 表里只有邮箱时, 预检认不出「他已经是成员」—— 靠 422 兜住 |
| 按邮箱发的邀请, 在 API 里 `login` **恒为 `null`** | 待处理邀请要同时看 `email` 和 `login` 两个集合 |
| Team 成员接口只收**用户名**, 不认邮箱(实测 6 种写法全 404) | 只有邮箱的人本次加不了 team, 必须明确报告 |
| 未接受组织邀请的人加不进 team | 第 2 档只对「已确认在组织里」的人动手 |
| 组织邀请 **7 天过期** | 失败清单里的 `Invitation expired` 单独给「重发」的建议 |
| `PUT .../teams/{slug}/memberships/{username}` 会**改**角色 | 已在 team 的人必须跳过, 否则 maintainer 被降级 |
| 用户名列写的用户名可能**根本不存在**(`GET /users/{login}` 404), 而邮箱却属于某个**已经是成员**的人 | 这种行会走邮箱邀请, 然后收到 422「已是成员」—— 见下方「同一个人的用户名和邮箱可能对不上」 |

---

## 二、模块地图

| 模块 | 职责 | 关键函数 |
|---|---|---|
| `constants.py` | 常量 + **项目根锚点** | `APP_NAME` / `PROJECT_ROOT` / 各正则 |
| `console.py` | 输出与中文按 2 列宽对齐 | `log` / `die` / `pad` / `clip` / `display_width` |
| `tableio.py` | csv/tsv/xlsx 零依赖读取 | `read_table` / `_read_xlsx_minimal` / `_read_delimited` |
| `columns.py` | 三步定列 | `detect_columns` / `locate_header` / `parse_cols_spec` / `norm_key` / `_blank_cols` |
| `records.py` | 清洗 + 按内容归类 | `clean_email` / `clean_login` / `classify_cell` / `build_records` / `Record` |
| `prompt.py` | 表头认不出时问人 | `ask_columns` / `ask_data_rows` / `parse_row_range` / `_ask_line` |
| `input_source.py` | 一个表格文件 + 可选的命令行补充 | `resolve_input` / `records_from_tokens` / `InputSource` |
| `github_api.py` | REST 客户端(限流退避、分页、各接口封装) | `GitHub` / `ApiError` / `describe_error` |
| `tokens.py` | token 解析 | `resolve_token`(`--token` > 环境变量 > `token.txt`) |
| `outcomes.py` | 结果模型与状态码 | `Outcome` / `S_*` / `T_*` / `ORG_LABEL` / `TEAM_LABEL` / `REMEDY` |
| `report.py` | 控制台输出 | `print_results` / `print_summary` / `print_issues` / `print_retry_list` / `print_status_report` |
| `storage.py` | 落盘 + 续跑历史 | `write_outputs` / `load_history` |
| `workflow.py` | 两档动作与管道步骤 | `process_org_invites` / `process_team` / `deduplicate` / `select_team` |
| `cli.py` | 参数表 + 组装(**唯一的组装点**) | `parse_args` / `normalize_argv` / `main` |

依赖方向**只能自上而下, 不许成环**(`_test/run_module_tests.py` 会逐个模块单独导入来验):

```
constants / console
  └─► columns / records / tokens
        └─► tableio / prompt / github_api / outcomes
              └─► input_source / report / storage
                    └─► workflow
                          └─► cli
                                └─► github_inviter.py (兼容层, 无逻辑)
```

### 兼容层 `github_inviter.py` 的三条约定

1. **里面不许有 `def` / `class`。** 它只做 `from inviter.… import …`, 再接
   `if __name__ == "__main__": raise SystemExit(main())`。**新功能加到 `inviter/` 下**,
   要暴露给外面就在兼容层的 `__all__` 里补一行 —— 这条由测试用 AST 盯着, 加逻辑会当场红。
2. **它导出的是「同一批函数对象」的引用, 不是包装。** 所以
   `github_inviter._stdin_is_interactive = ...` 这种替换**无效**: 内部调用点引用的是
   `inviter.prompt` 里的那个名字。要替行为请改 `inviter.prompt.<名字>`
   (或等价的 `github_inviter.prompt.<名字>`)。
   正因如此, `input_source` 里一律写 `prompt.ask_columns(...)` 这种**模块限定调用**,
   保证 patch 点只有一个 —— 写成 `from .prompt import ask_columns` 会让替换静默失效
   (这一条是实测踩出来的, `_test/run_prompt_tests.py` 现在就这么替换)。
3. **`constants.PROJECT_ROOT` 是「脚本旁边」的唯一解释。** 约定 `inviter/` 就放在仓库根下,
   于是 `PROJECT_ROOT == 仓库根 == github_inviter.py 所在目录`。子模块**不许**再自己算
   `Path(__file__).parent` —— 那会指向 `inviter/`, 让 `token.txt` 和默认 `output/`
   一起跑偏。这是拆模块时最容易踩的坑, 已有测试专门盯着它。


---

## 三、内容归类: 为什么不靠列名

用户的表格是问卷导出的, 经常出现「邮箱列里填了用户名」「用户名列里填了主页链接」
这类事情。所以定好列之后, `build_records` 把**两列都过一遍** `classify_cell(value)`:

```
classify_cell(value):
    能解析出合法邮箱   -> (email, "")
    否则能解析出用户名 -> ("", login)
    否则               -> ("", "")
```

合并时**各自优先信本职列**:

```python
email = email_from_email_col or (email_from_login_col if not login_from_login_col else "")
login = login_from_login_col or (login_from_email_col if not email_from_email_col else "")
```

这个顺序是刻意为之。旧项目曾写成 `login = login_a or login_b`(邮箱列优先),
结果示例表邮箱列里的 `not-an-email` 虽然不是邮箱、**却恰好是合法用户名格式**,
把用户名列里真正的 `sunba` 盖掉了。**本职列优先**才修好。

另外 `classify_cell` **不返回备注**(它只返回两元组)。要拿「一格里有 2 个邮箱,
只取了第一个」这类提醒, 用单独的 `email_note_of()` —— 这里踩过一次:
早期写成 `email_from_email_col, email_note = classify_cell(...)`, 备注恒为空,
因为 `classify_cell` 第二个位置是**用户名**。

---

## 四、三步定列

1. **`--cols` / `--email-col` / `--username-col`** —— 用户显式指定, 优先级最高。
   `--cols` 按**位置**(从 1 开始)解析, 支持 `username/email/name` 及中文别名,
   `-` 表示跳过该列; 指到超出表宽的列会 `die` 并提示, **不静默变空**。
2. **表头关键字** —— `locate_header` 在前几行里找最像表头的一行(按认出几个角色打分)。
3. **位置兜底** —— 一个都没认出来时, 第 1 列用户名、第 2 列邮箱。

第三步原本是「按单元格内容猜列」, 后来删掉了: 英文昵称(如 `alice`)本来就同时像用户名,
猜错的代价是**静默处理错人**。把列的语义交给用户显式约定更可预测。

### `--cols` 与表头行是两件事

脚本要定两件**互相独立**的事:

1. **哪一列是什么** —— `--cols` / 表头关键字 / 位置兜底;
2. **哪几行是数据** —— `--skip-rows` / `--no-header` / 自动认出的表头。

早期实现把两者搅在一起: 给了 `--cols` 就连表头识别一起关掉, 于是
「有表头 + 列名认不出」的表格**没有任何干净写法** —— 表头行会被当成数据。
用户问「我能不能写 `--cols "-,username,email,name,-"`」时, 答案里藏着的就是这个别扭。

现在两者分开, 且**表头认得出时给不给 `--cols` 都会自动跳过表头行**。

### 认不出来时问用户, 而不是猜

表头名字和列位置都认不出来时, 以前是直接报错退出。现在改成**在终端里问两句**:

```
表格前 3 行...        <- 让人能指出数据从哪行开始
这一行数据长这样...    <- 让人能指出哪一列是什么
列的顺序(直接回车 = 按老约定: 第1列用户名、第2列邮箱):
数据行范围(例如 2 或 2-10, 写 0 表示全是数据):
```

回答格式和 `--cols` 完全一致, 所以学一次两边都能用。几条例外:

- **给了参数就不再问** —— `--cols` / `--skip-rows` / `--no-header` 已经回答了对应的问题;
- **`--no-header` 表示"整张表都是数据"**, 所以第 2 个问题也不再问;
- **`--skip-rows 0` 是"显式给过"** 的语义, 会关掉自动表头识别(与旧行为一致);
- **非交互环境(脚本/管道/CI)绝不静默挂住**: `_stdin_is_interactive()` 为假时直接
  `die`, 并给出两条出路(在终端里跑, 或用 `--cols` + `--skip-rows`)。这是刻意的 ——
  按错的列发邀请会真的打扰到人, 宁可停下。

> **Windows 的坑**: 测试非交互路径时不能用 `subprocess.DEVNULL` —— Windows 上 NUL 是
> 字符设备, `sys.stdin.isatty()` 会返回 **True**, 等于没切断, 测试会卡在提问上。
> 要用 `input=""`(即 PIPE)。这是实测出来的, 不是猜的。

> **踩坑记录**: 实现时把 `first_data` 当成相对 `rows`(已砍掉 skip 行)的偏移, 却又拿它切
> `rows`, 等于**跳了两次**, `--skip-rows 1` 会少一行数据。现在统一用绝对偏移
> `data_offset`(相对整张表), 切片时再减掉 skip, 并有一条测试专门盯这个行数。

### 为什么只收表格(不支持 .txt / 管道)

早期版本还支持「每行一个用户名」的纯文本清单和 stdin。**已按用户要求移除**, 理由是它
在语义上就不够用, 而不是单纯"代码多":

- 一行只有一个字段, **装不下「邮箱 + 用户名」**。于是只有用户名的人发不了邮箱邀请,
  只有邮箱的人进不了 team 也认不出是否已是成员 —— 这个入口从一开始就带着两个能力缺口。
- 它需要**自己一套解析语义**(跳过 `#` 注释、只取第一个逗号前的字段、逐行按内容判邮箱还是
  用户名), 与表格分支并存。同一件事两套规则, 正是最容易长 bug 的地方。
- 表格分支本来就能覆盖它: 想要"一行一个人", 给一个单列 csv 即可。

`read_table` 现在遇到 `.txt` 会**明确报错并指向表格**(而不是静默当成单列表格),
免得老命令升级后行为悄悄变了。

---

## 五、两档动作的判定

### 第 1 档: 组织邀请

判定顺序(命中即停):

```
在成员列表里                      -> already_member (跳过)
邮箱/用户名 在待处理邀请里        -> already_invited (跳过)
历史 results-*.jsonl 里已成功过   -> skipped_resumed (跳过, --no-resume 可关)
--no-preflight                    -> unknown  (状态未知: 根本没查, 不能叫「计划要发」)
dry-run(预检过)                   -> planned  (确认可以发, 但这次没发)
否则                              -> POST 发邀请
```

发邀请时: 有用户名就 `GET /users/{login}` 拿 `id` 走 `invitee_id`;
拿不到(用户名拼错)再退回 `email`; 两者都没有才判失败。

响应处理:

| 状态码 | 判定 |
|---|---|
| 2xx | `invited` |
| 422 + `already a part of this organization` | `already_member` |
| 422 + 提到已有邀请 | `already_invited` |
| 422 其它 | `failed`, 保留原文 |
| 404 | 组织名错 / token 不是该组织 owner |
| 403 | 权限不足(细粒度要 Members 写权限且已批准; 经典要 `admin:org`) |
| 403 二级限流 / 429 / 5xx | 按 `Retry-After` 和 `X-RateLimit-Reset` 退避重试, 最多 4 次 |

### 第 2 档: team

严格「**先查后写**」: 先 `GET .../teams/{slug}/members` 拿现有成员, 再决定动不动手。

```
已在 team 里            -> already_in_team (绝不重设角色)
没有用户名              -> needs_username (team 接口不认邮箱)
不是组织成员            -> pending_org_accept (现在加必然失败, 留到下次)
dry-run                 -> team_added (计划)
否则                    -> PUT .../memberships/{username}
```

`pending_org_accept` 就是需求里「team 邀请, 如果没在组织内就先发组织邀请」的落地方式:
本次只发邀请, 对方接受后**重跑同一条命令**就自动补进 team。收尾会打印「待重跑名单」(按姓名)。

### 同一个人的用户名和邮箱可能对不上(实测遇到的真实情况)

有一次实跑, 预检说某两人「还不在组织里」, 结果发邀请时收到 422
`A user with this email address is already a part of this organization`。查下来是:

- 表里填的**用户名在 GitHub 上不存在**(`GET /users/{login}` → 404);
- 但表里那个**邮箱属于某个已经在组织里的账号**。

也就是说这行数据的**用户名和邮箱不是同一个人**(或者用户名拼错了)。
所以「预检说不在组织里」不等于「真的不在」:

- 用户名在成员列表里 → 跳过, 精确;
- 只有邮箱时, 预检查不出成员关系(成员接口不返回邮箱) → **靠发邀请时的 422 兜住**,
  判定为 `already_member`, 并且**不会给对方发邮件**(实测邀请 ID 前后不变)。

`parse_conflict_message` 只认 GitHub 的两种**明确措辞**
(`already a part of this organization` / `already a member`, 以及
`invitation` + `already`) —— 刻意不用宽松的「含 already 就算已存在」, 否则
`Name already exists` 这类无关的 422 会被误判成「已加入」, 把真失败吞掉。

### `--limit` 是「表里的前 N 行」, 不是「处理到成功 N 个」

这两者的区别会咬人。`--limit 2` 的语义是**只看表里第 1~2 行**:

- 这 2 行里如果有重复行、空行、或者上次已经成功过的人, 本次实际发出去的**少于 2 个**,
  这是对的 —— 它就是「按行号取样」;
- 反过来, 如果把 `--limit` 实现成「取前 N 个还没处理的」, 那么已经被续跑跳过的人会
  被**排除在 N 之外**, 结果 `--limit 2` 静默地处理了第 4、5 行, 前 N 行的语义就废了。

所以判断「在不在前 N 行」时, 必须用**表里的原始行号**(`Record.row`)去比, 而且要把
同一段行里的重复行、空行一起算进 N。剩下没轮到的行标记为 `skipped_unprocessed`
(「--limit 截断」), 明确说明**不是失败**, 并计入结果文件。

---

## 六、输出与续跑

- **控制台**: 逐行表(姓名/邮箱/用户名/组织邀请/Team/说明) + 汇总 + **失败明细(按原因归类,
  每条给处理措施)** + 待重跑名单。中文按 `unicodedata.east_asian_width` 算 2 列宽对齐。
- **`results-*.csv`**: `utf-8-sig`, Excel 直接打开, 含「失败原因」和「处理措施」两列。
- **`results-*.jsonl`**: 续跑凭据。`load_history()` 扫 `output/results-*.jsonl`,
  按 `email` 和 `login` 建索引, 越晚的记录越新。
  **必须用 `utf-8-sig` 读** —— 结果文件带 BOM 时用 `utf-8` 读会让第一行 JSON 解析失败
  被静默丢掉, 表现为「续跑少跳了一个人」。

`--no-preflight` 时状态是「不知道」, 用独立的 `unknown` 状态, 绝不冒充「已发出」;
即使配了 `--execute` 也不会发请求(没预检就发等于盲发)。这条容易被误会 —— 用户以为
「加了 `--execute` 就发出去了」, 所以它是**出声**的: 两个参数一起给会当场打印
「`--no-preflight` 优先, 本次一封都不会发」, 收尾也不会再劝他加一个刚加过的参数。
(这两句话曾经是静默的: 想提醒的那句警告落在了一个永远走不到的分支里 ——
`mode == "execute"` 的分支必然 `needs_api == True`。现在有离线测试钉着。)

### 汇总行是怎么算出来的

「组织邀请: …」那一行是一个**分区**: 每个状态只进一个桶, 末尾的**「合计」= 下面逐行
结果的总人数**。具名桶相加和合计之间的差额, 一定来自 `ORG_UNTOUCHED`(没联系方式 /
表里重复 / `--limit` 没轮到), 而那三类在明细行里逐条列着 —— 所以这一行可以直接当账对。

```
组织邀请: 计划邀请 1 人 | 已是组织成员 5 人 | 已有待处理邀请 0 人 | 邀请失败 0 人 | 合计 6 人
  - 计划邀请(dry-run): 1 人
  - 已是组织成员: 5 人
```

第一桶叫什么由模式决定, 后面几桶固定:

| 怎么跑 | 第一桶 | 含义 |
|---|---|---|
| `--execute` | 已发出邀请 N 人 | 这次真发了 |
| dry-run(预检过) | 计划邀请 N 人 | 确认可以发, 但这次没发 |
| `--no-preflight` | 状态未知(未预检) N 人 | **没查过**, 连他能不能发都不知道 |

| 桶(按行的顺序) | 装什么 | 状态值 |
|---|---|---|
| 第一桶 | 见上表, 三选一 | `invited` / `planned` / `unknown` |
| 已是组织成员 | 本来就在组织里 | `already_member` |
| 跳过(上次已成功) | 续跑记忆生效(>0 时才出现) | `skipped_resumed` |
| 已有待处理邀请 | 对方那边已经挂着一封 | `already_invited` |
| 邀请失败 | 真失败, 明细在后面单列 | `failed` |
| **合计** | 本次纳入统计的总人数 | 全部状态之和 |

具名桶的措辞一律取自 `ORG_LABEL`, 免得汇总和明细两处叫法漂移(曾经上面写「已是成员」、
下面明细写「已是组织成员」)。

**末尾不要放「桶」** —— 这里踩过两次:

1. `planned` 和 `already_invited` 曾经既被单独列出来、又被末尾那个总数数了一遍。4 行的表
   读出「计划邀请 4 人 … 跳过 4 人」, 字面像 8 个人; 更极端的一次是 12 行的表, 各桶相加
   有 22 人。
2. 改成只装 `ORG_UNTOUCHED` 之后给它起名「本次没动」, 结果一眼就被看穿: 那措辞像在描述
   **整次运行**(dry-run 本来就什么都没发), 而它其实只描述一类行; 恒为 0 时更是废话。

所以末尾只放「合计」, 一个不会重复计数、也不需要解释的词。改汇总之前先看状态归类
(定义都在 `inviter/outcomes.py`):

| 集合 | 含义 |
|---|---|
| `ORG_OK` | 成功, 或本来就无需动作 |
| `ORG_SKIP` | 有意不动作(对方已有邀请 / 这一行本身没什么可做的) |
| `ORG_PENDING` | 悬着: 计划要发、或没预检状态不明 —— 都还不算「办结」 |
| `ORG_UNTOUCHED` | `ORG_SKIP` 里跟对方无关的那部分(合计与具名桶之间的差额) |
| `ORG_SETTLED` | 已有终态, 可以写进续跑历史(= `ORG_OK \| ORG_SKIP`) |

这五个集合 + `failed` 覆盖全部状态且**两两不重叠**: `_test/run_module_tests.py` 断言
这一点, `_test/run_output_tests.py` 还会把三种模式的汇总各数一遍、断言合计等于总人数、
且差额恰好等于 `ORG_UNTOUCHED` 的人数。**加新状态时必须同时想清楚它属于哪个桶**,
否则汇总立刻开始漏人或重复计数。

### 状态值与处理措施

`ORG_LABEL` / `TEAM_LABEL` / `REMEDY` 三张表按状态值索引, 所以**所有状态值必须两两不同**。
这里踩过一次大的: `S_FAILED` 和 `T_FAILED` 都写成字符串 `"failed"`, 撞成同一个字典键,
`REMEDY` 里 team 那条把组织那条**静默覆盖**了 —— 组织邀请失败的行会拿到一段讲 team 的建议。
现在 team 档的值一律带 `team_` 前缀(`team_added` / `team_failed`), 并且有一个离线测试
专门断言所有状态值两两不同, 防止复发。

`process_team` 会重设 `outcome.team_status`, 但**不会**重设 `org_status`, 所以已落定的
组织邀请结果不会被 team 判定冲掉。

---

## 七、xlsx 最小解析器

`.xlsx` 本质是个装了 XML 的 zip, 所以用标准库 `zipfile` + `xml.etree` 直接读, 不需要安装任何东西。
装了 `openpyxl` 就优先用它(日期等格式更完整), 没装也不影响。

`_read_xlsx_minimal` 处理的情况:

- `xl/sharedStrings.xml` 的共享字符串(`t="s"`, 值是下标)
- `t="inlineStr"` 内联字符串
- `t="b"` 布尔
- **跳格**: 单元格靠 `r="C3"` 定位, 缺失的列补空 —— 不能按出现顺序数, 否则整行错位
- 通过 `xl/workbook.xml` + `xl/_rels/workbook.xml.rels` 解析 `--sheet` 指定的工作表
- 完全空白的行直接丢掉(含末尾空行), 它们不是数据

csv 侧: 自动试 `utf-8-sig` → `utf-8` → `gb18030` → `cp936` → `latin-1`,
分隔符按第一行里 `,` `;` `\t` 出现次数取最多的。

---

## 八、参数解析里的一个坑

`--cols "-,email,username"` 这种写法, argparse 看到以 `-` 开头的值会以为那是另一个选项,
直接报 `expected one argument`。但「跳过第一列」天生就长这样, 所以 `normalize_argv()`
在解析前把 `--cols 值` 合并成 `--cols=值` —— 对本脚本而言两种写法完全等价。

另外, 命令行上直接写的用户名会**严格校验**格式并直接报错(笔误当场拦住),
而表格里的脏数据只给提醒不拦截(表格是别人填的, 拦了就没法处理)。两者刻意不对称。

---

## 九、隐私

**除 `token.txt` 外不出现任何敏感信息。**

- 代码里不硬编码 token, 运行时只打印 token 的**来源**(如「配置文件 token.txt」), 不打印内容。
- 脚本输出、README、ARCHITECTURE、示例文件里**没有真实姓名/邮箱/用户名**。
- `.gitignore` 挡住 `token.txt`、`.env`、`output/`, 以及 `*.xlsx` / `*.csv` / `*.tsv`  (收集表是最大的泄露源), 只放行 `docs/**` 下的示例模板。
- `output/` 里的结果文件含真实邮箱, 不要提交。

### 推送前自检

```bash
python _test/scan_pii.py            # 工作区这一版有没有敏感信息
python _test/scan_pii_history.py    # 整个历史有没有(删掉的文件也翻得出来)
```

两个都要跑, 因为它们看的**不是一个地方**: `scan_pii.py` 只查 `git ls-files`
(现在这一版), `scan_pii_history.py` 把 `rev-list --objects --all` 里所有可达 blob
倒出来查历史 —— **历史是跟着 push 一起走的**, 一个曾经提交过的真实姓名, 后来删掉了,
`git log -p` 照样翻得出来。

两个脚本都只报文件名/行号, **不回显命中的内容**, 免得扫描器自己成了新的泄露点。
词表放在不入库的 `.pii-terms` 里(它本身就是敏感信息), 所以干净克隆里这两个脚本
会以退出码 2 提示"没有词表", 属正常。

`scan_pii_history.py` 除了词表还会精确比对本机 `token.txt` 里的 token 值 ——
它是防"哪天不小心提交过一次"的。另外会把 `ghp_` / `github_pat_` 这类**长相**单独列一段
"参考", 不算失败: 测试和文档里本来就写着占位符, 一定会命中, 不能当结论。

---

## 十、离线自测

`_test/` 下是开发期的离线测试台, 覆盖旧项目 16 条踩坑。这些测试都**不碰网络、不发任何邀请**,
不含任何真实数据, 可以随时重跑:

```bash
python _test/run_offline_tests.py    # 51 项: 表头识别/--cols/--skip-rows/--no-header/csv 变体/xlsx/--limit/报错路径/汇总分区/--no-preflight 与 --execute 的冲突
python _test/run_output_tests.py     # 12 项: csv+jsonl 内容/续跑 BOM 坑/状态值不重名/422 归类/汇总的合计与差额
python _test/run_path_tests.py       # 22 项: 用假客户端驱动真实的 process_org_invites / process_team
python _test/run_example_tests.py     #  6 项: 验证 docs/examples/ 下的示例文件真的能被正确解析
python _test/run_prompt_tests.py      #  7 项: 交互提问(替换掉终端判断和 input, 模拟人的回答)
python _test/check_token_formats.py   #  验证 token.txt 的各种写法(裸 token / KEY=VALUE / 注释 / 引号)
python _test/run_module_tests.py      #  9 项: 结构体检(兼容层无逻辑/不反向依赖/无环/PROJECT_ROOT/导出面/状态表一致/状态归类不重不漏/两个入口等价)
python _test/scan_pii.py              #  扫被跟踪文件里的真实个人信息(词表在 .pii-terms, 不入库)
python _test/scan_pii_history.py      #  扫整个 git 历史(含 token.txt 里的 token 值), 推送前跑
```

`run_prompt_tests.py` 替换的是 **`g.prompt._stdin_is_interactive` / `g.prompt._ask_line`**
(即 `inviter/prompt.py` 里那两个名字), 不是兼容层上的同名属性 —— 原因见第二节约定 2。

`run_module_tests.py` 查的是**结构**而不是行为(行为由上面几套盯着): 它用 AST 确认兼容层里
没有 `def`/`class`、`inviter/` 不反向引用兼容层、没有两个模块重复定义同名顶层函数; 用子进程
逐个导入确认无循环导入; 并断言 `PROJECT_ROOT` 指在仓库根、默认输出目录是 `PROJECT_ROOT/output`、
三张状态表与状态常量一一对上(**搬家丢一个状态值会在这里当场现形**), 以及状态归类集合
两两不重叠、合起来覆盖全部状态(汇总行「不漏人不重复计数」的结构保证)。

### 夹具为什么不在版本库里

`.gitignore` 把 `*.csv` / `*.xlsx` 全挡住了(收集表是最大的泄露源), 所以**夹具不能入库**,
只能由 `_test/make_fixtures.py` 现场生成。用夹具的套件开头会调一次
`make_fixtures.ensure()`(缺什么补什么, 已存在的不动), **干净克隆直接跑就行, 不需要先手动准备**:

```bash
python _test/make_fixtures.py            # 一般不用跑, 套件自己会补; 想手动补也可以
python _test/make_fixtures.py --force    # 全部重造(改了夹具内容想重新比对时用)
```

> 这条规矩是踩出来的: `standard.csv` / `noheader.csv` / `semicolon.csv` 曾经只是本地手写的文件,
> 被 `*.csv` 挡住没入库, 于是**干净克隆里 45 项挂了 27 项**(全是「找不到表格文件」)。
> 现在三个 csv 也由 `make_fixtures.py` 生成(与当年手写内容**逐字节一致**),
> 并且 `run_offline_tests.py` 有一条断言盯着「这套用到的夹具都登记过」,
> 以后新增夹具忘了登记会当场失败, 不会再悄悄退化成只在作者机器上能跑。

`run_offline_tests.py` 最后会拿真实收集表 `uu们的GitHub用户名和邮箱开盒.xlsx` 只做**解析**
(不联网), 打印出它认到的列映射供人工确认 —— 这条会在该文件不存在时自动跳过。

`run_example_tests.py` 是 README 的保险: README 指向 `docs/examples/` 里的文件,
这一套保证那些文件**真的能用**, 免得文档写了一个跑不通的格式。

不想要这些测试文件, 整个删掉 `_test/` 即可, 不影响 `github_inviter.py`。

### `run_path_tests.py` 是干什么的

真实实跑里拿不到「邀请成功(2xx)」的样本(试跑的人恰好都已是成员), 所以这一套用一个
`FakeGitHub` 只实现 `user_id()` 和 `request()` 两个方法, **驱动真实的
`process_org_invites` / `process_team`**, 然后检查两件事:

1. **脚本真实发出的请求体对不对** —— 有用户名时必须是 `{"invitee_id": N, "role": ...}`
   且**不能**同时带 `email`; 只有邮箱时才是 `{"email": ..., "role": ...}`;
   已在 team 的人**一次 PUT 都不能发**(发了就会降级 maintainer)。
2. **各种响应码落到哪个状态** —— 201 → `invited`/`committed`, 422
   `already a part of this organization` → `already_member`, 无关的 422(如
   `Name already exists`)→ **必须判失败**, 403 / 404 → 带对应处理提示的 `failed`,
   dry-run → 零请求且状态为 `planned`。

另有一条端到端断言: 201 之后写出的 CSV/JSONL 正确, 且**续跑能索引到这个人**
(下次重跑会跳过, 不会重复打扰)。

---

## 十一、维护时的注意点

- **新能力加到 `inviter/` 下的对应模块**, 不要往 `github_inviter.py` 里写逻辑 ——
  它只是兼容层, 有 AST 检查盯着; 要暴露给外部就在它的 `__all__` 里补一行。
- 加/改 import 后跑 `python _test/run_module_tests.py`: 它逐个模块单独导入, 能抓住循环导入。
- 改状态值时, 同步改 `ORG_LABEL` / `TEAM_LABEL` / `REMEDY`, 并跑 `run_output_tests.py`
  与 `run_module_tests.py`(后者检查三张表的键集合与状态常量一致)。
- **加新状态时还要想清楚它属于哪个归类集合**(`ORG_OK` / `ORG_SKIP` / `ORG_PENDING` /
  `failed`)以及进不进汇总行的哪个桶 —— 忘了这一步, 汇总行会静默开始漏人或重复计数,
  而这正是「计划邀请 4 人 … 跳过 4 人」那个 bug 的成因。两个套件都会拦。
- 定位「脚本旁边」一律用 `constants.PROJECT_ROOT`, **别用 `Path(__file__)`** ——
  子模块里的 `__file__` 指向 `inviter/`。
- 改 `build_records` 后务必跑 `run_offline_tests.py` —— 归类顺序和备注是两个出过 bug 的地方。
- 加新接口时, `GitHub.request` 已经负责退避重试, 别在调用处再套一层 sleep(除搜索接口的
  30/分钟限速需要自己 `time.sleep(2)`)。
- `--delay` 默认 2 秒是有意的: 这个接口官方明确写了可能触发二级限流, 别为了快把它去掉。
- 拆模块这类纯搬家, 验证方法是**重构前先录一份输出快照**, 搬完逐字节 diff
  (`--help` 加几个 `--no-preflight` 的 dry-run 就够; 只归一化时间戳和耗时秒数)。
