"""按逐字映射截疑字原图、拼图（不需要 Obsidian 与笔记1012 插件；横排、竖排都行）。

与 doubt_crops.py 的分工：那个读插件生成的「OCR疑字.md」、要书在 Obsidian 库里；这个直接读映射（book.json 指的
index.json 与各章 sidecar 的 candidates），书文件夹在哪都行。

  crops  全部疑字截图：每个疑字一张「整行/整列」图（红线夹住疑字）与一张单字放大图，每 5 个拼一张 sheet-N.png，
         写 manifest.json（n、file、from/to（UTF-16 偏移）、pdfPage、page（印刷页）、context、minerU（正文现字）、
         ocr（本机 OCR 读法）、alternatives、line、zoom）。交子代理判定，结果 judgement-bK.json 格式同 judging-prompt.md。
  owner  把要站长拍板的几组编号拼成带说明的图：--groups 是 JSON {"组名": [编号, ...], ...}。

用法：
  python -X utf8 doubt_sheets.py crops --book <书> --out <书>/_数据/疑字判定
  python -X utf8 doubt_sheets.py owner --dir <书>/_数据/疑字判定 --groups groups.json
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

PER_SHEET = 5


def load_candidates(book: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((book / "_数据" / "book.json").read_text(encoding="utf-8"))
    index = json.loads((book / meta["index"]).read_text(encoding="utf-8"))
    config = json.loads((book / meta.get("rebuild", {}).get("config", "_数据/config.json")).read_text(encoding="utf-8"))
    offsets = {name: config.get("printedPageOffset", 0) for name in config.get("markdownFiles", [])}
    for item in config.get("fileOffsets", []):
        name, value = item.rsplit("|", 1)
        offsets[name] = int(value)
    items = []
    for name in config.get("markdownFiles", index["chapters"].keys()):
        chapter = index["chapters"].get(name)
        if not chapter:
            continue
        text = (book / name).read_text(encoding="utf-8").encode("utf-16-le")
        sidecar = json.loads(gzip.decompress((book / chapter["map"]).read_bytes()))
        for block in sidecar["blocks"]:
            for c in block.get("candidates", []):
                seg = lambda a, b: text[max(0, a) * 2:b * 2].decode("utf-16-le", "ignore")
                items.append({"file": name, "from": c["from"], "to": c["to"], "pdfPage": c["page"], "rect": c["rect"],
                              "page": f"p.{c['page'] - offsets.get(name, 0)}", "minerU": seg(c["from"], c["to"]),
                              "ocr": c["value"], "alternatives": c.get("alternatives", []),
                              "context": seg(c["from"] - 12, c["from"]) + "**" + seg(c["from"], c["to"]) + "**" + seg(c["to"], c["to"] + 12)})
    return {"pdf": book / meta["pdf"], "vertical": config.get("writingMode") == "vertical-rl"}, items


def crop_pair(doc, item: dict, vertical: bool, line_path: Path, zoom_path: Path) -> None:
    import pymupdf
    from PIL import Image, ImageDraw

    page = doc[item["pdfPage"] - 1]
    w, h = page.rect.width, page.rect.height
    x0, y0, x1, y1 = item["rect"]
    # 整行（横排）或整列（竖排）：沿书写方向多取一截，另一方向只留一点边
    if vertical:
        box = (max(0, x0 - 0.012), max(0, y0 - 0.14), min(1, x1 + 0.012), min(1, y1 + 0.14))
    else:
        box = (max(0, x0 - 0.22), max(0, y0 - 0.006), min(1, x1 + 0.22), min(1, y1 + 0.006))
    dpi = 200
    page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(box[0] * w, box[1] * h, box[2] * w, box[3] * h)).save(line_path)
    page.get_pixmap(dpi=500, clip=pymupdf.Rect((x0 - 0.012) * w, (y0 - 0.02) * h, (x1 + 0.012) * w, (y1 + 0.02) * h)).save(zoom_path)
    image = Image.open(line_path).convert("RGB")
    k = dpi / 72
    ax, ay = (x0 * w - box[0] * w) * k, (y0 * h - box[1] * h) * k
    bx, by = (x1 * w - box[0] * w) * k, (y1 * h - box[1] * h) * k
    draw = ImageDraw.Draw(image)
    # 红色短线夹住疑字（竖排画在左右两侧的上下沿，横排画在上下两侧的左右沿）
    if vertical:
        for y in (ay, by):
            draw.line([(0, y), (max(2, ax - 3), y)], fill="red", width=3)
            draw.line([(min(image.width - 2, bx + 3), y), (image.width, y)], fill="red", width=3)
    else:
        for x in (ax, bx):
            draw.line([(x, 0), (x, max(2, ay - 3))], fill="red", width=3)
            draw.line([(x, min(image.height - 2, by + 3)), (x, image.height)], fill="red", width=3)
    image.save(line_path)


def tile(out: Path, item: dict, vertical: bool, label: str, font, small=None):
    from PIL import Image, ImageDraw

    line = Image.open(out / item["line"]).convert("RGB")
    zoom = Image.open(out / item["zoom"]).convert("RGB")
    header = 40 if small is None else 90
    if vertical:
        line = line.resize((max(1, int(line.width * 480 / line.height)), 480))
        zoom = zoom.resize((140, max(1, int(zoom.height * 140 / zoom.width))))
        width = max(line.width, 150, 200 if small else 0) + 20
        canvas = Image.new("RGB", (width, header + zoom.height + 10 + line.height), "white")
        canvas.paste(zoom, ((width - zoom.width) // 2, header))
        canvas.paste(line, ((width - line.width) // 2, header + zoom.height + 10))
    else:
        line = line.resize((max(1, int(line.width * 90 / line.height)), 90))
        zoom = zoom.resize((max(1, int(zoom.width * 90 / zoom.height)), 90))
        canvas = Image.new("RGB", (max(line.width + zoom.width + 30, 420), header + 100), "white")
        canvas.paste(line, (5, header + 5))
        canvas.paste(zoom, (line.width + 25, header + 5))
    draw = ImageDraw.Draw(canvas)
    draw.text((6, 4), label, fill="red", font=font)
    if small is not None:
        draw.text((6, 36), f"正文「{item['minerU']}」 OCR「{item['ocr']}」", fill="black", font=small)
        draw.text((6, 60), f"{item['page']} {item['file']}", fill="gray", font=small)
    return canvas


def paste_row(tiles: list, vertical: bool, title: str | None, font):
    from PIL import Image, ImageDraw

    top = 40 if title else 0
    if vertical:
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


def font_of(size: int):
    from PIL import ImageFont

    for path in (r"C:\Windows\Fonts\msyh.ttc", "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def crops(book: Path, out: Path) -> None:
    import pymupdf

    info, items = load_candidates(book)
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(info["pdf"])
    for n, item in enumerate(items, start=1):
        item.update({"n": n, "kind": "disagree", "line": f"{n:03d}-line.png", "zoom": f"{n:03d}-zoom.png"})
        crop_pair(doc, item, info["vertical"], out / item["line"], out / item["zoom"])
    font = font_of(26)
    for start in range(0, len(items), PER_SHEET):
        group = items[start:start + PER_SHEET]
        paste_row([tile(out, it, info["vertical"], f"#{it['n']}", font) for it in group], info["vertical"], None, font) \
            .save(out / f"sheet-{start // PER_SHEET + 1}.png")
    for item in items:
        item.pop("rect", None)
    (out / "manifest.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"DOUBT_SHEETS items={len(items)} sheets={(len(items) + PER_SHEET - 1) // PER_SHEET} vertical={info['vertical']}")


def owner(folder: Path, groups_path: Path) -> None:
    manifest = {m["n"]: m for m in json.loads((folder / "manifest.json").read_text(encoding="utf-8"))}
    groups = json.loads(groups_path.read_text(encoding="utf-8"))
    # 整列图竖长、整行图横长：由第一张截图判断这本书的排版方向
    vertical = bool(manifest) and _is_vertical(folder / next(iter(manifest.values()))["line"])
    out = folder / "站长拍板"
    out.mkdir(exist_ok=True)
    font, small = font_of(22), font_of(17)
    names = []
    for g, (title, numbers) in enumerate(groups.items(), start=1):
        for start in range(0, len(numbers), 6):
            tiles = [tile(folder, manifest[n], vertical, f"#{n}  PDF {manifest[n]['pdfPage']}", font, small)
                     for n in numbers[start:start + 6]]
            name = f"组{g}-{start // 6 + 1}.png"
            paste_row(tiles, vertical, title, font).save(out / name)
            names.append(name)
    print(f"OWNER_SHEETS {len(names)}")


def _is_vertical(path: Path) -> bool:
    from PIL import Image

    with Image.open(path) as image:
        return image.height > image.width


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("crops")
    c.add_argument("--book", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    o = sub.add_parser("owner")
    o.add_argument("--dir", type=Path, required=True)
    o.add_argument("--groups", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "crops":
        crops(args.book.resolve(), args.out)
    else:
        owner(args.dir, args.groups)


if __name__ == "__main__":
    main()
