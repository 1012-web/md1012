"""按逐行映射截疑字所在的整行（竖排是整列）原图、拼图，交子代理判定或在对话里给用户看。

疑字清单来自 vl_compare.py 的 doubts.json（底稿与 VL 读法不同的地方）。逐行映射只知道字在哪一行、
不知道在行里的哪一格，所以截整行、不画红线：每张小图旁边写编号、页码和前后文（疑字用【】标出），
判定的人对着前后文在行里找到那个字。逐页退路的疑字没有行框，截整页。

  crops  每条截一张图，每 5 条拼一张 sheet-N.png，写 manifest.json（doubts.json 各字段，加 line（截图文件名））。
         交子代理判定，结果 judgement-bK.json 格式见 references/judging-prompt.md。
  owner  把要用户拍板的几组编号拼成带说明的图（发在对话里给用户看）：--groups 是 JSON {"组名": [编号, ...], ...}。

用法：
  python -X utf8 doubt_sheets.py crops --pdf 书.pdf --doubts <书>/_数据/疑字判定/doubts.json --out <书>/_数据/疑字判定
  python -X utf8 doubt_sheets.py owner --dir <书>/_数据/疑字判定 --groups groups.json
"""
from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

PER_SHEET = 5


def crop_line(doc, item: dict, path: Path) -> None:
    import pymupdf

    page = doc[item["pdfPage"] - 1]
    w, h = page.rect.width, page.rect.height
    if item.get("rect") is None:  # 逐页退路：截整页
        page.get_pixmap(dpi=110).save(path)
        return
    x0, y0, x1, y1 = item["rect"]
    if is_column(item):  # 整列，左右各带一点邻列
        box = (x0 - 0.02, y0 - 0.01, x1 + 0.02, y1 + 0.01)
    else:  # 整行，上下各带一点邻行
        box = (x0 - 0.01, y0 - 0.008, x1 + 0.01, y1 + 0.008)
    box = [max(0.0, box[0]), max(0.0, box[1]), min(1.0, box[2]), min(1.0, box[3])]
    page.get_pixmap(dpi=220, clip=pymupdf.Rect(box[0] * w, box[1] * h, box[2] * w, box[3] * h)).save(path)


def is_column(item: dict) -> bool:
    """竖排书的行是列；横排书里也会有竖排的块（封底、书脊），按行框的形状认。"""
    rect = item.get("rect")
    if rect is None:
        return False
    return item.get("writingMode") == "vertical-rl" or (rect[3] - rect[1]) > 2 * (rect[2] - rect[0])


def font_of(size: int):
    from PIL import ImageFont

    for path in (r"C:\Windows\Fonts\msyh.ttc", "/System/Library/Fonts/PingFang.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def caption(item: dict) -> list[str]:
    context = item["context"].replace("**", "【", 1).replace("**", "】", 1)
    return [f"#{item['n']}  {item['page']}  PDF 第 {item['pdfPage']} 页",
            f"正文「{item['minerU'] or '（无）'}」  VL「{item['ocr'] or '（无）'}」",
            *textwrap.wrap(context, 26)]


def tile(folder: Path, item: dict, font):
    """竖排：左边整列图、右边说明；横排与整页：上边说明、下边图。"""
    from PIL import Image, ImageDraw

    image = Image.open(folder / item["line"]).convert("RGB")
    text = caption(item)
    step = font.size + 8
    if is_column(item):
        image = image.resize((max(1, int(image.width * 640 / image.height)), 640))
        canvas = Image.new("RGB", (image.width + 420, max(image.height, step * len(text)) + 20), "white")
        canvas.paste(image, (10, 10))
        draw = ImageDraw.Draw(canvas)
        for i, row in enumerate(text):
            draw.text((image.width + 25, 10 + i * step), row, fill="red" if i == 0 else "black", font=font)
        return canvas
    if item.get("rect") is None:
        image = image.resize((640, max(1, int(image.height * 640 / image.width))))
    else:
        image = image.resize((max(1, int(image.width * 70 / image.height)), 70))
    top = step * len(text) + 10
    canvas = Image.new("RGB", (max(image.width + 20, 640), top + image.height + 20), "white")
    draw = ImageDraw.Draw(canvas)
    for i, row in enumerate(text):
        draw.text((10, 5 + i * step), row, fill="red" if i == 0 else "black", font=font)
    canvas.paste(image, (10, top))
    return canvas


def stack(tiles: list, title: str | None, font):
    """竖排的小图横着排（每张都高），横排的竖着摞（每张都宽）。"""
    from PIL import Image, ImageDraw

    top = font.size + 20 if title else 0
    if all(t.height > t.width for t in tiles):
        sheet = Image.new("RGB", (sum(t.width + 10 for t in tiles), max(t.height for t in tiles) + top), "white")
        x = 0
        for t in tiles:
            sheet.paste(t, (x, top))
            x += t.width + 10
    else:
        sheet = Image.new("RGB", (max(t.width for t in tiles), sum(t.height for t in tiles) + top), "white")
        y = top
        for t in tiles:
            sheet.paste(t, (0, y))
            y += t.height
    if title:
        ImageDraw.Draw(sheet).text((8, 6), title, fill="black", font=font)
    return sheet


def crops(pdf: Path, doubts_path: Path, out: Path) -> None:
    import pymupdf

    items = json.loads(doubts_path.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    for item in items:
        item["line"] = f"{item['n']:04d}-line.png"
        crop_line(doc, item, out / item["line"])
    font = font_of(22)
    for start in range(0, len(items), PER_SHEET):
        group = items[start:start + PER_SHEET]
        stack([tile(out, it, font) for it in group], None, font).save(out / f"sheet-{start // PER_SHEET + 1}.png")
    (out / "manifest.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"DOUBT_SHEETS items={len(items)} sheets={(len(items) + PER_SHEET - 1) // PER_SHEET}")


def owner(folder: Path, groups_path: Path) -> None:
    manifest = {m["n"]: m for m in json.loads((folder / "manifest.json").read_text(encoding="utf-8"))}
    groups = json.loads(groups_path.read_text(encoding="utf-8"))
    out = folder / "用户拍板"
    out.mkdir(exist_ok=True)
    font = font_of(22)
    names = []
    for g, (title, numbers) in enumerate(groups.items(), start=1):
        for start in range(0, len(numbers), 6):
            tiles = [tile(folder, manifest[n], font) for n in numbers[start:start + 6]]
            name = f"组{g}-{start // 6 + 1}.png"
            stack(tiles, title, font).save(out / name)
            names.append(name)
    print(f"OWNER_SHEETS {len(names)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("crops")
    c.add_argument("--pdf", type=Path, required=True)
    c.add_argument("--doubts", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    o = sub.add_parser("owner")
    o.add_argument("--dir", type=Path, required=True)
    o.add_argument("--groups", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "crops":
        crops(args.pdf, args.doubts, args.out)
    else:
        owner(args.dir, args.groups)


if __name__ == "__main__":
    main()
