"""把 PaddleOCR-VL 找到、MinerU 却整块漏掉的版面块，写成一份补充的 MinerU model JSON。

MinerU 偶尔整块漏掉署名、页下注、目录页这类块（layout 与 model 里都不存在），逐字映射的补行框
无从补起。这里取 vl_parse.py 存下的 VL 块坐标：块内若没有任何 MinerU 行框（ocr_text 的中心落在块内），
就按墨迹投影切出行（横排）或列（竖排），连同块文字写成 model JSON 条目。把输出文件作为一份
modelParts（physicalStart = --pages 的起页）追加进映射配置，映射程序就照常识别这些行、定逐字位置。

页眉、页脚、页码、图、表、公式不收：它们的文字不在 Markdown 里，补进来只会给对齐添乱。

用法：
  python -X utf8 vl_supplement.py --pdf 书.pdf --vl-dir <书>/_数据/VL --pages 1-497 \
      --model-part <MinerU>/xxx_model.json|1 [--model-part ...] --writing-mode vertical-rl \
      --out <书>/_数据/VL补块/model.json
stdout 只打一行 ASCII；报告写在输出旁的 .report.json。
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

SKIP_LABELS = {"header", "footer", "number", "header_image", "footer_image", "image", "table", "chart",
               "formula", "display_formula", "inline_formula", "seal", "algorithm", "formula_number"}
TITLE_LABELS = {"doc_title", "paragraph_title", "title"}
FOOTNOTE_LABELS = {"footnote", "vision_footnote", "page_footnote"}


def load_mineru_lines(parts: list[str]) -> dict[int, list[tuple[float, float, float, float]]]:
    lines: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    for value in parts:
        path, start = value.rsplit("|", 1)
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for index, page in enumerate(payload):
            for item in page:
                if item.get("type") == "ocr_text" and item.get("bbox") and len(item["bbox"]) == 4:
                    lines[int(start) + index].append(tuple(float(v) for v in item["bbox"]))
    return lines


def covered(rect, lines) -> bool:
    return any(rect[0] <= (l[0] + l[2]) / 2 <= rect[2] and rect[1] <= (l[1] + l[3]) / 2 <= rect[3] for l in lines)


def ink_lines(page, rect, vertical: bool) -> list[list[float]]:
    """块内按墨迹投影切行/列：竖排按 x 投影得列（从右到左），横排按 y 投影得行（从上到下）。"""
    import numpy as np
    import pymupdf

    width, height = page.rect.width, page.rect.height
    clip = pymupdf.Rect(rect[0] * width, rect[1] * height, rect[2] * width, rect[3] * height)
    pix = page.get_pixmap(dpi=144, clip=clip, colorspace=pymupdf.csGRAY)
    if pix.width < 2 or pix.height < 2:
        return []
    ink = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 140
    along = ink.sum(axis=0 if vertical else 1)
    length = pix.height if vertical else pix.width
    on = along > max(1, 0.01 * length)
    spans: list[list[int]] = []
    start = None
    for position, value in enumerate(list(on) + [False]):
        if value and start is None:
            start = position
        elif not value and start is not None:
            if position - start >= 3:
                spans.append([start, position])
            start = None
    merged: list[list[int]] = []
    for span in spans:  # 一个字的左右（或上下）部件之间的细缝不算行间
        if merged and span[0] - merged[-1][1] < 0.25 * (span[1] - span[0] + merged[-1][1] - merged[-1][0]):
            merged[-1][1] = span[1]
        else:
            merged.append(span)
    out = []
    for a, b in (reversed(merged) if vertical else merged):
        band = ink[:, a:b] if vertical else ink[a:b, :]
        extent = np.where(band.any(axis=1 if vertical else 0))[0]
        if not len(extent):
            continue
        if vertical:
            box = [a / pix.width, extent[0] / pix.height, b / pix.width, (extent[-1] + 1) / pix.height]
        else:
            box = [extent[0] / pix.width, a / pix.height, (extent[-1] + 1) / pix.width, b / pix.height]
        out.append([round(rect[0] + box[0] * (rect[2] - rect[0]), 6), round(rect[1] + box[1] * (rect[3] - rect[1]), 6),
                    round(rect[0] + box[2] * (rect[2] - rect[0]), 6), round(rect[1] + box[3] * (rect[3] - rect[1]), 6)])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--vl-dir", type=Path, required=True)
    parser.add_argument("--pages", required=True, help="physical pages, 1-based: 17 or 17-20")
    parser.add_argument("--model-part", action="append", required=True, help="MinerU *_model.json|physical-start")
    parser.add_argument("--writing-mode", choices=("horizontal-tb", "vertical-rl"), default="horizontal-tb")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import pymupdf

    first, _, last = args.pages.partition("-")
    first, last = int(first), int(last or first)
    doc = pymupdf.open(args.pdf)
    mineru = load_mineru_lines(args.model_part)
    vertical = args.writing_mode == "vertical-rl"
    pages_out: list[list[dict]] = []
    report = []
    for page_number in range(first, last + 1):
        items: list[dict] = []
        vl_path = args.vl_dir / f"p{page_number:04d}.json"
        if vl_path.exists():
            vl = json.loads(vl_path.read_text(encoding="utf-8"))
            width, height = vl["width"], vl["height"]
            for block in vl.get("parsing_res_list", []):
                label = block.get("block_label") or ""
                content = (block.get("block_content") or "").strip()
                if label in SKIP_LABELS or not content:
                    continue
                x0, y0, x1, y1 = block["block_bbox"]
                rect = (x0 / width, y0 / height, x1 / width, y1 / height)
                if covered(rect, mineru.get(page_number, [])):
                    continue
                lines = ink_lines(doc[page_number - 1], rect, vertical)
                if not lines:
                    continue
                kind = "title" if label in TITLE_LABELS else "page_footnote" if label in FOOTNOTE_LABELS else "text"
                items.append({"type": kind, "bbox": [round(v, 6) for v in rect], "angle": 0, "content": content,
                              "source": "PaddleOCR-VL", "vlLabel": label})
                items.extend({"type": "ocr_text", "bbox": line, "text": "", "score": 1.0, "source": "VL块墨迹投影"}
                             for line in lines)
                report.append({"page": page_number, "label": label, "kind": kind, "lines": len(lines),
                               "text": content[:30]})
        pages_out.append(items)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pages_out, ensure_ascii=False), encoding="utf-8")
    args.out.with_suffix(".report.json").write_text(
        json.dumps({"physicalStart": first, "blocks": report}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"VL_SUPPLEMENT pages={last - first + 1} blocks={len(report)} physicalStart={first}")


if __name__ == "__main__":
    main()
