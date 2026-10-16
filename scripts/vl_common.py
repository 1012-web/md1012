"""VL 版面块的公用部分：哪些块不进正文、块内按墨迹投影切行（横排）或切列（竖排）。

line_map.py（逐行映射）与 vl_build.py（VL 作底稿时整理 Markdown）共用。
"""
from __future__ import annotations

# 页眉、页脚、页码、图、表、公式：它们的文字不在正文里（图表另行处理），不映射、不比对
SKIP_LABELS = {"header", "footer", "number", "header_image", "footer_image", "image", "table", "chart",
               "formula", "display_formula", "inline_formula", "seal", "algorithm", "formula_number"}
TITLE_LABELS = {"doc_title", "paragraph_title", "title"}
FOOTNOTE_LABELS = {"footnote", "vision_footnote", "page_footnote"}


def ink_lines(page, rect, vertical: bool) -> list[list[float]]:
    """块内按墨迹投影切行/列：竖排按 x 投影得列（从右到左），横排按 y 投影得行（从上到下）。

    rect 与返回的框都是页面 0–1 比例坐标。一个字左右（或上下）部件之间的细缝不算行间。
    """
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
    for span in spans:
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
