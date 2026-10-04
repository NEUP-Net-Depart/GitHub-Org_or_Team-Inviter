# 架构与实现说明

> 面向**维护者与 AI**: 记录设计取舍、接口细节、边界情况和踩坑。
> 日常使用请看 [README.md](README.md)。

全部逻辑在单文件 `github_inviter.py` 里(约 1800 行, **零第三方依赖**, Python 3.10+)。
脚本本身**不发邮件** —— 邀请邮件由 GitHub 发出。

```
输入解析 ──► 记录构造 ──► 预检 ──► 第1档: 组织邀请 ──► 第2档: team ──► 打印 + 落盘
(read_table)  (build_records) (members/       (POST /orgs/{org}     (PUT .../teams/   (csv+jsonl)
                              invitations)     /invitations)         {slug}/memberships)
```

---

## 一、必须先知道的硬限制

这些全部来自实测(见 [dsh/logs](dsh/logs)), 直接决定了上面的设计:

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

| 区段 | 关键函数 | 职责 |
|---|---|---|
| 1 通用 | `log/die/pad/clip/display_width` | 输出与中文按 2 列宽对齐 |
| 2 读表 | `read_table` / `_read_xlsx_minimal` / `_read_delimited` | csv/tsv/xlsx/stdin, 零依赖 |
| 3 列识别 | `detect_columns` / `locate_header` / `parse_cols_spec` | 三步定列 |
| 4 记录 | `clean_email` / `clean_login` / `classify_cell` / `build_records` | 清洗 + 按内容归类 |
| 5 输入 | `resolve_input` / `records_from_tokens` / `_rows_to_records` | 命令行/文件/stdin 三种来源 |
| 6 API | `GitHub` | 限流退避、分页、各接口封装 |
| 7 token | `resolve_token` | `--token` > 环境变量 > `token.txt` |
| 8 结果模型 | `Outcome` / `*_LABEL` / `REMEDY` | 状态码与「原因→处理措施」映射 |
| 9 打印 | `print_results` / `print_summary` / `print_issues` / `print_retry_list` | 控制台输出 |
| 10 续跑 | `load_history` | 读历史 `results-*.jsonl` |
| 11 主流程 | `main` / `process_org_invites` / `process_team` / `write_outputs` | 串起来 |

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
3. **位置兜底** —— 一个都没认出来时, 第 1 列用户名、第 2 列邮箱, 并打印提示。

第三步原本是「按单元格内容猜列」, 后来删掉了: 英文昵称(如 `alice`)本来就同时像用户名,
猜错的代价是**静默处理错人**。把列的语义交给用户显式约定更可预测。

另外有个自动识别: **单列文件**(或 `.txt`)按「每行一个用户名/邮箱」处理, 不做表头识别,
免得第一行的人被当成表头吃掉。每行只取第一个逗号/制表符/分号分隔的字段,
所以整行粘过来的 `用户名,邮箱,备注` 也能用。

---

## 五、两档动作的判定

### 第 1 档: 组织邀请

判定顺序(命中即停):

```
在成员列表里                      -> already_member (跳过)
邮箱/用户名 在待处理邀请里        -> already_invited (跳过)
历史 results-*.jsonl 里已成功过   -> skipped_resumed (跳过, --no-resume 可关)
--no-preflight                    -> planned (状态不明)
dry-run                           -> planned
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

`--no-preflight` 时状态是「不知道」, 用独立的 `planned` 状态, 绝不冒充「已发出」;
即使配了 `--execute` 也不会发请求(没预检就发等于盲发)。

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
- `.gitignore` 挡住 `token.txt`、`.env`、`output/`, 以及 `*.xlsx` / `*.csv` / `*.tsv`
  (收集表是最大的泄露源), 只放行 `emails.example.csv` 和 `users.example.txt`。
- `output/` 里的结果文件含真实邮箱, 不要提交。

---

## 十、离线自测

`_test/` 下是开发期的离线测试台, 覆盖旧项目 16 条踩坑。三套测试都**不碰网络、不发任何邀请**,
不含任何真实数据, 可以随时重跑:

```bash
python _test/make_fixtures.py        # 造最小 xlsx(两张表/内联字符串/跳格/空行) + BOM csv
python _test/run_offline_tests.py    # 40 项: 表头识别/--cols/csv 变体/xlsx/命令行/stdin/--limit/报错路径
python _test/run_output_tests.py     # 11 项: csv+jsonl 内容/续跑 BOM 坑/状态值不重名/422 归类
python _test/run_path_tests.py       # 17 项: 用假客户端驱动真实的 process_org_invites / process_team
```

`run_offline_tests.py` 最后会拿真实收集表 `uu们的GitHub用户名和邮箱开盒.xlsx` 只做**解析**
(不联网), 打印出它认到的列映射供人工确认 —— 这条会在该文件不存在时自动跳过。

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

- 改状态值时, 同步改 `ORG_LABEL` / `TEAM_LABEL` / `REMEDY`, 并跑 `run_output_tests.py`。
- 改 `build_records` 后务必跑 `run_offline_tests.py` —— 归类顺序和备注是两个出过 bug 的地方。
- 加新接口时, `GitHub.request` 已经负责退避重试, 别在调用处再套一层 sleep(除搜索接口的
  30/分钟限速需要自己 `time.sleep(2)`)。
- `--delay` 默认 2 秒是有意的: 这个接口官方明确写了可能触发二级限流, 别为了快把它去掉。
