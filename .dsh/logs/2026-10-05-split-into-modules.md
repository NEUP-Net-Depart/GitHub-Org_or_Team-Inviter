# 把单文件拆成 `inviter/` 包(入口仍叫 `github_inviter.py`)

**日期**: 2026-10-05
**触发**: 用户要求「按模块拆分, 不然后期发展逻辑会很乱」。当时全部实现挤在一个
2047 行的 `github_inviter.py` 里。

## 现象: 为什么这一刀不好下

单文件本身不难拆, 难的是**拆完之后谁还在依赖它**:

| 依赖方 | 依赖形式 | 拆了会怎样 |
|---|---|---|
| README / `.dsh/skills` 技能 | 命令写死 `python github_inviter.py` | 改名 = 文档与技能全失效 |
| `_test/run_offline_tests.py`、`run_example_tests.py` | 子进程跑 `ROOT/github_inviter.py` | 同上 |
| `_test/run_path_tests.py`、`run_output_tests.py`、`check_token_formats.py` | `import github_inviter as g`, 直接引用 `g.process_org_invites` 等内部名字 | 名字一搬家就全断 |
| `_test/run_prompt_tests.py` | 还**替换**了 `g._stdin_is_interactive` / `g._ask_line` | 见下面「认知纠正 1」, 会**静默失效** |

所以定下来的做法是: **仓库根的 `github_inviter.py` 留作兼容层与入口**, 实现搬进
`inviter/` 包。所有命令、文档、技能、测试的调用方式一个字都不用改。

## 处理

- 新包 14 个模块, 依赖自上而下单向: `constants/console` → `columns/records/tokens`
  → `tableio/prompt/github_api/outcomes` → `input_source/report/storage` → `workflow` → `cli`。
- 兼容层里**不含任何 `def`/`class`**, 只有转发 import 加 `__main__` 守卫。
- 搬迁是**纯搬家**: 函数体、注释、docstring、用户可见字符串一律逐字照抄。

### 怎么证明「纯搬家没改行为」

动手之前先把原 `github_inviter.py` 复制到 `_tmp_golden/` 留底, 用同一组命令录一份输出:

`--help`, 以及 `--no-preflight` 对 `docs/examples/` 下四个示例表(含
`--no-header --cols name,email,username` 那条, 顺带跑 `normalize_argv`)。

搬完对同一个快照逐字节 diff —— 只归一化两处必然变化的东西: `plan-<时间戳>.csv` 和
`耗时 X.X 秒`。结果 **6 / 6 全等**。这比人工走查 2000 行可信得多, 也是这次唯一能
证明「没改行为」的硬证据。

### 套件结果

```
run_offline_tests.py     通过 46 / 失败 0
run_output_tests.py      通过 11 / 失败 0
run_path_tests.py        通过 22 / 失败 0
run_example_tests.py     通过  6 / 失败 0
run_prompt_tests.py      通过  7 / 失败 0
check_token_formats.py   全 PASS
run_module_tests.py      通过  8 / 失败 0   (新增)
```

新增的 `run_module_tests.py` 查结构不查行为: 兼容层里 `def`/`class` 必须为 0、
`inviter/` 不许反向引用兼容层、逐个模块单独导入(抓循环导入)、`PROJECT_ROOT` 指对地方、
兼容层导出面完整、**状态常量与 `ORG_LABEL`/`TEAM_LABEL`/`REMEDY` 的键一一对上**、
没有两个模块定义同名顶层函数、`python -m inviter` 与 `python github_inviter.py` 的
`--help` 完全一致。

## 认知纠正(踩到的坑)

### 1. monkeypatch 会随代码一起搬家, 而且**是静默失效**

`run_prompt_tests.py` 原本这么干:

```python
g._stdin_is_interactive = lambda: True
source = g._load_table_source(str(weird), args())
```

原单文件里这两件事在同一个模块命名空间, 所以替换生效。搬家后兼容层上的
`_stdin_is_interactive` **只是对 `inviter.prompt` 里同一个函数对象的一份引用**;
`inviter.input_source` 里的调用点引用的是 `inviter.prompt` 的全局名。于是替换
`g._stdin_is_interactive` 什么也没换掉, 测试会去读真实 stdin —— 在管道里
`isatty()` 为假, 直接 `die`。

三条一起上才算修好:

1. 调用点一律写**模块限定调用** `prompt._stdin_is_interactive()` /
   `prompt.ask_columns(...)`, 保证 patch 点只有一个;
2. 测试改成替换 `g.prompt._stdin_is_interactive` / `g.prompt._ask_line`(断言一条没动);
3. 兼容层 docstring 里写明「这些名字只是引用, 替换它们无效」, 并让
   `run_module_tests.py` 断言 `g.prompt is inviter.prompt`。

**教训**: 凡是「测试替换模块全局名」的项目, 拆模块时替换点就是接口的一部分, 必须
跟着代码一起搬; 而且这类失效**不报错**, 只会表现为测试跑到别的分支上去。

### 2. `Path(__file__).parent` 是隐形依赖

原文件有两处靠它找「脚本旁边」:

- `resolve_token`: 找 `token.txt` / `.env`;
- `main`: 默认输出目录 `output/`。

代码一搬进子模块, `__file__` 就变成 `inviter/…`, 这两处会**悄悄**指向 `inviter/`:
token 找不到(报「没找到 token」)、结果文件跑进包里。现在统一锚在
`constants.PROJECT_ROOT`(包目录的上一级, 也就是仓库根), 并约定 `inviter/` 必须直接
放在仓库根下; `run_module_tests.py` 盯着这个不变量。

### 3. 结构检查也要对齐项目里**已有的豁免规则**

新套件里我第一版写的是「`REMEDY` 的键必须覆盖两档全部状态」, 当场红了: 它缺
`not_requested`(`T_NA`)。但 `T_NA` 是「本次没要求做 team」, **本来就不该有处理措施**
—— `run_output_tests.py` 早就把这条豁免写进断言了(`if status != g.T_NA and not ...`)。
断言改成「除 `T_NA` 以外全覆盖」才是对的。

**教训**: 新写的检查如果不照着既有规则写, 就会把**正确的**东西判成错的, 然后逼着人
去「修」一个没坏的地方。

### 4. 环境插曲: 受限沙箱里跑不了这套测试(不是代码的问题)

排查过程中发现有段时间**仓库根能写、已存在的子目录(`_test/`、`docs/`、`output/`、
`.dsh/`)一个都写不进**, 报 `UnauthorizedAccessException`; 而根下**新建**的目录又能写。
原因是这些子目录是在沙箱那条可继承的写入项加上之前就存在的, 所以没继承到它。

更麻烦的是第二层: 四个套件用 `tempfile.mkdtemp`, 而它按 0o700 建的目录带一份私有的
权限项, 受限令牌**连它的权限都读不到** —— 于是「在受限沙箱里跑套件」这条路根本不通,
表现是一堆 `PermissionError`, 看着像代码坏了。

后来会话切到**完全权限**, 上述问题全部消失, 套件一次跑通。顺带清掉了探查时留下的
`_tmp_scratch/`、`_tmp_scratch2/` 两个空目录(受限时删不掉)。

**结论**: 这类 `PermissionError` 要先问「是代码写错, 还是当前会话的文件权限不够」,
别急着改代码。

## 顺带: 跟着这次拆分一起修的文档

搬完家按「文档里说的还算不算数」把说明文件过了一遍(用户的要求是「不改大意, 只改必要的
部分」)。顺带修掉的都记在这儿, 免得以后有人以为是谁乱动:

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `ARCHITECTURE.md` | 开头的「全部逻辑在单文件里」+ 第二节整张模块地图 | 拆分之后这两处就是错的 |
| `ARCHITECTURE.md` | 坏链 `[dsh/logs](dsh/logs)` → `.dsh/logs` | 目录早改名了, 这个链接一直是 404 |
| `.dsh/README.md` | 标题 `# dsh/` → `# .dsh/`; skills 那段注 | 原注说 skill 在 `dsh/skills/`, 又说「要让 DSH 自动发现它就得把 SKILL.md 放到 `.dsh/skills/`」—— 路径是旧的, 后半句还自相矛盾(它本来就在那儿, DSH 本来就会发现) |
| `README.md` | `办法一` 前面补空行; 去掉指向不存在的「基础版命令」; `方法一/方法二` → `办法一/办法二` | 三行挤在一个段落里会被渲染成一整段; 而「基础版命令」全文从未定义过, 读者按这个名字找不到东西 |
| `requirement.md` | 追加一段 `后续(2026-10-05)`(原文一个字没删) | 那里还写着「要求一个 `dsh/` 目录…其他没想好, 到时候和我一起讨论」, 结构现在已经定了 |
| `SKILL.md` | 「涉及的文件」注明实现拆在 `inviter/` 下 | 让读到这个技能的 AI 知道逻辑不在那个单文件里了 |
| `requirement.md` / `SKILL.md` / `.dsh/README.md` | 行尾 CRLF → LF | 仓库 `.gitattributes` 声明的就是 `eol=lf`, git 提交时反正会归一化(所以提交内容不变), 但工作区能少一条 "LF will be replaced by CRLF" 警告 |

历史日志(`.dsh/logs/` 里早先那几篇)**一个字没动** —— 里面写着「1625 行」「单文件 1900 余行」,
那是当时的事实记录, 改了就成篡改记录。

顺带发现、但**故意没动**的: README 的章节编号不齐(前三个章节没编号, 后面是「四、五、六」)。
属于改了收益为零的排版, 留给用户定。

## 回滚方式

改动前工作树是干净的, 所以整条退回去就是:

```bash
git checkout -- .
```

临时目录 `_tmp_golden/`(快照与对照脚本)已在验证完成后删除。

## 复现验证

```bash
python _test/run_offline_tests.py     # 46
python _test/run_output_tests.py      # 11
python _test/run_path_tests.py        # 22
python _test/run_example_tests.py     #  6
python _test/run_prompt_tests.py      #  7
python _test/check_token_formats.py
python _test/run_module_tests.py      #  8   ← 拆分后新增的结构体检
python github_inviter.py --org demo-org --no-preflight docs/examples/收集表示例.csv
python -m inviter --help              # 与上一行的入口等价
```
