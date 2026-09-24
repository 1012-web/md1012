# 把待站长判定的几处疑字（整行 + 单字放大）拼成一张图，每处标编号、页码、正文现在的写法
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

d = Path(sys.argv[1])
numbers = [int(n) for n in sys.argv[2].split(",")]
manifest = {i["n"]: i for i in json.loads((d / "manifest.json").read_text("utf-8"))}
font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 26)
rows = []
for n in numbers:
    item = manifest[n]
    line = Image.open(d / item["line"]).convert("RGB")
    zoom = Image.open(d / item["zoom"]).convert("RGB")
    line = line.resize((int(line.width * 110 / line.height), 110))
    zoom = zoom.resize((int(zoom.width * 110 / zoom.height), 110))
    label = f"#{n}　书上 {item['page']}　PDF 第 {item['pdfPage']} 页　正文现作「{item['minerU']}」"
    row = Image.new("RGB", (max(line.width + zoom.width + 30, 900), 160), "white")
    draw = ImageDraw.Draw(row)
    draw.text((10, 5), label, fill="red", font=font)
    row.paste(line, (10, 45))
    row.paste(zoom, (line.width + 30, 45))
    rows.append(row)
width = max(r.width for r in rows)
sheet = Image.new("RGB", (width, sum(r.height for r in rows)), "white")
y = 0
for r in rows:
    sheet.paste(r, (0, y))
    y += r.height
out = d / "pending-for-owner.png"
sheet.save(out)
print(out)
