# 纠错过程

**2026-10-03** · GitHub 组织批量邀请（`NEUP-Net-Depart`，12 人，最终零失败）

---

## 1. 工作目录权限被拒（会话开始）

- **现象**：所有命令报 `SetNamedSecurityInfoW failed (Win32 5)`，连列目录都不行
- **原因**：`Miaomiao_Tool` 缺当前用户的完全控制权限，DSH 无法为其授权
- **处理**：一次性诊断+修复，只补一条用户完全控制权限；复查生效，原操作重跑成功
- **回滚**：
  ```
  pwsh -NoProfile -File 'D:\VScode-library\My_projects\_acl-recovery\acl-backup-455e1508aeb942cb986d2ba17b879390.json.ps1' -Path 'D:\VScode-library\My_projects\Miaomiao_Tool' -AllowRoot 'D:\VScode-library\My_projects\Miaomiao_Tool' -Restore 'D:\VScode-library\My_projects\_acl-recovery\acl-backup-455e1508aeb942cb986d2ba17b879390.json'
  ```

## 2. 统计口径错误

- **现象**：dry-run 报"跳过 3 条"，但其中 2 条明明在"将邀请"列表里
- **原因**：「没有邮箱、只能按用户名邀请」的行被同时塞进了 skipped 列表
- **修复**：拆成 `excluded`（真排除）和 `warnings`（仅提醒）两个概念

## 3. 续跑静默丢记录

- **现象**：伪造 2 条历史成功记录后，续跑只跳过了 1 个
- **原因**：结果文件带 UTF-8 BOM，`read_text(encoding="utf-8")` 让第一行 JSON 解析失败被静默跳过
- **修复**：改用 `utf-8-sig`

## 4. 重构回归 + 复测方法错误

- **现象**：`NameError: name 'rows' is not defined`
- **原因**：把读表逻辑抽成 `load_records()` 后，`main` 里仍在引用局部变量 `rows`
- **次生问题**：复测时用 `Select-String` 过滤输出，把 traceback 一起过滤掉了，**误判为通过**
- **修复**：`load_records` 一并返回总行数；复测改为不过滤、看完整输出

## 5. 对账误判（最严重）

- **现象**：`--limit 3` 后对账，10 行被判「已经是成员（推测）」，**其中 8 行从未发送过**
- **原因**：对账只看组织当前状态（不在待处理/失败清单 → 推断已是成员），没看我们实际发过什么
- **影响**：会让人误以为"大家都已经在组织里了，不用发了"
- **修复**：新增 `load_attempts()`，先读 `results-*.jsonl` 确认发送历史，没发过的标「未处理」

## 6. `--resolve-emails` 在对账路径失效

- **现象**：`--report --resolve-emails` 时目标行仍显示「未处理」
- **原因**：反查逻辑只写在发送路径里
- **修复**：抽成 `apply_email_resolution()`，发送与对账两条路径共用

---

## 认知纠正（结论修正，非代码 bug）

| 原判断 | 实测结论 |
|---|---|
| 表里只有邮箱就查不出谁已在组织里 | **不准确**。直接发邀请，GitHub 回 `422`：`A user with this email address is already a part of this organization`，且不给对方发邮件 |
| （未知） | 组织邀请**有效期 7 天**，失败清单里可见 `Invitation expired. User did not accept this invite for 7 days` |

---

# 第二批纠错（同日后续）

**2026-10-03 下午~晚间** · 状态修正 + 功能调整 + 隐私清理

## 7. 已接受邀请的人仍显示"待接受"

- **现象**：某人明明已加入组织，脚本仍显示「⏳ 邀请已发出, 待接受」
- **原因**：邀请被接受后该邮箱同时退出 pending 和 failed 两份清单，代码却仍按
  `history[email]["status"] == "invited"` 判断
- **修复**：`describe_person` 改为 —— `invited` 历史 + 不在 pending/failed ⇒ 已加入
- **副产**：修完发现另有 2 人其实早已接受

## 8. 系统代理：加了又删

- **背景**：环境开着系统代理，`urllib` 自动读取，导致 TLS 握手随机失败
- **实测**：裸 TLS 14/14 (100%)、走系统代理 5/12 (42%)、直连 12/12 (100%)
- **过程**：先加了 `--proxy` / `--system-proxy` / 自定义 opener，验证确实有效
- **回退**：判断不值得为环境问题加代码，全部删除，恢复原生 `urlopen`
- **结论**：改为在 README 顶部显著提示「跑之前关掉系统代理」

## 9. 邮箱 → 用户名的穷举（结论：接口层做不到）

| 接口 | 结果 |
|---|---|
| REST `GET /orgs/{org}/members` | 只有 login，无邮箱 |
| REST `GET /orgs/{org}/memberships/{user}` | 无邮箱 |
| REST 待处理邀请 | 有 email，但 `login` 恒为 `null` |
| REST 审计日志 | 404（免费组织无此功能） |
| GraphQL `membersWithRole { email }` | 只返回**公开**邮箱（本组织 38/114） |
| GraphQL `OrganizationInvitation.invitee` | `null` |
| GraphQL `Organization.pendingMembers` | `totalCount = 0`，尽管有 2 条待处理邀请 |
| GraphQL `invitationType` | `"EMAIL"` |

- **根因**：GitHub 不做反查。发邮箱邀请时它只是把邀请链接寄到该地址，由**对方**
  用自己账号认领；「邮箱必须是已验证邮箱」这条限制**本身就是链接机制**
- **推论**：组织邀请可以只用邮箱；但 **Team 成员接口只收用户名**

## 10. 用邮箱加 Team：6 种写法全失败

- 邮箱原样 / URL 编码 / 放进 body / 旧版 `members` 路径 —— 全部 `404`
- team 成员数不变，确认接口层不认邮箱

## 11. 列识别：从"按内容猜"改为"位置约定"

- **背景**：先实现了 `classify_cell()` + `guess_columns_from_data()`（按单元格内容猜列）
- **调整**：决定直接用位置约定（第 1 列用户名、第 2 列邮箱），删掉按内容猜列
- **保留**：`classify_cell()` 留着，用于兜住「两列写反 / 一列混装」
- **新增**：`--cols "username,email"` 显式按位置指定；表头认不出时回退到该约定

## 12. `github.com/x` 被解析成 `github.com`

- **原因**：正则要求 `https://` 前缀，裸域名不匹配，随后 `split("/")[0]` 取到了域名
- **修复**：改为 `^(?:https?://)?(?:www\.)?github\.com/`

## 13. 本职列优先级（隐蔽）

- **现象**：示例表邮箱列填了 `not-an-email`，它不是邮箱、**却恰好是合法用户名格式**，
  把用户名列里真正的值盖掉了
- **原因**：合并写成 `login = login_a or login_b`（邮箱列优先）
- **修复**：各自优先信本职列 —— `email = email_a or email_b`、`login = login_b or login_a`

## 14. `--cols` 越界静默变空

- **现象**：给只有 2 列的表指定第 3 列，不报错，只是该列永远为空
- **修复**：解析后校验下标是否落在表宽内，超出直接 `die`，并提示位置从 1 开始数

## 15. 预检把"待处理邀请"数成 0

- **现象**：实际有 2 条待处理邀请，预检打印「待处理邀请 0 个」，但跳过逻辑工作正常
- **原因**：打印的是 `len(pending_logins)`，只数带用户名的邀请；按邮箱发的邀请
  `login` 恒为 `null`，于是恒为 0
- **性质**：**仅显示错误，行为无误**（跳过逻辑用的是 `pending_emails`）
- **修复**：改为累计所有待处理邀请条数

## 16. 隐私清理

- README / ARCHITECTURE 中的真实姓名·邮箱·用户名 → 换成占位示例
- `.gitignore` 原先只挡 `token.txt` 和 `output/`，**漏了收集表 xlsx**（最大泄露源），已补
- 清空 `output/`（含真实邮箱的运行记录）

---

## 认知纠正（第二批）

| 原判断 | 实测结论 |
|---|---|
| 组织 owner 能看到成员邮箱 | **不能**。REST 完全不返回；GraphQL 只给公开邮箱（本组织仅 38/114 公开了） |
| 邮箱邀请会留下"某人待加入"的记录 | **不会**。`pendingMembers` 对邮箱邀请计数为 0，`invitee` 为 `null` |
| `already_member` 的 422 可能仍给对方发了邮件 | **没有**。邀请 ID 前后不变，未创建任何对象；只有 201 才发邮件 |

