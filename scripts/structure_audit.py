"""OCR 底稿（尤其 VL 底稿）的结构核查：页下注是否收全、篇题是否读丢、正文里的残留，统一按页改。

VL 读古籍时有几类系统性毛病，逐字映射查不出来（它只比字，不管结构）：整条注漏收或混进正文、
两注并成一条、篇题整行读丢、书眉与注号残字夹进句子。本脚本出核对材料、出按页任务、执行改动：

  notes-sheet  每页按分隔线裁出注区，配上 Markdown 里记在这一页的注，4 页拼一张图，写 manifest.json。
               交子代理逐页对照，结果写 audit-*.json（格式见 references/structure-audit.md）。
  notes-count  用核对结果复查：每页注数 = 核对时 Markdown 的注数 + 漏收 + 并条，与现在的注数比对。
  toc-check    拿目录文件（「- 篇名　作者 …… 页码」一行一条）逐条对正文标题，列出正文里找不到的篇题。
  tasks        出按页任务：--kind notes（据 audit 的 missing）、titles（据 toc-check 的结果）、
               residue（扫注号残字、书眉等可疑字串）。每页一条：整页图、该页正文段、该页的注、要处理的项。
  apply        执行子代理按页写的改动 result-*.json：[{file, printed, edits:[{find, replace}], newNotes:[{label, text}]}]。
               find 必须在该页正文段里恰好出现一次，否则整页不动、列进报告。改前备份。
  renumber     按注号在正文里出现的先后把 [^n] 重编为 1..N，注文按新号排序；列出无注号的注文、无注文的注号。

用法（<书> 为书文件夹，配置默认 <书>/_数据/config.json）：
  python -X utf8 structure_audit.py notes-sheet --book <书> --out <书>/_数据/注区审核
  python -X utf8 structure_audit.py notes-count --book <书> --audit <书>/_数据/注区审核
  python -X utf8 structure_audit.py toc-check --book <书> --toc 01-目录.md --body 03-正文.md --from 自明試務疏 --out <书>/_数据/补篇题/missing.json
  python -X utf8 structure_audit.py tasks --book <书> --kind notes --audit <书>/_数据/注区审核 --out <书>/_数据/补注
  python -X utf8 structure_audit.py tasks --book <书> --kind titles --titles <书>/_数据/补篇题/missing.json --out <书>/_数据/补篇题
  python -X utf8 structure_audit.py tasks --book <书> --kind residue --running-head 黄道周的生平與思想 --out <书>/_数据/残留清理
  python -X utf8 structure_audit.py apply --book <书> --tasks <任务目录> [--book-forms]
  python -X utf8 structure_audit.py renumber --book <书>
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

NOTES_MARK = "\n---\n\n## 注释\n"
ANCHOR = re.compile(r"<!--p\.(\d+)-->")
DEF = re.compile(r"^\[\^(\w+)\]: <!--p\.(\d+)--> ?(.*)$", re.M)
REF = re.compile(r"\[\^(\w+)\](?!:)")
TOC_ENTRY = re.compile(r"^- (.+?) …… (\S+)\s*$")
# 注号残字：〔一〕（一）《一》「一」；正文里正常的「（一）小节」标题行另行排除
RESIDUE_PATTERNS = [r"[〔（《「][一二三四五六七八九十]{1,2}[〕）》」]", r"<!--\?fnref \d+-->"]
HEADING = re.compile(r"^#+ .*$", re.M)


# ---------- 公共 ----------

class Book:
    def __init__(self, book: Path, config: Path | None = None):
        self.root = book
        self.config_path = config or book / "_数据" / "config.json"
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.pdf = book / cfg["pdf"]
        self.files = cfg.get("markdownFiles", [])
        default = cfg.get("printedPageOffset", 0)
        self.offsets = {name: default for name in self.files}
        for item in cfg.get("fileOffsets", []):
            name, value = item.rsplit("|", 1)
            self.offsets[name] = int(value)
        self.vertical = cfg.get("writingMode") == "vertical-rl"
        # 本书字形表：每组第一个字是原书字形（variantEquivalents 按这个顺序写）
        self.variants = cfg.get("variantEquivalents", [])

    def text(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")

    def to_book_forms(self, text: str) -> str:
        for group in self.variants:
            for other in group[1:]:
                text = text.replace(other, group[0])
        return text


def split_notes(text: str) -> tuple[str, str]:
    """正文与注释节（「---」+「## 注释」之后）；没有注释节时注释为空串。"""
    if NOTES_MARK in text:
        body, notes = text.split(NOTES_MARK, 1)
        return body, notes
    return text, ""


def join_notes(body: str, notes: str) -> str:
    return body + NOTES_MARK + notes if notes.strip() else body


def page_segments(body: str) -> list[tuple[int, int, int]]:
    """[(印刷页, 起, 止)]：一页的正文从它的锚点之后到下一个锚点之前。"""
    anchors = list(ANCHOR.finditer(body))
    return [(int(m.group(1)), m.end(), anchors[i + 1].start() if i + 1 < len(anchors) else len(body))
            for i, m in enumerate(anchors)]


def segment_of(body: str, printed: int) -> tuple[int, int] | None:
    for page, start, end in page_segments(body):
        if page == printed:
            return start, end
    return None


def note_defs(notes: str) -> list[tuple[str, int, str]]:
    return [(m.group(1), int(m.group(2)), m.group(3)) for m in DEF.finditer(notes)]


def load_vl_build():
    spec = importlib.util.spec_from_file_location("vl_build", Path(__file__).with_name("vl_build.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["vl_build"] = module
    spec.loader.exec_module(module)
    return module


def render(doc, pdf_page: int, path: Path, dpi: int = 130, clip=None) -> None:
    doc[pdf_page - 1].get_pixmap(dpi=dpi, clip=clip).save(path)


# ---------- notes-sheet / notes-count ----------

def notes_sheet(book: Book, out: Path) -> None:
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont

    vl_build = load_vl_build()
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(book.pdf)
    by_page: dict[int, list[dict]] = collections.defaultdict(list)
    for name in book.files:
        _, notes = split_notes(book.text(name))
        for label, printed, text in note_defs(notes):
            by_page[printed + book.offsets[name]].append({"file": name, "label": label, "text": text[:60]})
    pages = []
    for number in range(1, doc.page_count + 1):
        page = doc[number - 1]
        rule = vl_build.note_separator(page, book.vertical)
        if rule is None and number not in by_page:
            continue
        w, h = page.rect.width, page.rect.height
        # 竖排注在分隔线左侧，横排在分隔线下方；没找到线（只有 Markdown 里有注）时裁常见位置
        if book.vertical:
            clip = pymupdf.Rect(0, 0.08 * h, ((rule if rule is not None else 0.3) * w) + 4, 0.95 * h)
        else:
            clip = pymupdf.Rect(0.05 * w, ((rule if rule is not None else 0.7) * h) - 4, 0.95 * w, h)
        name = f"p{number:04d}.png"
        page.get_pixmap(dpi=130, clip=clip).save(out / name)
        pages.append({"pdfPage": number, "separator": rule is not None, "image": name, "notes": by_page.get(number, [])})
    font = ImageFont.load_default(size=30)
    for start in range(0, len(pages), 4):
        group = pages[start:start + 4]
        images = [Image.open(out / g["image"]).convert("RGB") for g in group]
        if book.vertical:
            images = [im.resize((max(1, int(im.width * 900 / im.height)), 900)) for im in images]
            sheet = Image.new("RGB", (sum(max(im.width, 160) + 20 for im in images), 950), "white")
            x = 0
            for g, im in zip(group, images):
                ImageDraw.Draw(sheet).text((x + 5, 5), f"PDF {g['pdfPage']}", fill="red", font=font)
                sheet.paste(im, (x, 45))
                x += max(im.width, 160) + 20
        else:
            images = [im.resize((1200, max(1, int(im.height * 1200 / im.width)))) for im in images]
            sheet = Image.new("RGB", (1200, sum(im.height + 50 for im in images)), "white")
            y = 0
            for g, im in zip(group, images):
                ImageDraw.Draw(sheet).text((5, y + 5), f"PDF {g['pdfPage']}", fill="red", font=font)
                sheet.paste(im, (0, y + 45))
                y += im.height + 50
        sheet_name = f"sheet-{start // 4 + 1}.png"
        sheet.save(out / sheet_name)
        for g in group:
            g["sheet"] = sheet_name
    (out / "manifest.json").write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"NOTES_SHEET pages={len(pages)} sheets={(len(pages) + 3) // 4}")


def expected_counts(manifest: list[dict], audits: list[dict]) -> collections.Counter:
    expected = collections.Counter({m["pdfPage"]: len(m["notes"]) for m in manifest})
    for item in audits:
        if item.get("type") in ("missing", "merged"):
            expected[item["pdfPage"]] += 1
        elif item.get("type") == "extra":
            expected[item["pdfPage"]] -= 1
    return expected


def notes_count(book: Book, audit_dir: Path) -> None:
    manifest = json.loads((audit_dir / "manifest.json").read_text(encoding="utf-8"))
    audits = [x for f in sorted(audit_dir.glob("audit-*.json")) for x in json.loads(f.read_text(encoding="utf-8"))]
    expected = expected_counts(manifest, audits)
    now = collections.Counter()
    for name in book.files:
        _, notes = split_notes(book.text(name))
        for _, printed, _ in note_defs(notes):
            now[printed + book.offsets[name]] += 1
    diff = {p: {"expected": expected[p], "now": now[p]} for p in sorted(set(expected) | set(now)) if expected[p] != now[p]}
    (audit_dir / "count-report.json").write_text(json.dumps(diff, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"NOTES_COUNT pages={len(set(expected) | set(now))} mismatched={len(diff)}")


# ---------- toc-check ----------

def toc_entries(toc_text: str) -> list[tuple[str, str]]:
    """目录条目 [(篇名, 页码)]；篇名与作者之间是全角空格，只取篇名。"""
    entries = []
    for line in toc_text.splitlines():
        m = TOC_ENTRY.match(line.strip())
        if m:
            entries.append((m.group(1).split("　")[0].strip(), m.group(2)))
    return entries


def chinese_page(value: str) -> int | None:
    digits = dict(zip("〇○零一二三四五六七八九", [0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]))
    if value.isdigit():
        return int(value)
    if all(c in digits for c in value):
        return int("".join(str(digits[c]) for c in value))
    return None


def heading_titles(body: str) -> set[str]:
    return {re.sub(r"\s*<small>.*?</small>|\[\^\w+\]", "", m.group(1)).strip()
            for m in re.finditer(r"^#+ (.+)$", body, re.M)}


def missing_titles(entries: list[tuple[str, str]], titles: set[str], start: str | None = None) -> list[tuple[str, str]]:
    begun = start is None
    missing = []
    for title, page in entries:
        begun = begun or title == start
        if begun and title not in titles:
            missing.append((title, page))
    return missing


def toc_check(book: Book, toc: str, body_file: str, start: str | None, out: Path) -> None:
    entries = toc_entries(book.text(toc))
    body, _ = split_notes(book.text(body_file))
    result = []
    for title, page in missing_titles(entries, heading_titles(body), start):
        printed = chinese_page(page)
        result.append({"file": body_file, "title": title, "printed": printed,
                       "pdfPage": printed + book.offsets[body_file] if printed is not None else None})
    # 目录里卷名、文体标签写成粗体行（**卷一**、**疏**），正文里是标题，也算目录里有
    labels = set(re.findall(r"^\*\*(.+?)\*\*\s*$", book.text(toc), re.M))
    extra = sorted(heading_titles(body) - {t for t, _ in entries} - labels)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"missing": result, "headingsNotInToc": extra}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"TOC_CHECK entries={len(entries)} missing={len(result)} headingsNotInToc={len(extra)}")


# ---------- tasks ----------

def residue_hits(segment: str, running_heads: list[str]) -> list[str]:
    plain = HEADING.sub("", segment)
    hits = [m.group(0) for p in RESIDUE_PATTERNS for m in re.finditer(p, plain)]
    hits += [h for h in running_heads if h in plain]
    return hits


def make_tasks(book: Book, kind: str, out: Path, audit_dir: Path | None, titles: Path | None,
               running_heads: list[str]) -> None:
    import pymupdf

    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(book.pdf)
    bodies = {name: split_notes(book.text(name)) for name in book.files}
    file_of_page = {}
    for name in book.files:
        for printed, _, _ in page_segments(bodies[name][0]):
            file_of_page.setdefault(printed + book.offsets[name], (name, printed))
    items: dict[tuple[str, int], list[dict]] = collections.defaultdict(list)
    temp = 9001
    if kind == "notes":
        audits = [x for f in sorted(audit_dir.glob("audit-*.json")) for x in json.loads(f.read_text(encoding="utf-8"))]
        for x in audits:
            if x.get("type") != "missing" or x["pdfPage"] not in file_of_page:
                continue
            items[file_of_page[x["pdfPage"]]].append({"label": str(temp), "local": x.get("local"),
                                                      "text": book.to_book_forms(x["text"])})
            temp += 1
    elif kind == "titles":
        for x in json.loads(titles.read_text(encoding="utf-8"))["missing"]:
            if x.get("printed") is not None:
                items[(x["file"], x["printed"])].append({"title": x["title"]})
    else:
        for name in book.files:
            body = bodies[name][0]
            for printed, start, end in page_segments(body):
                hits = residue_hits(body[start:end], running_heads)
                if hits:
                    items[(name, printed)] = [{"hits": hits}]
    tasks = []
    for (name, printed), entries in sorted(items.items(), key=lambda kv: (book.files.index(kv[0][0]), kv[0][1])):
        body, notes = bodies[name]
        span = segment_of(body, printed)
        if span is None:
            continue
        pdf_page = printed + book.offsets[name]
        image = f"page{pdf_page:04d}.png"
        render(doc, pdf_page, out / image)
        page_notes = [{"label": l, "text": t} for l, p, t in note_defs(notes) if p == printed]
        tasks.append({"file": name, "printed": printed, "pdfPage": pdf_page, "image": image,
                      "segment": body[span[0]:span[1]], "pageNotes": page_notes, "items": entries})
    (out / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"TASKS kind={kind} pages={len(tasks)} items={sum(len(t['items']) for t in tasks)}")


# ---------- apply ----------

def apply_page(segment: str, edits: list[dict]) -> tuple[str, list[str]]:
    """在一页正文段里依次做 find → replace；任何一条 find 不是恰好一次就整页不动。"""
    problems = [f"「{e['find'][:30]}」出现 {segment.count(e['find'])} 次" for e in edits
                if not e.get("find") or segment.count(e["find"]) != 1]
    if problems:
        return segment, problems
    for e in edits:
        if segment.count(e["find"]) != 1:  # 前一条改动吃掉了后一条的定位串
            return segment, [f"「{e['find'][:30]}」被本页前面的改动影响，找不到唯一位置"]
        segment = segment.replace(e["find"], e["replace"], 1)
    return re.sub(r"\n{3,}", "\n\n", segment), []


def apply_results(book: Book, tasks_dir: Path, book_forms: bool) -> None:
    results = [r for f in sorted(tasks_dir.glob("result*.json")) for r in json.loads(f.read_text(encoding="utf-8"))]
    texts = {name: book.text(name) for name in {r["file"] for r in results}}
    backup = tasks_dir / "backup"
    backup.mkdir(exist_ok=True)
    for name in texts:
        if not (backup / name).exists():
            shutil.copy2(book.root / name, backup / name)
    report = {"applied": [], "failed": []}
    for r in results:
        body, notes = split_notes(texts[r["file"]])
        span = segment_of(body, r["printed"])
        if span is None:
            report["failed"].append({"file": r["file"], "printed": r["printed"], "problems": ["找不到这一页的锚点"]})
            continue
        segment, problems = apply_page(body[span[0]:span[1]], r.get("edits", []))
        if problems:
            report["failed"].append({"file": r["file"], "printed": r["printed"], "problems": problems})
            continue
        body = body[:span[0]] + segment + body[span[1]:]
        for n in r.get("newNotes", []):
            text = book.to_book_forms(n["text"]) if book_forms else n["text"]
            notes = notes.rstrip("\n") + f"\n\n[^{n['label']}]: <!--p.{r['printed']}--> {text}\n"
        texts[r["file"]] = join_notes(body, notes)
        report["applied"].append({"file": r["file"], "printed": r["printed"], "edits": len(r.get("edits", [])),
                                  "newNotes": len(r.get("newNotes", []))})
    for name, text in texts.items():
        (book.root / name).write_text(text, encoding="utf-8")
    (tasks_dir / "apply-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"APPLY pages={len(report['applied'])} failed={len(report['failed'])}")


# ---------- renumber ----------

def renumber_text(text: str) -> tuple[str, list[str], list[str]]:
    body, notes = split_notes(text)
    if not notes:
        return text, [], []
    order: list[str] = []
    for m in REF.finditer(body):
        if m.group(1) not in order:
            order.append(m.group(1))
    defs = {m.group(1): m.group(0).split(": ", 1)[1] for m in DEF.finditer(notes)}
    unused = [k for k in defs if k not in order]
    undefined = [k for k in order if k not in defs]
    new = {old: str(i + 1) for i, old in enumerate(k for k in order if k in defs)}
    body = REF.sub(lambda m: f"[^{new[m.group(1)]}]" if m.group(1) in new else m.group(0), body)
    lines = [f"[^{new[k]}]: {defs[k]}" for k in order if k in defs]
    # 没被引用的注文不丢：编在最后，报告里列出来等人处理
    lines += [f"[^{len(lines) + i + 1}]: {defs[k]}" for i, k in enumerate(unused)]
    return body + NOTES_MARK + "\n" + "\n\n".join(lines) + "\n", unused, undefined


def renumber(book: Book) -> None:
    for name in book.files:
        text = book.text(name)
        if NOTES_MARK not in text:
            continue
        new, unused, undefined = renumber_text(text)
        (book.root / name).write_text(new, encoding="utf-8")
        print(f"RENUMBER file={name.encode('ascii', 'replace').decode()} notes={new.count(chr(10) + '[^')} unused={len(unused)} undefined={len(undefined)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("notes-sheet", "notes-count", "toc-check", "tasks", "apply", "renumber"):
        p = sub.add_parser(action)
        p.add_argument("--book", type=Path, required=True)
        p.add_argument("--config", type=Path)
        if action in ("notes-sheet", "tasks", "toc-check"):
            p.add_argument("--out", type=Path, required=True)
        if action in ("notes-count",):
            p.add_argument("--audit", type=Path, required=True)
        if action == "toc-check":
            p.add_argument("--toc", required=True)
            p.add_argument("--body", required=True)
            p.add_argument("--from", dest="start")
        if action == "tasks":
            p.add_argument("--kind", choices=["notes", "titles", "residue"], required=True)
            p.add_argument("--audit", type=Path)
            p.add_argument("--titles", type=Path)
            p.add_argument("--running-head", action="append", default=[])
        if action == "apply":
            p.add_argument("--tasks", type=Path, required=True)
            p.add_argument("--book-forms", action="store_true", help="新注文按本书字形表改成原书字形（异体字方针为保留原书字形时）")
    args = parser.parse_args()
    book = Book(args.book.resolve(), args.config)
    if args.action == "notes-sheet":
        notes_sheet(book, args.out)
    elif args.action == "notes-count":
        notes_count(book, args.audit)
    elif args.action == "toc-check":
        toc_check(book, args.toc, args.body, args.start, args.out)
    elif args.action == "tasks":
        if args.kind == "notes" and not args.audit:
            parser.error("--kind notes 需要 --audit")
        if args.kind == "titles" and not args.titles:
            parser.error("--kind titles 需要 --titles")
        make_tasks(book, args.kind, args.out, args.audit, args.titles, args.running_head)
    elif args.action == "apply":
        apply_results(book, args.tasks, args.book_forms)
    else:
        renumber(book)


if __name__ == "__main__":
    main()
