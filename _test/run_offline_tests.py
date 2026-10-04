#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试台: 逐个跑 github_inviter.py 的 dry-run 场景, 检查关键断言。

只跑 --no-preflight 与本地报错路径, 不碰网络、不发任何邀请。
不属于交付物, 交付前连同 _test/ 一起删除。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "github_inviter.py"
FIX = ROOT / "_test"

PASS, FAIL = [], []


def run(args: list[str], stdin_text: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check(title: str, args: list[str], must_have: list[str] = (),
          must_not: list[str] = (), expect_code: int | None = None,
          stdin_text: str | None = None) -> None:
    code, output = run(args, stdin_text)
    problems = []
    for needle in must_have:
        if needle not in output:
            problems.append(f"缺少: {needle!r}")
    for needle in must_not:
        if needle in output:
            problems.append(f"不该出现: {needle!r}")
    if expect_code is not None and code != expect_code:
        problems.append(f"退出码 {code}, 期望 {expect_code}")
    status = "PASS" if not problems else "FAIL"
    (PASS if not problems else FAIL).append(title)
    print(f"[{status}] {title}")
    for problem in problems:
        print(f"         {problem}")
    if problems:
        print("         ---- 输出 ----")
        for line in output.splitlines()[:40]:
            print(f"         {line}")


def main() -> int:
    std = str(FIX / "standard.csv")
    # 运行时造的测试表放临时目录, 跑完就没了 —— 免得 _test/ 里堆一堆生成物
    tmp_dir = Path(tempfile.mkdtemp(prefix="inviter-offline-"))

    # --- 1. 表头自动识别 ---
    check("表头识别三列",
          ["--org", "o", "--no-preflight", std],
          must_have=["邮箱=<邮箱>", "用户名=<GitHub 用户名>", "姓名=<姓名>",
                     "待处理 9 条"],
          expect_code=0)

    # --- 2. 各列按内容归类 ---
    check("邮箱列里的 not-an-email 当用户名兜底",
          ["--org", "o", "--no-preflight", std],
          must_have=["孙八", "sunba"])
    check("邮箱统一小写",
          ["--org", "o", "--no-preflight", std],
          must_have=["lisi@example.com"], must_not=["LiSi@Example.COM"])
    check("github.com/x 提取成 x",
          ["--org", "o", "--no-preflight", std],
          must_have=["wangwu"], must_not=["https://github.com/wangwu"])
    check("一格多邮箱只取第一个",
          ["--org", "o", "--no-preflight", std],
          must_have=["单元格里有 2 个邮箱, 只取了第一个", "zhoujiu@example.com"],
          must_not=["zhoujiu2@example.com"])
    check("可疑用户名给提醒不拦截",
          ["--org", "o", "--no-preflight", std],
          must_have=["wu_shi", "用户名格式可疑"])
    check("重复行只留一条",
          ["--org", "o", "--no-preflight", std],
          must_have=["与第 2 行是同一个人", "与第 6 行是同一个人"])
    check("完全空白的行不占号",
          ["--org", "o", "--no-preflight", std],
          must_have=["解析到 11 行数据", "待处理 9 条"],
          must_not=["行空白已忽略"])

    # --- 3. --cols ---
    check("--cols 换顺序(真实错位)",
          ["--org", "o", "--no-preflight", std, "--cols", "username,name,email"],
          must_have=["邮箱=<第 3 列>", "用户名=<第 1 列>", "姓名=<第 2 列>"],
          expect_code=0)
    check("--cols 以 - 开头也要能用(argparse 坑)",
          ["--org", "o", "--no-preflight", std, "--cols", "-,email,username"],
          must_have=["邮箱=<第 2 列>", "用户名=<第 3 列>"],
          expect_code=0)
    check("--cols 连续跳过两列",
          ["--org", "o", "--no-preflight", std, "--cols", "-,-,username"],
          must_have=["用户名=<第 3 列>", "邮箱=<(未使用)>"],
          expect_code=0)
    check("--cols 认不出的项要报错",
          ["--org", "o", "--no-preflight", std, "--cols", "name,email,code"],
          must_have=["看不懂"], expect_code=2)
    check("--cols 越界要报错",
          ["--org", "o", "--no-preflight", "--no-header", str(FIX / "noheader.csv"),
           "--cols", "-,-,-,email"],
          must_have=["只有 3 列"], expect_code=2)
    check("--cols 一项都没给有效角色要报错",
          ["--org", "o", "--no-preflight", std, "--cols", "-,-,-"],
          must_have=["没有一列是 email 或 username"], expect_code=2)

    # --- 3.5 --skip-rows: 表头认不出来时跳过表头行 ---
    # 场景: 表头写成「日期,QQ,微信,怎么称呼,备注」—— 一个列名都认不出
    weird = tmp_dir / "weird_header.csv"
    weird.write_text("日期,QQ,微信,怎么称呼,备注\n"
                     "2026-03-01,zhangsan,zhangsan@example.com,张三,组长\n"
                     "2026-03-01,lisi-2026,lisi@example.com,李四,\n",
                     encoding="utf-8")
    check("--cols 配 --skip-rows 1 跳过认不出的表头",
          ["--org", "o", "--no-preflight", str(weird),
           "--cols", "-,username,email,name,-", "--skip-rows", "1"],
          must_have=["邮箱=<第 3 列>", "用户名=<第 2 列>", "姓名=<第 4 列>",
                     "待处理 2 条", "张三", "李四"],
          must_not=["怎么称呼", "格式可疑"], expect_code=0)
    check("--cols 尾部写多余 '-' 也接受",
          ["--org", "o", "--no-preflight", str(weird),
           "--cols", "-,username,email,name,-", "--skip-rows", "1"],
          must_have=["待处理 2 条"], expect_code=0)
    check("--cols 不配 --skip-rows 时提醒表头行不会被跳过",
          ["--org", "o", "--no-preflight", str(weird),
           "--cols", "-,username,email,name,-"],
          must_have=["表头行不会被自动跳过", "--skip-rows 1"], expect_code=0)
    check("认不出表头且没给 --cols 时要停下问清楚(不静默按错的列发)",
          ["--org", "o", "--no-preflight", str(weird)],
          must_have=["兜底也对不上", "--cols", "--skip-rows 1"], expect_code=2)
    check("--skip-rows 超过总行数要报错",
          ["--org", "o", "--no-preflight", std, "--skip-rows", "999"],
          must_have=["超过表里的行数"], expect_code=2)

    # --- 4. csv 变体 ---
    check("分号分隔 + 中文表头",
          ["--org", "o", "--no-preflight", str(FIX / "semicolon.csv")],
          must_have=["邮箱=<邮箱>", "用户名=<GitHub用户名>", "待处理 4 条"], expect_code=0)
    check("带 BOM 的 csv",
          ["--org", "o", "--no-preflight", str(FIX / "bom.csv")],
          must_have=["待处理 2 条", "bom-one", "bom-two"], expect_code=0)

    # --- 5. 无表头 ---
    check("--no-header 按位置",
          ["--org", "o", "--no-preflight", "--no-header", str(FIX / "noheader.csv")],
          must_have=["用户名=<第 1 列>", "邮箱=<第 2 列>", "login-1", "person1@example.com"],
          expect_code=0)

    # --- 6. xlsx ---
    check("xlsx 最小解析(共享字符串+内联+跳格+空行)",
          ["--org", "o", "--no-preflight", str(FIX / "fixture.xlsx")],
          must_have=["表头: 姓名 | 邮箱 | GitHub用户名", "xiaoming", "xiaogang@example.com",
                     "待处理 3 条"],
          expect_code=0)
    check("xlsx 指定第二张工作表",
          ["--org", "o", "--no-preflight", str(FIX / "fixture.xlsx"), "--sheet", "第二张"],
          must_have=["sheet2-user", "第二张的人"], expect_code=0)
    check("xlsx 工作表不存在要报错",
          ["--org", "o", "--no-preflight", str(FIX / "fixture.xlsx"), "--sheet", "不存在"],
          must_have=["工作表不存在", "现有工作表"], expect_code=2)
    check("损坏的 xlsx 要给清楚报错",
          ["--org", "o", "--no-preflight", str(FIX / "broken.xlsx")],
          must_have=["不是有效的 xlsx"], expect_code=2)

    # --- 7. 命令行直接输入 ---
    check("表格 + 命令行补一个用户名",
          ["--org", "o", "--no-preflight", std, "extra-person"],
          must_have=["extra-person", "命令行上的 1 个输入"], expect_code=0)
    check("表格 + 命令行补一个邮箱",
          ["--org", "o", "--no-preflight", std, "extra@example.com"],
          must_have=["extra@example.com"], expect_code=0)
    check("命令行补的输入不合法要报错",
          ["--org", "o", "--no-preflight", std, "bad@@x"],
          must_have=["不是合法的 GitHub 用户名"], expect_code=2)
    check("两个以上输入要报错",
          ["--org", "o", "--no-preflight", std, "extra1", "extra2"],
          must_have=["最多给两个输入"], expect_code=2)
    check("不给输入要报错",
          ["--org", "o", "--no-preflight"],
          must_have=["必须给一个表格文件"], expect_code=2)
    check("只给命令行用户名不给表格要报错",
          ["--org", "o", "--no-preflight", "alice"],
          must_have=["找不到表格文件"], expect_code=2)

    # --- 8. .txt 与标准输入已不再支持 ---
    txt = tmp_dir / "legacy_list.txt"
    txt.write_text("alice\nbob\n", encoding="utf-8")
    check(".txt 名单给出明确的迁移提示",
          ["--org", "o", "--no-preflight", str(txt)],
          must_have=[".txt 名单不再支持", "请改用表格"], expect_code=2)
    check("'-' 不再是标准输入记号, 而是当成文件名",
          ["--org", "o", "--no-preflight", "-"],
          must_have=["找不到表格文件"], expect_code=2)

    # --- 10. --limit 语义(必须是「表里的前 N 行」) ---
    check("--limit 2 只看表里第 1~2 行",
          ["--org", "o", "--no-preflight", std, "--limit", "2"],
          must_have=["本次只看表里第 1~2 行", "跳过(--limit 截断)"],
          expect_code=0)
    check("--limit 截断的人不算失败",
          ["--org", "o", "--no-preflight", std, "--limit", "2"],
          must_have=["失败 0 人"],
          must_not=["邀请失败"],
          expect_code=0)
    check("--limit 覆盖重复行时不会多处理人",
          ["--org", "o", "--no-preflight", "--no-header", str(FIX / "noheader.csv"),
           "--limit", "3"],
          must_have=["本次只看表里第 1~3 行"],
          expect_code=0)

    # --- 11. 其它报错路径 ---
    check("文件不存在要报错",
          ["--org", "o", "--no-preflight", "不存在.xlsx"],
          must_have=["找不到表格文件"], expect_code=2)
    check("不支持的扩展名要报错",
          ["--org", "o", "--no-preflight", str(FIX / "make_fixtures.py")],
          must_have=["不支持的文件格式"], expect_code=2)
    check("--limit 负数要报错",
          ["--org", "o", "--no-preflight", std, "--limit", "-1"],
          must_have=["不能是负数"], expect_code=2)
    check("--status 不接受位置参数",
          ["--org", "o", "--status", std],
          must_have=["只读查询"], expect_code=2)

    # --- 11. 真实的参考收集表(只解析, 不外泄内容) ---
    real = ROOT / "reference" / "友友们的github账户开盒.xlsx"
    if real.is_file():
        code, output = run(["--org", "o", "--no-preflight", str(real)])
        head = output.splitlines()[1:8]
        print("\n[INFO] 参考表解析结果(前几行, 供人工确认):")
        for line in head:
            print(f"         {line}")
        status = "PASS" if code == 0 and "解析到" in output else "FAIL"
        (PASS if status == "PASS" else FAIL).append("参考收集表能解析")
        print(f"[{status}] 参考收集表能解析 (退出码 {code})")

    print(f"\n===== 结果: 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
    for title in FAIL:
        print(f"  失败: {title}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
