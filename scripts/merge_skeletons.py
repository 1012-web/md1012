"""拆卷解析的书：把几卷骨架接成一份整书骨架，再交 split_book.py。

mineru_api.py 把超过 200 页的 PDF 拆成几卷提交，build.py 对每卷各出一份骨架，脚注各自从 1 编号、
接缝处的段落被 PDF 硬切断。本脚本：
  - 按给定顺序拼接正文；后一卷的脚注号整体接着前一卷编（引用与定义一起改）；
  - 接缝处前一卷末段与后一卷首段按 build.py 的同一套判据（_common.is_break）判断是否断句，是就接成一段，
    页码锚点留在接缝处；接了没接都写进报告，务必对着原图核一眼；
  - 只留第一卷的 frontmatter（如有）。

用法：
  python -X utf8 merge_skeletons.py <卷1骨架.md> <卷2骨架.md> … -o <整书骨架.md>
报告写 <书>/_数据/过程报告/_merge_report.txt。stdout 一行 ASCII。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ANCHOR_RE, is_break, process_report, write_report  # noqa: E402

FM_RE = re.compile(r"^---\n.*?\n---\n", re.S)
NOTES_SPLIT = re.compile(r"\n+---\n+#+\s*注释\s*\n+")


def parse(text: str) -> tuple[str, str, str]:
    fm = ""
    match = FM_RE.match(text)
    if match:
        fm, text = match.group(0), text[match.end():]
    parts = NOTES_SPLIT.split(text, maxsplit=1)
    return fm, parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def shift(text: str, offset: int) -> str:
    return re.sub(r"\[\^(\d+)\]", lambda m: "[^%d]" % (int(m.group(1)) + offset), text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skeletons", nargs="+")
    parser.add_argument("-o", "--out", required=True)
    args = parser.parse_args()
    head, body_blocks, notes, offset = "", [], [], 0
    report = ["# 拆卷骨架合并报告\n\n"]
    for number, path in enumerate(args.skeletons, start=1):
        fm, body, note_text = parse(open(path, encoding="utf-8").read())
        head = head or fm
        defs = re.findall(r"^\[\^(\d+)\]:", note_text, re.M)
        blocks = [b for b in shift(body, offset).split("\n\n") if b.strip()]
        if body_blocks and blocks:
            # 接缝：前一卷最后一个正文段 + 后一卷第一个正文段（中间的页码锚点留在接缝处）
            anchors = []
            while blocks and ANCHOR_RE.match(blocks[0].strip()):
                anchors.append(blocks.pop(0).strip())
            prev, nxt = body_blocks[-1].strip(), (blocks[0].strip() if blocks else "")
            if blocks and is_break(prev, nxt):
                body_blocks[-1] = prev + "".join(anchors) + nxt
                blocks.pop(0)
                report.append("- 卷 %d 接缝：已接成一段 …%s ▷接▷ %s…\n" % (number, prev[-20:], nxt[:20]))
            else:
                blocks[:0] = anchors
                report.append("- 卷 %d 接缝：不接（前段已收句或后段像新段）…%s ｜ %s…\n" % (number, prev[-20:], nxt[:20]))
        body_blocks += blocks
        if note_text:
            notes.append(shift(note_text, offset))
        report.append("  卷 %d：%s，脚注 %d 条，编号从 %d 起\n" % (number, os.path.basename(path), len(defs), offset + 1))
        offset += len(defs)
    text = head + "\n\n".join(body_blocks) + ("\n\n---\n\n## 注释\n\n" + "\n\n".join(notes) if notes else "") + "\n"
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    report.append("\n合计脚注 %d 条。接下来跑 verify.py 看引用与定义是否一一对上。\n" % offset)
    write_report(process_report(os.path.dirname(os.path.abspath(args.out)), "_merge_report.txt"), "".join(report))
    print("MERGED volumes=%d notes=%d" % (len(args.skeletons), offset))


if __name__ == "__main__":
    main()
