# dsh/

这个目录是给 DSH(AI 协作)用的工作区, 不属于脚本运行所需。

| 目录 | 用途 |
|---|---|
| `logs/` | 本次重构与实跑的过程记录、踩坑、结论。出问题时先翻这里 |
| `skills/` | 可复用的操作流程 |

脚本本身**不读这个目录** —— 删掉它不影响 `github_inviter.py` 运行。

## skills/

`skills/<名字>/SKILL.md`, 带 YAML frontmatter(`name` + `description`)。
`description` 要写清**什么时候该用它**, 因为只有它会被列进技能目录供检索。

| 技能 | 什么时候用 |
|---|---|
| [`github-org-team-invite`](skills/github-org-team-invite/SKILL.md) | 跑 / 排查 / 核对批量组织与 team 邀请时。含铁律(先 dry-run、先小批量)、标准流程、常见症状对照表、以及「用户名和邮箱对不上」这类实测踩坑 |

> 注: 这个目录是给 AI 读的运行手册。目前放在 `dsh/skills/` 下按需求归档;
> 若要让 DSH 自动发现它, 把同样的 `SKILL.md` 放到工作区的 `.dsh/skills/<名字>/` 即可
> (`.agents/skills/` 是仓库级, `.dsh/skills/` 是工作区级)。

## 日志约定

`logs/YYYY-MM-DD-<主题>.md`, 记录: 现象 → 原因 → 处理 → 回滚方式(如果改过环境),
以及**认知纠正**(先前判断错了、实测结论是什么)。真实姓名/邮箱/用户名不写进日志。
