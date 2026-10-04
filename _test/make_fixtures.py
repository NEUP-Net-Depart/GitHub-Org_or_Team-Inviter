#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试夹具生成器: 造一个最小 xlsx(两张工作表, 含共享字符串/内联字符串/跳格)
和一个带 BOM 的 csv。只为验证 github_inviter.py 的读表能力, 不属于交付物。"""
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


def build_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/sharedStrings.xml", SHARED)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET1)
        zf.writestr("xl/worksheets/sheet2.xml", SHEET2)
    print(f"wrote {path.name}")


def build_bom_csv(path: Path) -> None:
    text = ("姓名,邮箱,GitHub用户名\r\n"
            "BOM甲,bom1@example.com,bom-one\r\n"
            "BOM乙,bom2@example.com,bom-two\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    print(f"wrote {path.name}")


def build_broken_xlsx(path: Path) -> None:
    path.write_bytes(b"this is definitely not a zip file")
    print(f"wrote {path.name}")


if __name__ == "__main__":
    build_xlsx(HERE / "fixture.xlsx")
    build_bom_csv(HERE / "bom.csv")
    build_broken_xlsx(HERE / "broken.xlsx")
