---
name: github-org-team-invite
description: Use when running, troubleshooting, or verifying batch GitHub organization and team invitations with this repository's github_inviter.py — before a first real send, when invitations fail or land on the wrong people, when preflight disagrees with what the API says, when someone must be re-added to a team after accepting an org invite, or when reconciling an earlier run against the organization's real state.
---

# 批量邀请组织 / Team 的运行与排错

**目标是「没人被漏掉、也没有人被重复打扰」。** 这个脚本会用你的组织 owner 身份
真的发邀请邮件, 所以每一步都要先看计划再动手。

涉及的文件: 脚本 `github_inviter.py`, 结果 `output/results-*.csv|jsonl`,
设计细节 [ARCHITECTURE.md](../../../ARCHITECTURE.md), 使用者说明 [README.md](../../../README.md)。

## 铁律

1. **第一次对一份新表跑, 永远先 dry-run**(不加 `--execute`)。它对同一份表做同样的解析和预检,
   但一个请求都不发。
2. **先小批量**: `--execute --limit 3`, 确认这几个人真收到邮件再全量。
3. **`--limit N` 的语义是「表里第 1~N 行」**, 不是「发成功 N 个」。
   这 N 行里如果有重复行、空行、或上次已成功过的人, 实际发出去的会**少于** N 个 —— 这是对的。
   要看还剩多少人没处理, 读汇总里的「跳过(--limit 截断)」那一行。
4. **不要在批量中途改表**。续跑靠 `output/results-*.jsonl` 里的邮箱/用户名做键;
   表变了、键对不上, 就可能重复发。
5. **别删 `output/`** —— 删了续跑就失去记忆, 重跑会重复打扰已经邀请过的人。
6. **`token.txt` 之外不要在任何地方落盘 token**, 也不要把 `output/` 或收集表提交进版本库。

## 标准流程

```sh
# 1. 环境与权限: 确认 token 有效、组织名对、看当前状态(只读)
python github_inviter.py --org <org> --status

# 2. 看计划: 认到的列、要邀请谁、排除了谁、为什么
python github_inviter.py --org <org> "<表.xlsx>"

# 3. 小批量真发
python github_inviter.py --org <org> "<表.xlsx>" --execute --limit 3

# 4. 抽查这几个人是否真收到(见下方「确认邀请真的发出去了」)

# 5. 全量
python github_inviter.py --org <org> "<表.xlsx>" --execute
```

带 team 时在第 2 步起就加上 `--team "<team 名或 slug>"`。

## 看计划时要盯的三件事

1. **列映射对不对**。打印的 `列映射: 邮箱=<...> 用户名=<...> 姓名=<...>` 如果指错了列,
   先停下, 用 `--cols` 按位置重指(位置从 **1** 开始, 不用的列写 `-`):
   `--cols "name,email,username"`。**不要**在列映射错的情况下 `--execute`。
2. **排除行和提醒**。`! 第 N 行: ...` 是提醒不是失败。特别注意:
   - 「邮箱列里写的不是邮箱, 当用户名用了」→ 那一行会被当成用户名邀请, 确认是不是本人;
   - 「用户名格式可疑」→ GitHub 用户名只允许字母数字和单个连字符, `wu_shi` 这种填错了;
   - 「与第 N 行是同一个人」→ 去重, 只发一次。
3. **`已是组织成员` 的数量**。如果是全表, 说明这份表已经很旧了。

## 确认邀请真的发出去了

**dry-run 的「计划邀请」不等于发出去了**, 而且**发出邀请也不等于对方点得动**。三步:

```sh
# 1) 过几分钟, 看组织当前的待处理邀请(只读)
python github_inviter.py --org <org> --status
#    预期: 待处理邀请的数量 ≈ 你刚发出的数量

# 2) 看本次结果文件
#    output/results-<时间戳>.csv -> 按姓名逐行看状态
#    「组织邀请已发出」= 接口回了 2xx

# 3) 让对方确认收到并点得动
```

### 按邮箱邀请有个硬限制

只有该邮箱是对方 GitHub 账号上**已验证**的邮箱, 他才点得动邀请链接。
**没验证时接口照样回 201(看起来成功)**, 对方却进不来, GitHub 会把这类记进
**failed invitations**。所以只有邮箱的行, 事后必须用 `--status` 对一遍:

- 出现在 failed invitations 且原因是 `Invitation expired` → 7 天没接受, 需要重发;
- 别的失败原因 → 让对方把该邮箱加到 GitHub 账号并验证, **或者改用用户名邀请**。

**能拿到用户名就优先用用户名**(脚本默认就这么做), 这条路可靠得多。

## 常见症状 → 处理

| 症状 | 真正的意思 | 处理 |
|---|---|---|
| `用户名不存在` | **表里那个用户名在 GitHub 上查不到**（`GET /users/{login}` 404）。对方永远接受不了邀请，脚本也不会白试 | 找本人核对拼写。**先试相邻两三个字符打反的情况**——实测两例都是这种换位：`u5er_na3e`→`user_name`（h/j 打反）、`usr_naem`→`usr_name`（a/c 打反） |
| 你在 GitHub 网页搜那个用户名, **能搜到 1 条** | 不一定是它存在。GitHub 网页搜索是**模糊/全文**匹配: 精确查 `q=<名字> in:login` 可能是 **0 结果**, 那 1 条是近似结果, 而且网页会把**你输入的词**加粗高亮, 看起来像账号名 | 以 `GET /users/{login}` 的 200/404 为准; 或点进结果看地址栏的真实登录名 |
| `已是组织成员` | 接口回 422 `already a part of this organization`, **没有给对方发邮件** | 不用管, 已正确跳过 |
| 预检说「不在组织里」, 发邀请却回「已是成员」 | 这一行的**用户名和邮箱不是同一个人**: 用户名在 GitHub 上不存在(`GET /users/{login}` 404), 邮箱却属于某个已在组织的账号 | 核对这一行, 问本人要准确的用户名; 不必重发 |
| 一批人 `缺用户名, 加不了 team` | 表里只有邮箱 —— team 接口只收用户名 | 补一列 GitHub 用户名再重跑 |
| 一批人 `待对方接受组织邀请` | 人还不是组织成员, 现在加不进 team | 等对方接受, **重跑同一条命令**即自动补进 team。**但先看下一行** |
| `404: 组织名写错, 或 token 不是这个组织的 owner` | 组织 slug 错 / token 无权 | 核对 slug 与 token 权限 |
| `403 权限不足` | 细粒度 token 缺 Members 写权限, 或**没被组织批准**; 经典 token 缺 `admin:org` | 补权限 / 去组织 Settings → Personal access tokens 批准 |
| 满屏重试、`SSL: UNEXPECTED_EOF_WHILE_READING` | **系统代理**在捣乱 | 关掉系统代理(实测直连 100%, 走代理 42%) |
| `找不到 team: xxx` | 名字写错 | 报错里会列出全部 team |
| 有人明明已加入却显示待接受 | 邀请被接受后会同时退出 pending 和 failed 两份清单 | 重跑 `--status` 看当前真实状态, 别信旧的结果文件 |

## team 这一步的安全设计(别绕过)
- **已在 team 里的人一律跳过, 绝不重设角色**。GitHub 的 team 成员接口是 PUT 语义,
  无脑重设会把 team 里的 **maintainer 降级成 member**。脚本靠「先查后写」避免这件事。
- 想给**新成员**设成 maintainer 才用 `--team-role maintainer`; 它只影响这次新增的人。

## 事后补加 team

对方接受组织邀请之后, 不需要重新整理名单, 直接对同一份输入重跑:

```sh
python github_inviter.py --org <org> "<表.xlsx>" --team "<team>" --execute
```

上次已成功的人会被续跑跳过, 只有新成为组织成员的人会被补进 team。

## 什么时候必须停下来问人

- 列映射看起来不对, 而你不确定该用哪个 `--cols`;
- 计划里出现了意料之外的人, 或某一行「用户名和邮箱对不上」;
- 准备对**全量**数据执行 `--execute`(先确认小批量那一步真的成功了);
- 有人反馈收到了不该收到的邀请(可能意味着表里有脏数据或重复的人)。
