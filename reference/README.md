# GitHub 组织运维脚本

两个脚本：**批量发组织邀请**、**批量把人拉进 Team**。

> ## ⚠️ 跑之前先关掉系统代理
>
> Clash / v2ray 之类的**「系统代理」开关请关掉**。
>
> 脚本**不需要代理**，代理反而经常导致连不上 GitHub —— 表现是满屏
> `网络异常, 2s 后重试`，或者 `SSL: UNEXPECTED_EOF_WHILE_READING`。
>
> 实测：走系统代理成功率 42%，直连 100%。

---

## 一次性准备

**1. 你得是组织 owner**（或至少是该 Team 的 maintainer）。

**2. 把 token 写进 `token.txt`**，放在脚本同目录，内容就是 token 本身，一行。

去 <https://github.com/settings/tokens> 建一个：

| token 类型 | 需要的权限 |
|---|---|
| Fine-grained | Organization permissions → **Members** → Read and write |
| Classic | `admin:org` |

---

## 脚本一：批量发组织邀请

读表格里的**邮箱**（或 GitHub 用户名），给人发组织邀请。

### 表格长这样

**约定：第 1 列是 GitHub 用户名，第 2 列是邮箱。**

| GitHub 用户名 | 邮箱 |
|---|---|
| zhangsan | zhangsan@example.com |
| lisi-2026 | lisi@example.com |

有表头行的话，表头随便写 —— 脚本**先按表头认列**（认得 `用户名` / `邮箱` /
`GitHub用户名` / `email` 这些），认不出来就按上面的位置约定。

只有用户名、或只有邮箱，也都能跑。支持 `.xlsx` 和 `.csv`。

### 列顺序不一样？用 `--cols` 说明

位置从 **1** 开始数。每项只填 `username` / `email` / `name`
（中文 `用户名` / `邮箱` / `姓名` 也认），不想用的列写 `-`：

```bash
--cols "username,email"      # 第1列用户名, 第2列邮箱 —— 和默认约定一样
--cols "email,username"      # 第1列邮箱, 第2列用户名
--cols "-,username,email"    # 第1列不管, 第2列用户名, 第3列邮箱
```

指到不存在的列会直接报错提醒，不会静默变空。

### 三条命令

```bash
cd /d/VScode-library/My_projects/Miaomiao_Tool/GitHub-Org-Inviter

# 1. 看计划 —— 不改动任何东西
python invite_org_members.py --org NEUP-Net-Depart --input "数据表.xlsx"

# 2. 试跑 3 个
python invite_org_members.py --org NEUP-Net-Depart --input "数据表.xlsx" --execute --limit 3

# 3. 确认没问题，全量执行
python invite_org_members.py --org NEUP-Net-Depart --input "数据表.xlsx" --execute
```

### 常用选项

| 选项 | 说明 |
|---|---|
| `--resolve-emails` | 先尝试把邮箱反查成 GitHub 用户名，**推荐加上** |
| `--report` | 对账：谁已加入、谁还在等、谁的邀请失败 |
| `--limit N` | 只处理前 N 条 |
| `--no-resume` | 重跑全部（默认会跳过已成功的） |

### 对账（看谁进来了）

```bash
python invite_org_members.py --org NEUP-Net-Depart --input "数据表.xlsx" --resolve-emails --report
```

---

## 脚本二：批量拉进 Team

```bash
# 用名单文件（每行一个 GitHub 用户名）
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" --users "名单.txt"

# 直接写在命令里
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" --users "alice,bob,carol"

# 用数据表（表里的邮箱会自动反查用户名）
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" --from-table "数据表.xlsx"

# 看过计划没问题，加 --execute 真正执行
python add_team_members.py --org NEUP-Net-Depart --team "NEUP 2026" --users "名单.txt" --execute
```

### 名单文件格式

每行一个用户名就行。`#` 开头是注释，空行忽略，重复自动去重。
从表格里直接复制的 `用户名,邮箱,备注` 也能用（只取第一列）。

```
alice
bob
carol
```

模板见 `users.example.txt`。

### 跑完会打印逐行状态

```
=== 逐行状态 (5 行) ===
行  姓名   邮箱                   用户名      组织状态                     team 状态
2   张三   zhangsan@example.com   zhangsan    ✅ 已是组织成员              ✅ 已在 team
3   李四   lisi@example.com       —           ⏳ 邀请已发出, 待接受        ❓ 未知(没有用户名)
4   王五   wangwu@example.com     wangwu      ✅ 已加入组织(邀请已被接受)  ❌ 未加入 team
5   赵六   zhaoliu@example.com    —           ❌ 邀请失败/过期             ❓ 未知(没有用户名)

小结: 已是组织成员 2 人 | 邀请待接受 1 人 | 邀请失败/过期 1 人 | 还不在组织 0 人 | 无法判断 0 人
```

纵列含义：

| 列 | 说明 |
|---|---|
| **组织状态** | ✅ 在组织里 / ⏳ 邀请发出去了还在等 / ❌ 失败或过期 |
| **team 状态** | ✅ 已在 team / ❌ 还没进 / ❓ 不知道用户名，加不了 |
| **本次动作** | 这次实际做了什么（只在 `--execute` 时有值） |

---

## 三条关键限制

1. **按邮箱邀请**要求该邮箱是对方 GitHub 账号上**已验证**的邮箱，否则对方加不进来。
   能拿到用户名就优先用用户名。
2. **邀请 7 天过期**，没接受就作废。
3. **只有知道 GitHub 用户名，才能把人拉进 Team**。只有邮箱不行 —— 所以收集表最好带一列 GitHub 用户名。

---

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 满屏 `网络异常, 2s 后重试` | 系统代理 | **关掉系统代理**（见开头） |
| 大量 `无法判断(邮箱未反查到账号)` | 邮箱没绑定到 GitHub 账号 | 让人报一下用户名 |
| `找不到 team: xxx` | 名字写错 | 报错里会列出全部 team |
| `这些人不在组织里` | 邀请还没被接受 | 等对方接受后再跑 |
| `HTTP 404` | token 权限不够 | 补上 Members 写权限 |
| `找不到 invite_org_members.py` | 不在脚本目录 | 先 `cd` 到 `GitHub-Org-Inviter` |

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `invite_org_members.py` | 发组织邀请 |
| `add_team_members.py` | 拉进 Team（**依赖上面那个，别分开**） |
| `token.txt` | 你的 token（已被 `.gitignore` 排除） |
| `users.example.txt` | 名单文件模板 |
| `output/` | 每次运行的结果 CSV / JSONL，自动生成 |
| `ARCHITECTURE.md` | 设计说明、接口细节、踩坑记录（维护用） |

> 设计取舍与实现细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。
