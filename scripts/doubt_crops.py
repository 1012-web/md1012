# 从某书的 OCR 疑字清单取前 N 个「两家读法不一致」的字，按新映射截原图（整行附近 + 单字放大），
# 每 5 个拼一张图，写 manifest.json。只读笔记库，输出到 out_dir。
import gzip
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf
from PIL import Image, ImageDraw, ImageFont

vault = Path(r"D:\projects\313考研")
title, count, out_dir = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
out_dir.mkdir(parents=True, exist_ok=True)
note = (vault / "90-工作流/校勘记" / f"{title} OCR疑字.md").read_text("utf-8")
rows = []
for line in note.splitlines():
    match = re.match(r"\| (?P<page>[^|]+) \| (?P<context>[^|]+) \| MinerU 「(?P<m>[^」]*)」 · OCR 「(?P<o>[^」]*)」[^|]* \| \[定位\]\((?P<link>[^)]+)\) \|", line)
    if match:
        rows.append(match.groupdict())
    if len(rows) >= count:
        break
catalog = json.loads((vault / "90-工作流/MinerU联动/catalog.json").read_text("utf-8"))
chapters: dict[str, dict] = {}
docs: dict[str, pymupdf.Document] = {}
items = []
for number, row in enumerate(rows, start=1):
    query = parse_qs(urlparse(row["link"]).query)
    file, start = query["path"][0], int(query["from"][0])
    book = next(b for b in catalog["books"] if file.startswith(b["markdownRoot"] + "/"))
    if file not in chapters:
        index = json.loads((vault / book["index"]).read_text("utf-8"))
        chapters[file] = json.loads(gzip.decompress((vault / index["chapters"][file]["map"]).read_bytes()))
        docs[file] = pymupdf.open(vault / book["pdf"])
    hit = None
    for block in chapters[file]["blocks"]:
        for char in block.get("candidates", []) + block["chars"]:
            if char["from"] == start:
                hit = char
                break
        if hit:
            break
    page = docs[file][hit["page"] - 1]
    w, h = page.rect.width, page.rect.height
    x0, y0, x1, y1 = hit["rect"]
    line = page.get_pixmap(dpi=220, clip=pymupdf.Rect(max(0, x0 - 0.22) * w, (y0 - 0.006) * h, min(1, x1 + 0.22) * w, (y1 + 0.006) * h))
    zoom = page.get_pixmap(dpi=500, clip=pymupdf.Rect((x0 - 0.01) * w, (y0 - 0.003) * h, (x1 + 0.01) * w, (y1 + 0.003) * h))
    line_path = out_dir / f"{number:02d}-line.png"
    zoom_path = out_dir / f"{number:02d}-zoom.png"
    line.save(line_path)
    zoom.save(zoom_path)
    items.append({"n": number, "page": row["page"].strip(), "context": row["context"].strip(), "minerU": row["m"], "ocr": row["o"], "file": file, "from": start, "pdfPage": hit["page"], "line": line_path.name, "zoom": zoom_path.name})

# 每 5 个拼一张：左边编号，中间整行，右边放大
font = ImageFont.load_default(size=28)
for sheet in range(0, len(items), 5):
    group = items[sheet:sheet + 5]
    tiles = []
    for item in group:
        line_img = Image.open(out_dir / item["line"]).convert("RGB")
        zoom_img = Image.open(out_dir / item["zoom"]).convert("RGB")
        scale = 90 / line_img.height
        line_img = line_img.resize((int(line_img.width * scale), 90))
        zoom_img = zoom_img.resize((int(zoom_img.width * 90 / zoom_img.height), 90))
        tile = Image.new("RGB", (70 + line_img.width + 20 + zoom_img.width, 100), "white")
        ImageDraw.Draw(tile).text((5, 30), f"#{item['n']}", fill="red", font=font)
        tile.paste(line_img, (70, 5))
        tile.paste(zoom_img, (70 + line_img.width + 20, 5))
        tiles.append(tile)
    width = max(t.width for t in tiles)
    sheet_img = Image.new("RGB", (width, 100 * len(tiles)), "white")
    for index, tile in enumerate(tiles):
        sheet_img.paste(tile, (0, index * 100))
    sheet_img.save(out_dir / f"sheet-{sheet // 5 + 1}.png")
(out_dir / "manifest.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
print("items", len(items), "sheets", (len(items) + 4) // 5)
