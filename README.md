# GitHub 组织 / Team 批量邀请

一个脚本, 一份名单, 两件事: **批量发组织邀请**, 顺手**把人加进 Team**。

## 使用前提

1. **你自己得是这个组织的 owner**(或至少是该 Team 的 maintainer)。
2. **跑之前关掉系统代理**(Clash / v2ray 之类的「系统代理」开关)。
   这是实测过的: 走系统代理成功率 42%, 直连 100%。代理开着时典型症状是满屏
   重试, 
   或者 `SSL: UNEXPECTED_EOF_WHILE_READING`。
3. 建一个 token, 然后**创建脚本同目录的 `token.txt`**，并将token直接写入第一行，其他什么都不要有。

   | token 类型 | 需要的权限 |
   |---|---|
   | 细粒度 (推荐) | Organization permissions → **Members** → Read and write |
   | 经典 | `admin:org` |

   > 细粒度 token 还要组织那边**批准**一下: 组织 Settings → Personal access tokens。
   > 没批准的话调用会一直失败。

**默认是 dry-run** —— 不加 `--execute` 绝不发出任何邀请, 只解析名单、做预检、打印计划。

## 命令

```bash
# 0. 先看一眼组织现状(只读, 不改任何东西)
python github_inviter.py --org <组织名> --status

# 1. 看计划(dry-run, 不发东西)
python github_inviter.py --org <组织名> "<收集表.xlsx>"

# 2. 试跑 3 个
python github_inviter.py --org <组织名> "<收集表.xlsx>" --execute --limit 3

# 3. 确认没问题, 全量执行
python github_inviter.py --org <组织名> "<收集表.xlsx>" --execute
```

### 输入可以怎么写

```bash
# 直接写用户名/邮箱(自动分辨; @前缀和 github.com/xxx 链接都认)
python github_inviter.py --org my-org alice bob@example.com
python github_inviter.py --org my-org @alice https://github.com/bob

# 一张表: .xlsx / .csv / .tsv
python github_inviter.py --org my-org "收集表.xlsx"

# 每行一个用户名的名单(.txt), 或从标准输入喂
python github_inviter.py --org my-org 名单.txt
cat 名单.csv | python github_inviter.py --org my-org -
```

xlsx/csv 默认**先按表头认列**: `GitHub用户名` / `用户名` / `账号` / `username`、
`邮箱` / `email` / `mail`、`姓名` / `name` / `昵称` 这些都认。
认不出来就按约定位置: **第 1 列用户名, 第 2 列邮箱**。

列顺序不一样时用 `--cols` 说明(**位置从 1 开始数**, 不想用的列写 `-`):

```bash
--cols "username,email,name"    # 第1列用户名、第2列邮箱、第3列姓名
--cols "email,username"         # 第1列邮箱、第2列用户名
--cols "-,email,username"       # 跳过第1列
--cols "name,email,username"    # 收集表常见顺序
```

### 把人加进 Team

加 `--team`, 就会在发组织邀请的同时, 把**已经是组织成员**的人加进这个 team:

```bash
# 看计划
python github_inviter.py --org <my-org> "<收集表.xlsx>" --team "<my-team>"

# 执行
python github_inviter.py --org <my-org> "<收集表.xlsx>" --team "<my-team>" --execute
```

**没接受组织邀请的人, 这次不会去动 team** —— 因为人还不是组织成员时加不进去。
他们会被列进「待重跑名单」, 等对方点了接受, **再跑一次同一条命令**就自动补进 team。

已在 team 里的人一律跳过, **不会改动他们的 team 角色**(否则 maintainer 会被降级成 member)。

### 常用选项

| 选项 | 说明 |
|---|---|
| `--execute` | 真正发送。**不加就是 dry-run** |
| `--limit N` | 本次最多处理 N 条, 首次建议 3~5 |
| `--delay S` | 每条之间的间隔秒数, 默认 2.0。别调太小, 会触发限流 |
| `--status` | 只读: 看组织成员数、待处理邀请、失败邀请(含原因) |
| `--team "名字"` | 同时把人加进这个 team |
| `--cols` / `--email-col` / `--username-col` | 指定列 |
| `--sheet 名字` | xlsx 指定工作表, 默认第一个 |
| `--resolve-emails` | 先用搜索接口把邮箱反查成用户名, 命中就更可靠(慢一些) |
| `--prefer-email` | 强制只按邮箱邀请 |
| `--no-resume` | 忽略历史结果重跑全部(**会重复发送**) |
| `--no-preflight` | 跳过预检, 只看名单解析结果(不用 token) |

## 跑完看什么

控制台会打印一张**按姓名**的逐行表 + 汇总 + 失败明细(含处理措施):

```
== 逐行结果 (3 人) ==
姓名  邮箱                  用户名    组织邀请        Team              说明
----  --------------------  --------  --------------  ----------------  ------------------
张三  zhangsan@example.com  zhangsan  组织邀请已发出  已加入 team       已按用户名邀请
李四  lisi@example.com      —         组织邀请已发出  缺用户名          已按邮箱邀请
王五  wangwu@example.com    wangwu    已是组织成员    待对方接受组织邀请  在组织成员列表里
```

同时在 `output/` 下留档, 每跑一次一组:

| 文件 | 内容 |
|---|---|
| `results-<时间戳>.csv` | 姓名/邮箱/用户名/状态/失败原因/处理措施, Excel 直接打开 |
| `results-<时间戳>.jsonl` | 机器可读, **续跑靠它**: 重跑自动跳过上次已成功的人 |
| `plan-<时间戳>.csv` | dry-run 时生成的计划 |

## 三条要知道的限制

1. **按邮箱邀请**: 只有该邮箱是对方 GitHub 账号上**已验证**的邮箱, 他才点得动邀请链接。
   接口这时候照样返回成功, 所以只有邮箱的人事后要用 `--status` 对一遍。
   **能拿到用户名就优先用用户名邀请**, 这条路可靠得多(脚本默认就这么做)。
2. **组织邀请 7 天过期**, 到期前催一下没接受的人。
3. **Team 成员接口只收用户名, 不认邮箱**, 而且对方得先是组织成员。
   所以收集表最好带一列 GitHub 用户名。

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 满屏重试 / SSL 报错 | 系统代理 | **关掉系统代理** |
| `404` | 组织名写错, 或 token 不是该组织 owner | 核对组织 slug 和 token |
| `403 权限不足` | 权限没给够, 或细粒度 token 没被组织批准 | 补 Members 写权限 / 去组织批准 |
| `找不到 team: xxx` | 名字写错 | 报错里会列出全部 team |
| `已经是组织成员` | 人本来就在 | 不用管, 接口也不会给他发邮件 |
| 失败清单里 `Invitation expired` | 7 天没接受 | 重新发一次 |
| 一批人「缺用户名, 加不了 team」 | 表里只有邮箱 | 补一列 GitHub 用户名再重跑 |

设计取舍、接口细节和踩坑记录见 [ARCHITECTURE.md](ARCHITECTURE.md)。
