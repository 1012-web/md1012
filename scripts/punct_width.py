"""中文语境里的半角标点统一成全角：, : ; ? ! ( ) → ，：；？！（）；汉字之间多余的空格删去。

只动挨着汉字（或中文标点）的半角标点；两侧都是西文、数字的不动（「1,000」「pp. 3-5」「(1949)」英文书目照旧）。
括号要整对转：「(」后面或「)」前面是汉字，且同一段里配得上对，才把这一对换成全角。
代码、链接、页码锚点 <!--…-->、HTML 标签、脚注标签 [^n] 不碰。改的是标点宽度，字一个不动，
diff_report.py 不会报出它（只比实义字符）；交付报告里概括一句「半角标点统一为全角 N 处」即可。

用法：
  python -X utf8 punct_width.py <成品.md> [<成品2.md> …] [--dry-run]
改前备份写进 <书>/_数据/过程报告/<文件名>.before-punct.md，统计写 _punct_width.txt。stdout 一行 ASCII。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import process_report, write_report  # noqa: E402

HAN = r"[㐀-䶿一-鿿〇々「」『』《》〈〉（）【】、。，；：？！…—]"
PAIRS = {",": "，", ":": "：", ";": "；", "?": "？", "!": "！"}
# 不碰的片段：代码、链接、注释、标签、脚注标签、frontmatter 里的键
PROTECT = re.compile(r"```.*?```|`[^`\n]*`|<!--.*?-->|<[^<>\n]+>|\[\^\d+\]:?|\]\([^)\n]*\)|https?://\S+", re.S)


def convert(text: str) -> tuple[str, dict[str, int]]:
    counts = {"punct": 0, "paren": 0, "space": 0}
    pieces, last = [], 0
    for match in PROTECT.finditer(text):
        pieces.append(("text", text[last:match.start()]))
        pieces.append(("keep", match.group(0)))
        last = match.end()
    pieces.append(("text", text[last:]))
    out = []
    for kind, piece in pieces:
        if kind == "keep":
            out.append(piece)
            continue
        # 半角标点：前一个字是汉字，或后一个非空白字是汉字
        def single(m: re.Match) -> str:
            counts["punct"] += 1
            return PAIRS[m.group(2)]
        piece = re.sub(r"(?<=" + HAN + r")(\s*)([,:;?!])\s*", lambda m: single(m), piece)
        piece = re.sub(r"\s*([,:;?!])\s*(?=" + HAN + r")",
                       lambda m: (counts.__setitem__("punct", counts["punct"] + 1), PAIRS[m.group(1)])[1], piece)

        # 括号整对：里面或外侧挨着汉字才换
        def paren(m: re.Match) -> str:
            inner = m.group(1)
            if re.search(HAN, inner) or re.search(HAN + r"$", piece[:m.start()]):
                counts["paren"] += 1
                return "（" + inner.strip() + "）"
            return m.group(0)
        piece = re.sub(r"\(([^()\n]*)\)", paren, piece)
        # 汉字之间的空格（OCR 断行、排版留下的）：删。西文、数字两侧的空格不动
        piece, n = re.subn(r"(?<=[㐀-䶿一-鿿])[ 　]+(?=[㐀-䶿一-鿿])", "", piece)
        counts["space"] += n
        out.append(piece)
    return "".join(out), counts


def split_frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"^---\n.*?\n---\n", text, re.S)
    return (match.group(0), text[match.end():]) if match else ("", text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("md", nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不改文件")
    args = parser.parse_args()
    report = ["# 半角标点统一为全角\n\n"]
    total = {"punct": 0, "paren": 0, "space": 0}
    for path in args.md:
        text = open(path, encoding="utf-8").read()
        head, body = split_frontmatter(text)  # frontmatter 是 YAML，冒号不能动
        new, counts = convert(body)
        for key in total:
            total[key] += counts[key]
        report.append("- %s：标点 %d、括号 %d 对、汉字间空格 %d\n"
                      % (os.path.basename(path), counts["punct"], counts["paren"], counts["space"]))
        if not args.dry_run and new != body:
            backup = process_report(os.path.dirname(os.path.abspath(path)),
                                    os.path.basename(path)[:-3] + ".before-punct.md")
            shutil.copyfile(path, backup)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(head + new)
    report.append("\n合计：标点 %d、括号 %d 对、汉字间空格 %d。%s\n"
                  % (total["punct"], total["paren"], total["space"], "（只统计，未改文件）" if args.dry_run else ""))
    write_report(process_report(os.path.dirname(os.path.abspath(args.md[0])), "_punct_width.txt"), "".join(report))
    print("PUNCT_WIDTH punct=%d paren=%d space=%d dry=%s" % (total["punct"], total["paren"], total["space"], args.dry_run))


if __name__ == "__main__":
    main()
