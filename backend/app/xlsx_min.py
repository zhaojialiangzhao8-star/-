"""极简 xlsx 生成器（零依赖，只用标准库 zipfile + xml）。

为什么要自己写：打包版（PyInstaller）里每多一个第三方依赖就多一份风险，
而导出只需要「多 sheet + 表头 + 数字/文本」这点能力，用 zip+XML 三百行足够。

用法：
    from app.xlsx_min import write_xlsx
    write_xlsx(path, [
        ("汇总", ["列1", "列2"], [[...], [...]]),
        ("明细", ["a", "b"], [[1, 2.5]]),
    ])
"""
from __future__ import annotations

import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""

SHEET_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
<cols>{cols}</cols>
<sheetData>"""

STYLE_HEAD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
  <font><sz val="11"/><name val="Calibri"/></font>
  <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
</cellXfs>
</styleSheet>"""


def _col(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def _cell(ref: str, value, header: bool) -> str:
    style = ' s="1"' if header else ""
    if value is None or value == "":
        return f'<c r="{ref}"{style}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return f'<c r="{ref}"{style}/>'
        return f'<c r="{ref}"{style}><v>{round(float(value), 6)}</v></c>'
    if isinstance(value, (datetime, date)):
        value = value.strftime("%Y-%m-%d %H:%M:%S" if isinstance(value, datetime) else "%Y-%m-%d")
    else:
        value = str(value)
    return f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def _width(header: list[str], rows: list[list]) -> str:
    out = []
    for i, h in enumerate(header):
        w = max([len(str(h))] + [len(str(r[i])) for r in rows[:400] if i < len(r) and r[i] is not None] or [8])
        out.append(f'<col min="{i + 1}" max="{i + 1}" width="{min(max(w + 2, 8), 42)}" customWidth="1"/>')
    return "".join(out)


def write_xlsx(path: str | Path, sheets: list[tuple[str, list[str], list[list]]]) -> Path:
    path = Path(path)
    ct_sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(sheets))
    )
    wb_sheets = "".join(
        f'<sheet name="{escape(name[:31])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, (name, _, _) in enumerate(sheets)
    )
    wb_rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(sheets))
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES.format(sheets=ct_sheets))
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("xl/workbook.xml", WORKBOOK.format(sheets=wb_sheets))
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            WORKBOOK_RELS.format(rels=wb_rels + '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'),
        )
        z.writestr("xl/styles.xml", STYLE_HEAD)
        for idx, (_, header, rows) in enumerate(sheets):
            body = ['<row r="1">']
            body += [_cell(f"{_col(j)}1", h, True) for j, h in enumerate(header)]
            body.append("</row>")
            for ri, row in enumerate(rows, start=2):
                body.append(f'<row r="{ri}">')
                body += [_cell(f"{_col(j)}{ri}", v, False) for j, v in enumerate(row)]
                body.append("</row>")
            xml = SHEET_HEAD.format(cols=_width(header, rows)) + "".join(body) + "</sheetData></worksheet>"
            z.writestr(f"xl/worksheets/sheet{idx + 1}.xml", xml)
    return path
