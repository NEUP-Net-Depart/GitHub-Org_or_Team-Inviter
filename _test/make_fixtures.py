#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具生成器。

为什么要有这个文件: `.gitignore` 把 `*.csv` / `*.xlsx` 全挡在版本库外面
(收集表是最大的泄露源), 所以**测试夹具不能入库** —— 必须能现场造出来。
否则一个干净的克隆跑 `_test/run_offline_tests.py` 会因为找不到夹具而大面积失败。

所以规矩是: **每个夹具都在下面的 `FIXTURES` 里登记**, 别的测试脚本只管调用
`ensure()`(缺什么补什么), 谁都不需要先手动跑这个文件。

    python _test/make_fixtures.py            # 补齐缺失的夹具
    python _test/make_fixtures.py --force    # 全部重造(改了内容想比对时用)

不属于交付物。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
<sheet name="报名表" sheetId="1" r:id="rId1"/>
<sheet name="第二张" sheetId="2" r:id="rId2"/>
</sheets>
</workbook>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""

SHARED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="12" uniqueCount="12">
<si><t>姓名</t></si>
<si><t>邮箱</t></si>
<si><t>GitHub用户名</t></si>
<si><t>小明</t></si>
<si><t>xiaoming@example.com</t></si>
<si><t>xiaoming</t></si>
<si><t>小红</t></si>
<si><t>xiaohong@example.com</t></si>
<si><t>xiaohong</t></si>
<si><t>小刚</t></si>
</sst>"""

# 第 1 行表头用共享字符串; 第 4 行用内联字符串; 第 3 行故意跳过 B 列(稀疏)
SHEET1 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>
<row r="1">
<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>
</row>
<row r="2">
<c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2" t="s"><v>5</v></c>
</row>
<row r="3">
<c r="A3" t="s"><v>6</v></c><c r="C3" t="s"><v>8</v></c>
</row>
<row r="4">
<c r="A4" t="inlineStr"><is><t>小刚</t></is></c>
<c r="B4" t="inlineStr"><is><t>xiaogang@example.com</t></is></c>
</row>
<row r="5"/>
</sheetData>
</worksheet>"""

SHEET2 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>
<row r="1">
<c r="A1" t="inlineStr"><is><t>姓名</t></is></c>
<c r="B1" t="inlineStr"><is><t>GitHub用户名</t></is></c>
</row>
<row r="2">
<c r="A2" t="inlineStr"><is><t>第二张的人</t></is></c>
<c r="B2" t="inlineStr"><is><t>sheet2-user</t></is></c>
</row>
</sheetData>
</worksheet>"""


# --------------------------------------------------------------------------
# 夹具本体
# --------------------------------------------------------------------------

def build_fixture_xlsx(path: Path) -> None:
    """最小 xlsx: 两张工作表, 混合共享字符串/内联字符串, 一个跳格, 一个空行。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/sharedStrings.xml", SHARED)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET1)
        zf.writestr("xl/worksheets/sheet2.xml", SHEET2)


def build_bom_csv(path: Path) -> None:
    """带 UTF-8 BOM + CRLF 的 csv —— BOM 会让第一列列名认不出来, 是个真坑。"""
    text = ("姓名,邮箱,GitHub用户名\r\n"
            "BOM甲,bom1@example.com,bom-one\r\n"
            "BOM乙,bom2@example.com,bom-two\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))


def build_broken_xlsx(path: Path) -> None:
    """扩展名是 xlsx 但内容不是 zip —— 用来验证报错路径。"""
    path.write_bytes(b"this is definitely not a zip file")


def build_standard_csv(path: Path) -> None:
    """主力夹具: 表头能认出来, 且每列都埋了边界情况。

    依次覆盖: 大小写邮箱 / 从 github.com URL 里抠用户名 / 只给邮箱 /
    只给用户名 / 完全重复行 / 邮箱列里塞了个非邮箱(要兜底当用户名) /
    一格两个邮箱 / 格式可疑的用户名(要提醒但不拦) / 全空白行(不该占行号)。
    """
    text = (
        "姓名,邮箱,GitHub 用户名,提交时间\n"
        "张三,zhangsan@example.com,zhangsan,2025-01-05\n"
        "李四,LiSi@Example.COM,,2025-01-05\n"
        "王五,wangwu@example.com,https://github.com/wangwu,2025-01-06\n"
        "赵六,,zhaoliu,2025-01-06\n"
        "钱七,qianqi@example.com,,2025-01-07\n"
        "张三重复,zhangsan@example.com,zhangsan,2025-01-08\n"
        "孙八,not-an-email,sunba,2025-01-09\n"
        '周九,"zhoujiu@example.com; zhoujiu2@example.com",zhoujiu,2025-01-10\n'
        "吴十,wu.shi@example.com,wu_shi,2025-01-11\n"
        ",,,\n"
        "郑十一,zhengshiyi@example.com,ZHENG-SHI-YI,2025-01-12\n"
        "钱七2,qianqi@example.com,,2025-01-13\n"
    )
    path.write_bytes(text.encode("utf-8"))


def build_noheader_csv(path: Path) -> None:
    """没有表头的 csv, 且列顺序是「用户名,邮箱,姓名」—— 必须靠 --no-header 或提问。"""
    path.write_bytes(("login-1,person1@example.com,甲\n"
                      "login-2,person2@example.com,乙\n").encode("utf-8"))


def build_semicolon_csv(path: Path) -> None:
    """分号分隔 + 中文表头 + 姓名列在中间 + 有缺列的行。"""
    path.write_bytes(("邮箱;姓名;GitHub用户名\n"
                      "someone@example.com;甲;someone\n"
                      "other@example.com;乙;other-login\n"
                      ";丙;only-login\n"
                      "last@example.com;丁;\n").encode("utf-8"))


# 夹具登记表: 名字 -> 生成函数。加了新夹具就往这里加一行, 别在别处硬编码文件名。
FIXTURES: dict[str, object] = {
    "standard.csv": build_standard_csv,
    "noheader.csv": build_noheader_csv,
    "semicolon.csv": build_semicolon_csv,
    "bom.csv": build_bom_csv,
    "fixture.xlsx": build_fixture_xlsx,
    "broken.xlsx": build_broken_xlsx,
}


def ensure(force: bool = False, verbose: bool = True) -> list[str]:
    """补齐缺失的夹具, 返回本次真正写出的文件名。

    已存在的一律不动(允许手工改成别的样子做实验); `force=True` 才全部重造。
    """
    written: list[str] = []
    for name, build in FIXTURES.items():
        path = HERE / name
        if path.exists() and not force:
            continue
        build(path)  # type: ignore[operator]
        written.append(name)
    if written and verbose:
        print(f"[夹具] 生成 {len(written)} 个: {', '.join(written)}")
    return written


def main(argv: list[str]) -> int:
    force = "--force" in argv
    written = ensure(force=force, verbose=False)
    if written:
        for name in written:
            print(f"wrote {HERE / name}")
    else:
        print(f"夹具齐全({len(FIXTURES)} 个), 未改动; 想重造加 --force")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
