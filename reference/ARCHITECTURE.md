# 架构与实现说明

> 这份文档面向**维护者与 AI**：记录设计取舍、接口细节、边界情况和历史踩坑。
> 日常使用请看 [README.md](README.md)。

本目录两个脚本，共用同一套 token 解析与 HTTP 客户端：

| 脚本 | 作用 |
|---|---|
| `invite_org_members.py` | 读数据收集表，按**邮箱**（或 GitHub 用户名）批量给人发**组织邀请**。也是 HTTP 客户端、表格读取、token 解析的所在地 |
| `add_team_members.py` | 把组织成员批量加入指定 **Team**（如 `NEUP 2026`），支持只加指定名单。依赖前者 |

下面**第一～八节**讲邀请脚本，**第九节**讲 Team 脚本。

用的是 GitHub 官方接口 [`POST /orgs/{org}/invitations`](https://docs.github.com/en/rest/orgs/members#create-an-organization-invitation)，
脚本本身**不发邮件**——邀请邮件由 GitHub 发出。

> **默认是 dry-run**：不加 `--execute` 绝不会发出任何邀请，只会解析表格、做预检、生成计划文件。

---

## ⚠️ 先看这一条：按邮箱邀请有个硬限制

GitHub 官方文档（[Inviting users to join your organization](https://docs.github.com/en/organizations/managing-membership-in-your-organization/inviting-users-to-join-your-organization)）写得很明确：

> If you use an email address for the invitation, the invitee will only be able to accept the invitation
> **if the email address matches with a verified email address associated with the invitee's personal account**.

也就是说：**只有当这个邮箱是对方 GitHub 账号上「已验证」的邮箱时，对方才点得动邀请链接。**

后果是——如果对方根本没注册 GitHub，或者表里填的是 QQ 邮箱 / 学校邮箱而他的 GitHub 账号绑的是另一个邮箱，
接口**大概率仍然返回 201（看起来成功）**，但对方点链接进不来。GitHub 会把这类邀请单独记进
**failed invitations** 清单，用 `--report` 就能看到失败原因。

所以：

- 表里**有 GitHub 用户名**的话，脚本默认**优先用用户名**换 user id 再邀请（走 `invitee_id`），这条路径可靠得多；
- 只有邮箱的，做好「一部分邀请实际无法被接受」的心理准备，跑完一定用 `--report` 对一遍；
- 如果这份表是问卷收来的、你自己也不确定对方有没有 GitHub 账号，**先拿 3~5 条试**，别一上来全量。

---

## 一、准备工作

### 1. 你必须先是组织 owner

接口要求调用者是组织 owner，否则 404/403。

### 2. 建一个有权限的 token

两种都行，**推荐细粒度 token**：

| 类型 | 需要什么 |
|---|---|
| 细粒度 PAT（推荐） | Organization permissions → **Members: Read and write** |
| 经典 PAT | 勾 **`admin:org`** |

细粒度 token 还要注意：**组织那边必须批准**这个 token（组织 Settings → Personal access tokens → Pending requests），
没批准的话调用会一直失败。

### 3. 让脚本拿到 token

三种方式，**优先级从高到低**（脚本会打印它用的是哪个）。

#### 方式 A（推荐）：建一个 `token.txt`

在 `GitHub-Org-Inviter/` 目录里新建 `token.txt`，写一行就行：

```
GITHUB_TOKEN=github_pat_xxx
```

不用每次设环境变量，也不会留在命令历史里。

这个文件已经被 `.gitignore` 排除，不会误提交。

#### 方式 B：环境变量

```bash
# Git Bash
export GITHUB_TOKEN="github_pat_xxx"
```

```powershell
# PowerShell
$env:GITHUB_TOKEN = "github_pat_xxx"
```

只对当前窗口有效。Git Bash 想每次开窗口都自动生效，把 `export` 那行加到 `~/.bashrc`；
PowerShell 想长期生效用 `setx GITHUB_TOKEN "github_pat_xxx"`（新开窗口才生效）。

临时用一次也行，不用 export：

```bash
GITHUB_TOKEN=github_pat_xxx python invite_org_members.py --org my-org --input data.xlsx
```

#### 方式 C：`--token` 参数（不推荐）

能用，但 token 会留在 shell 历史里。

> 💡 **不建议直接把 token 写死在 `invite_org_members.py` 里面**：
> 那样它就跟脚本绑死了，脚本一旦分享、备份、或者哪天传上 GitHub，token 就一起泄露出去了。
> `token.txt` 的便利程度完全一样，但能单独排除、随时换、也不会污染代码。

---

## 二、表格长什么样

支持 **.csv / .tsv / .xlsx**，第一行是表头。列名脚本会自动认，认这些（大小写、空格、下划线都无所谓）：

| 用途 | 认得的表头 |
|---|---|
| 邮箱（必需其一） | `邮箱` `电子邮箱` `邮箱地址` `邮件` `email` `e-mail` `mail` |
| GitHub 用户名 | `GitHub 用户名` `用户名` `账号` `github` `username` `login` |
| 姓名（仅记录用） | `姓名` `名字` `昵称` `name` `nickname` |

示例见 [`emails.example.csv`](emails.example.csv)。

### 列是怎么定下来的（三步）

1. **`detect_columns(header)`** —— 按表头关键字认，用的是上面那张对照表。
2. **`--cols` / `--email-col` / `--username-col`** —— 用户显式指定，覆盖第 1 步。
   `parse_cols_spec("username,email")` 按**位置**解析（列表下标 0-based，语义是"第几列"），
   支持 `username` / `email` / `name` 及中文别名，写 `-` 表示跳过该位置。
   指到超出表宽的列会 `die` 并提示，**不静默变空**。
3. **位置兜底** —— 表头一个都没认出来时，按约定取第 1 列=用户名、第 2 列=邮箱，
   并打印一行提示告诉用户可以用 `--cols` 改。

第 3 步原本是"按单元格内容猜列"（`guess_columns_from_data`），后来删掉了：
把列的语义交给使用者的显式约定，比让脚本猜更可预测 —— 英文昵称（如 `alice`）
本来就会同时像用户名，猜错的代价是静默处理错人。

### `classify_cell()`：兜住"列里放错东西"

定好列之后，`build_records()` 不会盲目信任列的内容 —— 它把**两个列**都过一遍
`classify_cell(value)`，按内容把值归到 email 或 login：

```
classify_cell(value):
    能解析出合法邮箱        -> (email, "")
    否则能解析出合法用户名  -> ("", login)
    否则                    -> ("", "")
```

合并时**各自优先信本职列**：

```python
email = email_a or email_b   # 邮箱列优先
login = login_b or login_a   # 用户名列优先
```

这个顺序是有意为之。曾经写成 `login = login_a or login_b`，结果
`emails.example.csv` 第 8 行邮箱列里填的 `not-an-email` 虽然不是邮箱、
却恰好是合法用户名格式，把用户名列里真正的 `sunba` 盖掉了。**本职列优先**才修好。

### 脏数据处理

- 邮箱大小写混写 → 统一小写（GitHub 不区分大小写）
- 单元格里写了多个邮箱 → 只取第一个，并给出提示
- 用户名写成 `https://github.com/xxx`、`github.com/xxx`、或 `@xxx` → 自动提取 `xxx`
- 重复行 → 自动去重，保留第一次出现的
- 空行、没有邮箱也没有用户名的行 → 排除并列出原因
- 用户名格式可疑（比如带下划线的 `wu_shi`，GitHub 用户名不允许下划线）→ 给提示

列名不在上面的清单里，就手动指定：

```bash
python invite_org_members.py --org my-org --input data.xlsx --email-col "工作邮箱" --username-col "GitHub链接"
```

---

## 三、跑起来的三个步骤

> 下面的命令在 **PowerShell、Git Bash、cmd** 里都能直接跑（已在 Git Bash 实测，含中文文件名）。
> 唯一有区别的是设环境变量，见上一节；用 `token.txt` 的话连这个区别都没有。

### 第 1 步：dry-run 看计划

```powershell
python invite_org_members.py --org my-org --input data.xlsx
```

输出会有：识别到的列、解析到的行数、排除/警告明细、本次要邀请谁，
计划写到 `output/plan-<时间戳>.csv`。**这一步不会发任何邀请。**

如果连 token 都还没配，加 `--no-preflight` 也能先看解析结果：

```powershell
python invite_org_members.py --org my-org --input data.xlsx --no-preflight
```

（`--preflight` 会去查组织已有成员和待处理邀请，跳过重复的，所以正式跑之前建议带上 token 再 dry-run 一次。）

### 第 2 步：小批量试跑

```powershell
python invite_org_members.py --org my-org --input data.xlsx --execute --limit 3
```

`--limit 3` 只发前 3 条。确认这 3 个人**真的收到了邀请邮件**、且能点进组织之后，再往下走。

### 第 3 步：全量

```powershell
python invite_org_members.py --org my-org --input data.xlsx --execute
```

跑到一半断了不要紧，**重跑会自动跳过已经成功邀请过的**（结果文件在，默认续跑）。
想强制重来加 `--no-resume`——注意那会重复发送。

### 跑完对一遍

**等几分钟**再跑（GitHub 处理邀请有延迟）：

```powershell
python invite_org_members.py --org my-org --input data.xlsx --report
```

它会逐条告诉你每个人现在是什么状态：邀请已发出 / 邀请失败（带原因）/ 已经是成员。
失败的邮箱就是「对方接受不了」的那批，需要换 GitHub 用户名再邀请，
或者让对方先把邮箱加到 GitHub 账号上并验证。详见[第四节](#四有人已经在组织里了怎么办)。

---

## 四、有人已经在组织里了，怎么办

这是最常见的情况：人已经在组织里，又填了一次表。

### 结论先说：GitHub 会直接告诉你，不用猜

**实测确认**：向一个已经是成员的人发邀请，接口会返回 `422`，并给出明确原因：

```
A user with this email address is already a part of this organization
```

而且**不会给对方发任何邮件**。所以：

- 对方不会被打扰
- 脚本会把它记成 `already_member`，并在结果里保留这句原文
- 你事后看结果文件就知道是谁

也就是说，**这件事基本不需要额外操心**，正常发就行。

### 为什么预检拦不住（但不影响结果）

预检是靠 `GET /orgs/{org}/members` 拿已有成员来比对的，而这个接口的响应里**只有 `login`，没有 `email`**（[官方文档](https://docs.github.com/en/rest/orgs/members#list-organization-members)的响应样例可确认）。
表里只有邮箱时两边对不上，所以预检看不出"这人已经在里面了"——但这没关系，真正发的时候 GitHub 会兜住。

### 想真正提前拦下来：把邮箱反查成用户名 ⭐

```powershell
python invite_org_members.py --org my-org --input data.xlsx --resolve-emails
```

它用 GitHub 搜索接口按邮箱反查账号，一旦命中就能同时拿到两个好处：

- 拿登录名去比成员列表，**在发送之前就精确认出已经在组织里的人**，直接跳过
- 改走**用户名邀请**，绕开「邮箱必须是已验证邮箱」那个坑

只对「在 GitHub 上公开了邮箱」的账号有效，所以命中率有限——实测一份 12 人的表命中了 3 个，
其中恰好有一个是已经在组织里的、还有一个发出去几分钟就接受加入了。
成本很低：搜索接口限速 30 次/分钟，12 条大约 25 秒。

**建议每次都带上这个参数**，命中的都是白赚的，没命中也不影响原本的流程。

### 用对账看清最终结果

```powershell
python invite_org_members.py --org my-org --input data.xlsx --resolve-emails --report
```

它把「我们实际发过什么」（读 `output/results-*.jsonl`）和「组织当前真实状态」对起来：

| 判定 | 含义 |
|---|---|
| 已经是组织成员 | 接口明确回了 422，或在成员列表里 |
| 邀请已发出 | 邀请在待处理列表里，等对方接受 |
| 已经加入 | 对方已经接受了邀请 |
| 邀请失败 | 失败清单里有它，附原因 |
| **未处理** | **还没发过**（比如被 `--limit` 截断了）——不是已经是成员 |

> ⚠️ 「未处理」和「已经是成员」是两件完全不同的事。早期版本的对账会把没发过的行误判成
> 「已经是成员（推测）」，这个错误已经修掉：现在对账会先读发送记录，没发过的就老实说没发过。

### 顺带一提：邀请会过期

失败清单里可能看到历史遗留的：

```
Invitation expired. User did not accept this invite for 7 days
```

**组织邀请的有效期是 7 天**，过期就作废了。所以发完别拖太久，到期前催一下没接受的人。
这类过期记录和组织当前状态无关，脚本会把它们单独列出来，不会跟你表里的人混在一起。

### 能提前拦一点是一点（可选）

```powershell
python invite_org_members.py --org my-org --input data.xlsx --execute --resolve-member-emails
```

加这个参数，预检时会逐个读成员的公开资料，把公开了邮箱的人先挑出来跳过。
缺点：大部分人资料里没公开邮箱，命中率一般；组织人多时还会明显变慢（每人一次请求）。

### 最省事的办法

如果你们组织不大，直接在 GitHub 网页版的成员列表里扫一眼，把已经在里面的人从表里标出来，
比任何自动化都快。**12 个人的规模，人工核对一次可能就 2 分钟。**

---

## 五、参数速查

| 参数 | 说明 |
|---|---|
| `--org` | **必填**，组织名（URL 里那个 slug） |
| `--input` | **必填**，数据表路径 |
| `--execute` | 真正发送。**不加就是 dry-run** |
| `--limit N` | 本次最多处理 N 条，建议首次用 3~5 |
| `--delay 2.0` | 每条之间间隔秒数，默认 2 秒。**别调太小** |
| `--role` | `direct_member`（默认）/ `admin` / `billing_manager` |
| `--team-ids 12,26` | 邀请的同时加入指定 team |
| `--email-col` / `--username-col` | 手动指定列名 |
| `--sheet 工作表名` | xlsx 指定工作表，默认第一个 |
| `--prefer-email` | 强制只按邮箱邀请（默认是有用户名就优先用用户名） |
| `--no-preflight` | 跳过预检（也就不用 token，只能 dry-run） |
| `--no-resume` | 忽略历史结果，重跑全部 |
| `--report` | 对账模式。读 `output/results-*.jsonl` 的发送记录，再比对组织当前状态，逐条给出判定 |
| `--resolve-member-emails` | 预检时尝试读成员公开邮箱，提前认出已在组织里的人（较慢） |
| `--resolve-emails` | 用搜索接口把邮箱反查成 GitHub 用户名（命中就能精确判断是否已在组织内，并改走用户名邀请） |
| `--out-dir` | 结果输出目录，默认 `output/` |

---

## 六、结果文件

都在 `output/` 下，按时间戳分次：

- `plan-<时间戳>.csv` — dry-run 的计划（要邀请谁、排除了谁、为什么）
- `results-<时间戳>.csv` — 执行结果，每行含状态 / HTTP 码 / 详情 / 时间，**可以直接用 Excel 打开**
- `results-<时间戳>.jsonl` — 同上，逐行 JSON，方便再处理；**续跑就是靠读这个**
- `verify-<时间戳>.csv` — 对账结果（`--report --input` 生成）

状态含义：

| 状态 | 含义 |
|---|---|
| `invited` | 邀请已发出（接口 201） |
| `already_member` | 已经是组织成员，跳过 |
| `already_invited` | 已有待处理邀请，跳过 |
| `skipped_duplicate` / `skipped_invalid` | 表内重复 / 无可用信息 |
| `failed` | 接口拒绝，看 `detail` |
| `error` | 网络或异常 |

---

## 七、踩坑提醒

**`422 Validation failed`**
常见于：邮箱格式不合法、该用户已在组织里、或者**短时间发太多被判定为刷**。
脚本已经按响应里的 `Retry-After` 和二级限流做了退避重试，但还是把 `--delay` 调大些（比如 3~5 秒）更稳。

**`404 Resource not found`**
组织名写错，或者 token 不是这个组织的 owner。

**`403` / `Bad credentials`**
token 无效、过期、没勾 `admin:org` / Members 写权限，或者细粒度 token 还没被组织批准。

**组织开了 2FA 强制要求**
那对方必须自己开启 2FA 才能加入，这跟脚本无关，邀请会卡在对方那边。

**邀请会过期**
组织邀请链接有有效期，过期后对方就点不了了。用 `--report` 看还剩哪些 pending，需要的话重发。

**别把 token 写进脚本或提交到 git**

**接口是按"创建内容"限流的**
官方对这个接口明确写了可能触发 secondary rate limit。别去掉 `--delay`。

---

## 八、脚本依赖

**零依赖**，Python 3.10+ 标准库就能跑，CSV 和 `.xlsx` 都直接读。

.xlsx 是用内置的解析器读的（xlsx 本质是个装 XML 的 zip 包）。如果你环境里装了 `openpyxl`，
脚本会优先用它（对日期等格式处理更完整）；没装也照样能跑，不会让你去 `pip install`。

---

## 九、把成员拉进 Team（`add_team_members.py`）

把组织成员批量加入某个 team。

接口是 [`PUT /orgs/{org}/teams/{team_slug}/memberships/{username}`](https://docs.github.com/en/rest/teams/members#add-or-update-team-membership-for-a-user)，
请求体 `{"role": "member"}`。调用者需要是**组织 owner** 或**该 team 的 maintainer**。

### 三种用法

**1. 用一份名单（最常用）**

名单可以是文件，也可以直接写在命令里：

```bash
# 名单文件：每行一个用户名
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" --users "名单.txt"

# 或者直接逗号分隔
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" --users "alice,bob,carol"
```

名单文件每行**只取第一个**逗号/分号/制表符/多空格分隔的字段，
所以 `登录名,邮箱,备注` 这种一行多列的表也能直接丢进去。空行和 `#` 开头的行会跳过，重复项自动去重。

**2. 用数据收集表**

```bash
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" \
  --from-table "友友们的github账户开盒.xlsx"
```

表里只有邮箱的行会自动反查用户名（和邀请脚本同一套逻辑）。

**3. 组织全体成员** —— 不加筛选就是全部，**但请先看下面的警告**

```bash
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026"
```

### 三条安全设计

**① 已在 team 里的人一律跳过，绝不重设角色** ⭐

这条最关键。team 里如果有 maintainer，被重新 `PUT` 成 `member` 就会**被降级**。
脚本先读现有成员名单，凡已在 team 里的直接跳过，不碰他们的角色。

（真实案例：这个 team 里当时有 2 位 maintainer。脚本若无脑重设角色，他们会当场被降级。）

**② 自动检测"误伤往届"** ⭐

team 名形如 `XXX 2026` 时，脚本会自动找出同系列的 `XXX 2025 / 2021 / 2018 / 2017`，
统计你的名单里有多少人已经在往届 team 里——那多半是学长学姐，不该拉进本届。

> 实测数据：`NEUP 2026` 现有 7 人，组织共 113 人，其中 **57 人属于往届 team**。
> 直接把组织全体成员塞进去，会新增 106 人，**其中 51 位是往届成员**。

要避开就往命令里加：

```bash
--exclude-teams "NEUP 2025,NEUP 2021,NEUP 2018,NEUP 2017"
```

**③ 非组织成员会被挡下并明确报告**

只有在组织里的人才能进 team。刚发出邀请还没接受的，脚本会直接告诉你"还不在组织里"。

### 默认 dry-run

不加 `--execute` 只看计划、不碰任何状态，计划写到 `output/team-plan-*.csv`。

### 参数速查

| 参数 | 说明 |
|---|---|
| `--org` | 组织名（必填） |
| `--team` | team 名称或 slug（必填），如 `"NEUP 2026"` |
| `--users` | 名单：逗号分隔的用户名，**或名单文件路径** |
| `--from-table` | 改用数据收集表（自动反查邮箱） |
| `--exclude-teams` | 排除这些 team 的成员，如 `"NEUP 2025,NEUP 2021"` |
| `--role` | `member`（默认）/ `maintainer` |
| `--limit` | 本次最多加几人（0=不限） |
| `--delay` | 调用间隔秒数，默认 1.0 |
| `--no-year-check` | 跳过往届误伤检测 |
| `--execute` | 真正执行 |

token 机制与邀请脚本完全一致：`--token` > 环境变量 `GITHUB_TOKEN`/`GH_TOKEN` > `token.txt`。

### 依赖

`add_team_members.py` **需要和 `invite_org_members.py` 放在同一目录**——它复用后者的
HTTP 客户端、限流重试和 token 解析，避免两份实现走偏。少一个文件会直接提示。
