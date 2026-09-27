"""按校订后的 Markdown 重建 PDF 的隐形文字层（「校订版 PDF」）。

逐字映射给出了每个字在原页上的位置；Obsidian 插件笔记1012 把「当前正文的每个字 + 它在原页上的框」
导出成一份清单（spec），本脚本据此：

1. 删掉每页原有的文字层，只删文字、不动图片与线条；删之前后各渲染一次低分辨率图逐像素比较，
   外观有变（说明那页的文字本来就看得见）就保留原页不动，写进报告。
2. 按清单在每个字的位置写入看不见的文字（渲染模式 3），顺序与正文一致，搜索、复制、
   Zotero 划线摘出来的都是校订后的字。
3. 回读核对：每页抽出的文字与清单逐字比较。

原 PDF 永不修改：build 只写新文件；replace 先把目标备份到指定目录再原子替换。
stdout 只打 ASCII 行（Windows 控制台是 GBK），报告写成 UTF-8 JSON。

用法：
  python -X utf8 pdf_textlayer.py build --pdf 原件.pdf --spec 清单.json.gz --output 校订版.pdf
  python -X utf8 pdf_textlayer.py find-copies --pdf 原件.pdf --search-root ~/Zotero/storage
  python -X utf8 pdf_textlayer.py replace --target 要换的.pdf --source 校订版.pdf --backup-dir 备份目录 --expect-sha256 <原件哈希>
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 隐形文字只要字体里有这个码位（抽取时才能还原成原字），字形不显示；首选 PyMuPDF 自带的中日韩字体，
# 缺的字依次去这些系统字体里找（实测 9 本教材 5431 个用字，自带字体缺「−」「䜣」，微软雅黑补齐）。
FALLBACK_FONT_FILES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)
# 删文字前后比较外观用的分辨率：够看出一行字，又快
VERIFY_DPI = 36
# 允许的像素差比例（抗锯齿边缘的偶发差异）；超过就当作「文字本来看得见」，保留原页
VERIFY_TOLERANCE = 0.0005


@dataclass
class PageResult:
    page: int
    written: int = 0
    removed_old: bool = False
    kept_old_layer: bool = False
    extracted_match: float | None = None
    notes: list[str] = field(default_factory=list)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_spec(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    spec = json.loads(raw.decode("utf-8"))
    if spec.get("version") != 1 or not isinstance(spec.get("pages"), dict):
        raise ValueError("spec must be version 1 with a pages object")
    return spec


class FontSet:
    """首选字体加后备字体；每个字挑第一个有它的字体。"""

    def __init__(self, fallback_files: tuple[str, ...] = FALLBACK_FONT_FILES) -> None:
        import pymupdf

        self.fonts = [pymupdf.Font("cjk")]
        for file in fallback_files:
            if Path(file).exists():
                self.fonts.append(pymupdf.Font(fontfile=file))
        self.missing: set[str] = set()

    def pick(self, char: str) -> Any:
        code = ord(char[0])
        for font in self.fonts:
            if font.has_glyph(code):
                return font
        self.missing.add(char)
        return self.fonts[0]


def pixmap_digest(page: Any, dpi: int = VERIFY_DPI) -> tuple[int, int, int, bytes]:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    return pixmap.width, pixmap.height, pixmap.n, bytes(pixmap.samples)


def changed_ratio(before: tuple[int, int, int, bytes], after: tuple[int, int, int, bytes]) -> float:
    if before[:3] != after[:3]:
        return 1.0
    width, height, channels, left = before
    right = after[3]
    if left == right:
        return 0.0
    differ = sum(1 for index in range(0, len(left), channels) if left[index:index + channels] != right[index:index + channels])
    return differ / max(1, width * height)


def remove_text_layer(page: Any, verify: bool = True) -> tuple[bool, float]:
    """删掉整页文字（图片、线条不动）。返回（是否删了，外观差异比例）；外观有变时由调用方恢复原页。"""
    import pymupdf

    if not page.get_text("text").strip() and not page.get_texttrace():
        return False, 0.0
    before = pixmap_digest(page) if verify else None
    page.add_redact_annot(page.rect)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )
    ratio = changed_ratio(before, pixmap_digest(page)) if before is not None else 0.0
    return True, ratio


def write_text_layer(page: Any, chars: list[list[Any]], fonts: FontSet) -> int:
    """按清单写隐形文字。chars 每项 [字, x0, y0, x1, y1]，坐标是相对页面的 0–1 比例。"""
    import pymupdf

    if not chars:
        return 0
    width, height = page.rect.width, page.rect.height
    writer = pymupdf.TextWriter(page.rect)
    written = 0
    for value, x0, y0, x1, y1 in chars:
        if not value or not value.strip():
            continue
        font = fonts.pick(value)
        box_height = max(0.5, (y1 - y0) * height)
        # 字号让字体的上下伸展正好填满字框高度，基线落在框内按比例的位置
        extent = max(0.5, font.ascender - font.descender)
        size = box_height / extent
        baseline = y0 * height + size * font.ascender
        writer.append((x0 * width, baseline), value, font=font, fontsize=size)
        written += len(value)
    writer.write_text(page, render_mode=3)
    return written


def split_columns(chars: list[list[Any]]) -> list[list[list[Any]]]:
    """竖排清单按阅读顺序切成列：纵向回到上方（换到下一列开头）或横向中心差出约一列宽，才算新列。"""
    columns: list[list[list[Any]]] = []
    for item in chars:
        if columns:
            last = columns[-1][-1]
            width = max(last[3] - last[1], item[3] - item[1])
            same = item[2] >= last[2] - 0.01 and abs((last[1] + last[3]) / 2 - (item[1] + item[3]) / 2) < 0.8 * width
            if same:
                columns[-1].append(item)
                continue
        columns.append([item])
    return columns


def write_text_layer_vertical(page: Any, chars: list[list[Any]], fonts: FontSet) -> int:
    """竖排：每一列写成一条旋转 90° 的隐形文字行，列内每个字仍落在自己的框里。

    逐字各写一行（横排的写法）在竖排书上，PDF.js 会把每个字当成一行，复制出来一字一行。
    整列一行才像普通竖排 PDF：复制一列一行、搜索跨列也连得上。三条要点（4 页试水实测得来）：
    整列用同一条基线（字号不同基线就错开，阅读器会断行）；每个字的前进宽度正好填满字框高度；
    与上一字之间不到四个字距的缝接上（否则 PDF.js 在字间插空格、甚至断行，搜「治生治産」搜不到）。
    """
    import pymupdf

    if not chars:
        return 0
    width, height = page.rect.width, page.rect.height
    main = fonts.fonts[0]
    extent = max(0.5, main.ascender - main.descender)
    rotate = pymupdf.Matrix(-90)  # 未旋转坐标 (u, v) -> 页面 (-v, u)：u 沿列向下，字顶朝右
    written = 0
    for column in split_columns([item for item in chars if item[0] and item[0].strip()]):
        x0 = sorted(item[1] for item in column)[len(column) // 2] * width
        x1 = sorted(item[3] for item in column)[len(column) // 2] * width
        base_x = x0 + (-main.descender) * (max(1.0, x1 - x0) / extent)  # 字顶朝右，基线在列框左侧
        pitch = sorted((item[4] - item[2]) * height for item in column)[len(column) // 2]
        writer = pymupdf.TextWriter(page.rect)
        cursor = None
        for value, _, cy0, _, cy1 in column:
            font = fonts.pick(value)
            top, bottom = cy0 * height, cy1 * height
            # 列内不到四个字距的缝都接上：纸面上的注号〔一〕不在正文字里、句号后常留一格，合起来能空出
            # 两三格；竖排的段落在列尾结束、下一段另起一列，所以列中的空缺都在一段话之内。
            # 更大的空白（题目与署名之间）照留，阅读器按空格或断行处理。
            if cursor is not None and 0 <= top - cursor < 4 * pitch:
                top = cursor
            per_point = max(1e-3, font.text_length(value, fontsize=1))
            size = max(0.5, (bottom - top) / per_point)
            writer.append((top, -base_x), value, font=font, fontsize=size)
            cursor = top + size * per_point
            written += len(value)
        writer.write_text(page, render_mode=3, morph=(pymupdf.Point(0, 0), rotate))
    return written


def comparable(text: str) -> str:
    return "".join(char for char in text if not char.isspace())


def match_ratio(expected: str, actual: str) -> float:
    import difflib

    if not expected:
        return 1.0
    return difflib.SequenceMatcher(None, expected, actual, autojunk=False).ratio()


def build(
    pdf: Path, spec: dict[str, Any], output: Path, verify: bool = True, writing_mode: str | None = None
) -> dict[str, Any]:
    import pymupdf

    writing_mode = writing_mode or spec.get("writingMode", "horizontal-tb")
    if writing_mode not in {"horizontal-tb", "vertical-rl"}:
        raise ValueError("writingMode must be horizontal-tb or vertical-rl")
    write = write_text_layer_vertical if writing_mode == "vertical-rl" else write_text_layer
    started = time.monotonic()
    source_hash = sha256(pdf)
    original = pymupdf.open(pdf)
    work = pymupdf.open(pdf)
    fonts = FontSet()
    results: list[PageResult] = []
    pages = spec["pages"]
    for index in range(work.page_count):
        number = index + 1
        result = PageResult(page=number)
        page = work[index]
        if str(number) not in pages:
            # 清单里没有这页（封面、插图页、没进正文的页，或增量同步时没改的页）：原有文字层照留
            results.append(result)
            continue
        # 列在清单里但一个字也没有：这页的正文都删了，旧文字层删掉、不写新的
        chars = pages[str(number)]
        removed, ratio = remove_text_layer(page, verify)
        if removed and ratio > VERIFY_TOLERANCE:
            # 那页的字本来就看得见（不是扫描图上的隐形层）：换回原页，不动它
            work.delete_page(index)
            work.insert_pdf(original, from_page=index, to_page=index, start_at=index)
            page = work[index]
            result.kept_old_layer = True
            result.notes.append(f"removing text changed {ratio:.4%} of pixels; original page kept")
        else:
            result.removed_old = removed
            result.written = write(page, chars, fonts)
            if chars:
                expected = comparable("".join(item[0] for item in chars))
                result.extracted_match = round(match_ratio(expected, comparable(page.get_text("text"))), 4)
        results.append(result)
    stamp = datetime.now().strftime("%Y-%m-%d")
    metadata = dict(work.metadata or {})
    note = f"text layer rebuilt from corrected Markdown by note1012 / mineru pdf_textlayer on {stamp}"
    metadata["keywords"] = f"{metadata.get('keywords') or ''} {note}".strip()
    work.set_metadata(metadata)
    work.subset_fonts()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    work.save(temporary, garbage=3, deflate=True)
    work.close()
    original.close()
    os.replace(temporary, output)
    scored = [item.extracted_match for item in results if item.extracted_match is not None]
    return {
        "version": 1,
        "source": str(pdf),
        "sourceSha256": source_hash,
        "sourceBytes": pdf.stat().st_size,
        "output": str(output),
        "writingMode": writing_mode,
        "outputSha256": sha256(output),
        "outputBytes": output.stat().st_size,
        "pages": len(results),
        "pagesWithText": sum(1 for item in results if item.written),
        "pagesRewritten": sum(1 for item in results if str(item.page) in pages and not item.kept_old_layer),
        "pagesUntouched": sum(1 for item in results if str(item.page) not in pages),
        "charactersWritten": sum(item.written for item in results),
        "pagesKeptOldLayer": [item.page for item in results if item.kept_old_layer],
        "lowestMatch": min(scored) if scored else None,
        "pagesBelow99": [item.page for item in results if item.extracted_match is not None and item.extracted_match < 0.99],
        "fontMissing": sorted(fonts.missing),
        "elapsedSeconds": round(time.monotonic() - started, 2),
        "sourceUnchanged": sha256(pdf) == source_hash,
    }


def find_copies(pdf: Path, root: Path) -> list[str]:
    """在 root 下找与 pdf 字节完全相同的副本（先比大小再比哈希）。"""
    size = pdf.stat().st_size
    digest: str | None = None
    found = []
    for candidate in root.rglob("*.pdf"):
        try:
            if candidate.resolve() == pdf.resolve() or candidate.stat().st_size != size:
                continue
        except OSError:
            continue
        digest = digest or sha256(pdf)
        if sha256(candidate) == digest:
            found.append(str(candidate))
    return found


def replace(target: Path, source: Path, backup_dir: Path | None, expect_sha256: str) -> dict[str, Any]:
    """先确认目标还是预期的那份，备份到 backup_dir（为 None 时不备份），再原子替换。

    不备份只用于自动同步：那时目标是插件上次自己写进去的文件，原件在第一次覆盖时已备份。
    """
    current = sha256(target)
    if current != expect_sha256:
        raise ValueError(f"target changed since the corrected PDF was built (sha256 {current[:12]} != {expect_sha256[:12]})")
    backup: Path | None = None
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{target.stem}.{datetime.now().strftime('%Y%m%dT%H%M%S')}{target.suffix}"
        shutil.copy2(target, backup)
        if sha256(backup) != current:
            raise RuntimeError("backup copy does not match the original; target left untouched")
    temporary = target.with_name(target.name + ".note1012-tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)
    return {"target": str(target), "backup": str(backup) if backup else None, "backupSha256": current, "newSha256": sha256(target)}


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build")
    b.add_argument("--pdf", type=Path, required=True)
    b.add_argument("--spec", type=Path, required=True)
    b.add_argument("--output", type=Path, required=True)
    b.add_argument("--report", type=Path)
    b.add_argument("--no-verify", action="store_true", help="skip the before/after appearance check when removing old text")
    b.add_argument("--writing-mode", choices=("horizontal-tb", "vertical-rl"),
                   help="default: the spec's writingMode, else horizontal-tb; vertical-rl writes one rotated line per column")
    f = sub.add_parser("find-copies")
    f.add_argument("--pdf", type=Path, required=True)
    f.add_argument("--search-root", type=Path, required=True)
    r = sub.add_parser("replace")
    r.add_argument("--target", type=Path, required=True)
    r.add_argument("--source", type=Path, required=True)
    r.add_argument("--backup-dir", type=Path)
    r.add_argument("--no-backup", action="store_true", help="automatic sync only: the target is a file this tool wrote earlier")
    r.add_argument("--expect-sha256", required=True)
    r.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.action == "build":
        report = build(args.pdf, load_spec(args.spec), args.output, verify=not args.no_verify,
                       writing_mode=args.writing_mode)
        target = args.report or args.output.with_suffix(".textlayer-report.json")
        write_report(target, report)
        print("TEXTLAYER_REPORT " + str(target).encode("ascii", "backslashreplace").decode("ascii"))
        if not report["sourceUnchanged"]:
            raise SystemExit(4)
    elif args.action == "find-copies":
        copies = find_copies(args.pdf, args.search_root.expanduser())
        print("COPIES " + json.dumps(copies, ensure_ascii=True))
    else:
        if not args.no_backup and args.backup_dir is None:
            parser.error("replace needs --backup-dir unless --no-backup is given")
        report = replace(args.target, args.source, None if args.no_backup else args.backup_dir, args.expect_sha256)
        if args.report:
            write_report(args.report, report)
        print("REPLACED " + json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"error={type(error).__name__}", file=sys.stderr)
        raise
