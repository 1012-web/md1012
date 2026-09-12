from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from collections import defaultdict
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pdf_preflight as preflight
import pdf_sync as sync
import sync_mapper as mapper


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="strict", newline="") as handle:
        handle.write(value)


def test_text_evidence_and_decisions():
    normal = preflight.text_evidence("正常文字。Another sentence.")
    assert preflight.summarize([normal], "auto")["recommendation"] == "reuse_text_after_visual_check"
    broken = preflight.text_evidence("甲\ufffd\ue001")
    assert broken["suspiciousCount"] == 2
    assert preflight.summarize([broken], "auto")["awaitUserChoice"]
    assert not preflight.summarize([broken], "on")["awaitUserChoice"]
    assert preflight.summarize([broken], "on")["decision"] == "user_requested_ocr"
    blank = preflight.text_evidence("")
    assert preflight.summarize([blank], "auto")["recommendation"] == "ocr"
    assert preflight.summarize([blank, normal], "auto")["sampleStatus"] == "mixed_sample"
    assert preflight.summarize([{"error": "DecodeError"}], "auto")["awaitUserChoice"]
    ancient = preflight.text_evidence("古文" * 120)
    assert preflight.summarize([ancient], "auto")["recommendation"] == "review_original"


def test_sample_distribution():
    assert preflight.sample_indexes(1) == [0]
    samples = preflight.sample_indexes(340)
    assert len(samples) == 9 and samples[0] == 0 and samples[-1] == 339


def test_preflight_real_pdf_no_input_changes(tmp_path):
    pytest.importorskip("pypdf")
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "scan.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.new_page().insert_text((40, 40), "Readable sentence.")
        doc.save(pdf)
    original = pdf.read_bytes()
    report = preflight.inspect_pdf(pdf)
    assert report["sampleStatus"] == "mixed_sample"
    assert report["networkRequests"] == report["ocrRuns"] == 0
    assert pdf.read_bytes() == original


@pytest.mark.parametrize("raw,visible", [
    ('<mark class="hl-yellow" data-id="abc123">甲**乙**</mark>%%@abc123 备注[[不算]]%%[[丙]]', "甲乙丙"),
    ('<u data-id="abc123">𠀀</u><s data-id="def456">丁</s>[[目标|戊]]', "𠀀丁戊"),
    ("[[甲]] [[目标|甲]] [乙](https://example.com)", "甲甲乙"),
])
def test_annotation_mask_preserves_body_offsets(raw, visible):
    mask = mapper.markdown_mask(raw)
    result = "".join(c for c, keep in zip(raw, mask) if keep)
    assert result == visible
    offsets = mapper.utf16_offsets(raw)
    for i, (c, keep) in enumerate(zip(raw, mask)):
        if keep:
            assert raw.encode("utf-16-le")[offsets[i]*2:offsets[i+1]*2].decode("utf-16-le") == c


@pytest.fixture
def book(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    vault = tmp_path / "另一个仓库"
    vault.mkdir()
    with fitz.open() as doc:
        doc.new_page(width=100, height=100)
        doc.new_page(width=100, height=100)
        doc.save(vault / "source.pdf")
    write(vault / "notes" / "自定义章名.md", '<!--p.1-->\r\n\r\n<mark data-id="abc123">甲</mark>%%@abc123 备注%%[[乙]]<!--p.2-->丙丁\r\n')
    # The existing loader accepts model-style pages as layout fallback too.
    model = [[{"type": "text", "bbox": [0.1, 0.1, 0.9, 0.3], "content": text},
              {"type": "ocr_text", "bbox": [0.1, 0.1, 0.9, 0.3], "text": "", "score": 1}]
             for text in ("甲乙", "丙丁")]
    write(vault / "model.json", json.dumps(model, ensure_ascii=False))
    layout = {"pdf_info": [{"page_idx": i, "page_size": [100, 100], "para_blocks": [
        {"type": "text", "bbox": [10, 10, 90, 30], "lines": [{"spans": [{"content": text}]}]}]}
        for i, text in enumerate(("甲乙", "丙丁"))]}
    write(vault / "layout.json", json.dumps(layout, ensure_ascii=False))
    raw = {"mode": "character", "vault": str(vault), "bookId": "test-book", "title": "测试", "pdf": "source.pdf",
           "markdownRoot": "notes", "markdownFiles": ["自定义章名.md"],
           "layoutParts": [{"path": str(vault / "layout.json"), "physicalStart": 1}],
           "modelParts": [{"path": str(vault / "model.json"), "physicalStart": 1}],
           "printedPageOffset": 0, "output": "sync/test-book", "cache": str(tmp_path / "cache")}
    path = tmp_path / "config.json"
    write(path, json.dumps(raw, ensure_ascii=False))
    return path, raw


def test_explicit_opt_in_and_alternate_backend_rejected(book):
    path, raw = book
    raw.pop("mode")
    write(path, json.dumps(raw))
    with pytest.raises(ValueError, match="mode=character"):
        sync.load_config(path)
    raw.update(mode="character", ocrBackend="tesseract")
    write(path, json.dumps(raw))
    with pytest.raises(ValueError, match="coordinate-verified engines only"):
        sync.load_config(path)
    # 坐标口径验证过的两个后端都要放行。
    for backend in ("paddle", "onnx"):
        raw.update(ocrBackend=backend)
        write(path, json.dumps(raw))
        assert sync.load_config(path)["ocrBackend"] == backend


def test_check_has_no_output_or_ocr_import(book):
    path, _ = book
    result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / "pdf_sync.py"), "check", "--config", str(path)], capture_output=True, text=True, encoding="utf-8", errors="strict")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["inputs"]["chapters"] == 1
    config = sync.load_config(path)
    assert not config["output"].exists() and not config["cache"].exists()


def test_generate_and_rerun_preserve_all_pages_and_inputs(book, monkeypatch):
    path, _ = book
    config = sync.load_config(path)
    calls = []
    class FakeOCR:
        backend = "paddle"
        def __init__(self, *args):
            pass
        def recognize(self, image, lines):
            raise AssertionError("render hook should provide page")
    def fake_load(engine, document, page, lines, cache, scale, writing_mode, force, base=None, loader=None):
        calls.append((page, force))
        return [mapper.OCRChar(c, (0.1 + i * 0.4, 0.1, 0.5 + i * 0.4, 0.3))
                for i, c in enumerate("甲乙" if page == 1 else "丙丁")]
    def run(args, check):
        monkeypatch.setattr(sys, "argv", [str(SCRIPTS / "sync_mapper.py"), *args[4:]])
        mapper.main()
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(mapper, "PaddleCharacterOCR", FakeOCR)
    monkeypatch.setattr(mapper, "load_or_run_ocr", fake_load)
    monkeypatch.setattr(sync.subprocess, "run", run)
    monkeypatch.setattr(sync, "dependency_report", lambda backend="paddle": {"python311": True, "packages": {"stub": {"available": True}}})
    first = sync.generate(config)
    assert first["blockAcceptance"] == "passed"
    assert first["mappedCharacters"] == 4
    assert first["inputBytesUnchanged"]
    first_bytes = (Path(first["output"]) / "index.json").read_bytes()
    calls.clear()
    second = sync.generate(config, "2-2")
    assert calls == [(1, False), (2, True)]
    assert second["mappedCharacters"] == 4
    assert set(sync.read_json(Path(second["output"]) / "index.json")["pages"]) == {"1", "2"}
    assert first["output"] != second["output"]
    assert (Path(first["output"]) / "index.json").read_bytes() == first_bytes
    assert second["inputHashes"] == first["inputHashes"]


def test_cache_isolation(book):
    path, _ = book
    config = sync.load_config(path)
    original = sync.cache_directory(config)
    config["renderScale"] = 3
    assert sync.cache_directory(config) != original
    config["renderScale"] = 2
    config["recognitionModel"] = "another-model"
    assert sync.cache_directory(config) != original


def test_snapshot_provenance():
    metadata = sync.read_json(SCRIPTS / "sync-source.json")
    assert metadata["sha256"] == sync.sha256(SCRIPTS / "sync_mapper.py")


def test_annotation_note_newlines_and_page_quotes_do_not_change_mapping(tmp_path):
    source = tmp_path / "chapter.md"
    write(source, '<!--p.1-->\n\n甲%%@abc123 备注\n\n<!--p.99-->不要映射%%乙\n\n丙')
    _, blocks = mapper.parse_markdown_file(source, tmp_path, lambda p: p + 40)
    assert ["".join(c.value for c in b.chars) for b in blocks] == ["甲乙", "丙"]
    assert {c.physical_page for b in blocks for c in b.chars} == {41}


def test_cached_page_is_reused_unless_explicitly_rerun(tmp_path, monkeypatch):
    class Engine:
        backend = "paddle"
        calls = 0
        def recognize(self, image, lines):
            self.calls += 1
            return [mapper.OCRChar("甲", (0.1, 0.1, 0.2, 0.2))]
    engine = Engine()
    monkeypatch.setattr(mapper, "render_page", lambda *args: object())
    for force in (False, False, True):
        chars = mapper.load_or_run_ocr(engine, object(), 1, [], tmp_path, 2, force=force)
        assert chars[0].value == "甲"
    assert engine.calls == 2


def test_cross_page_block_gap_fails_acceptance(tmp_path):
    write(tmp_path / "chapter.md", "甲乙")
    chars = [mapper.VisibleChar("甲", 0, 1, 1, 1, rect=(0.1, 0.1, 0.2, 0.2)),
             mapper.VisibleChar("乙", 1, 2, 2, 2)]
    block = mapper.MarkdownBlock("chapter.md", 0, 2, "paragraph", chars=chars,
                                 regions=[(1, (0.1, 0.1, 0.2, 0.2))], block_id="block")
    out = tmp_path / "sync" / "book"
    mapper.write_outputs(tmp_path, out, "book", "书", "book.pdf", ".", {"chapter.md": "甲乙"}, [block], 2, 0)
    report = sync.audit_output(tmp_path, out / "index.json")
    assert report["blockAcceptance"] == "failed"
    assert report["unmappedBlocks"][0]["physicalPages"] == [2]
    assert report["missingCharacterRanges"][0]["ranges"] == [[1, 2]]
    assert report["additionalRepair"]["localOCRMinerUQuota"] == 0


@pytest.mark.skipif(not os.environ.get("MINERU_SYNC_TEST_VAULT"), reason="Optional read-only prototype replay")
def test_prototype_cache_replay_without_mutation():
    vault = Path(os.environ["MINERU_SYNC_TEST_VAULT"])
    book = sync.read_json(vault / "90-工作流/MinerU联动/catalog.json")["books"][0]
    rebuild = book["rebuild"]
    index = sync.read_json(vault / book["index"])
    paths = [vault / book["pdf"], *[vault / name for name in index["chapters"]]]
    before = {str(path): sync.sha256(path) for path in paths}
    offsets = {value.rsplit("|", 1)[0]: int(value.rsplit("|", 1)[1]) for value in rebuild["fileOffsets"]}
    overrides = {(name, int(printed)): int(physical) for name, printed, physical in
                 (value.rsplit("|", 2) for value in rebuild["pageOverrides"])}
    grouped = defaultdict(list)
    for filename in index["chapters"]:
        source = vault / filename
        _, blocks = mapper.parse_markdown_file(source, vault, mapper.page_mapper_for_file(
            source.name, book["printedPageOffset"], offsets, overrides))
        for block in blocks:
            for char in block.chars:
                grouped[char.physical_page].append(char)
    mapped = 0
    for page, chars in sorted(grouped.items()):
        cached = sync.read_json(Path(rebuild["cache"]) / f"p{page:04d}.json")
        mapped += mapper.align_page(chars, [mapper.OCRChar(value, tuple(rect)) for value, rect in cached])
    total = sum(map(len, grouped.values()))
    assert mapped / total > 0.89
    phrase = "哈里发运动"
    page_chars = grouped[201]
    text = "".join(c.value for c in page_chars)
    at = text.index(phrase)
    assert all(c.rect is not None for c in page_chars[at:at + len(phrase)])
    assert before == {str(path): sync.sha256(path) for path in paths}
    print(f"PROTOTYPE_REPLAY pages={len(grouped)} mapped={mapped} total={total} coverage={mapped/total:.4%} hashes_unchanged=true")


@pytest.mark.skipif(not os.environ.get("MINERU_SYNC_REAL_OCR"), reason="Explicit local single-page PaddleOCR smoke test")
def test_real_ocr_cli_on_isolated_prototype_page(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    vault = Path(os.environ["MINERU_SYNC_TEST_VAULT"])
    book = sync.read_json(vault / "90-工作流/MinerU联动/catalog.json")["books"][0]
    original = vault / book["pdf"]
    before = sync.sha256(original)
    sandbox = tmp_path / "另一个独立仓库"
    sandbox.mkdir()
    with fitz.open(original) as source, fitz.open() as target:
        target.insert_pdf(source, from_page=200, to_page=200)
        target.save(sandbox / "source.pdf")
    rebuild = book["rebuild"]
    layout_path = next(Path(value.rsplit("|", 1)[0]) for value in rebuild["layoutParts"] if value.endswith("|201"))
    model_path = next(Path(value.rsplit("|", 1)[0]) for value in rebuild["modelParts"] if value.endswith("|201"))
    page = sync.read_json(layout_path)["pdf_info"][0]
    page["page_idx"] = 0
    write(sandbox / "layout.json", json.dumps({"pdf_info": [page]}, ensure_ascii=False))
    write(sandbox / "model.json", json.dumps([sync.read_json(model_path)[0]], ensure_ascii=False))
    chapter = next((vault / book["markdownRoot"]).glob("10-*.md"))
    with chapter.open(encoding="utf-8", errors="strict") as handle:
        paragraphs = handle.read().split("\n\n")
    paragraph = next(p for p in paragraphs if "开展了哈里发运动" in p)
    write(sandbox / "notes" / "自定义章名.md", "<!--p.161-->\n\n" + paragraph)
    config = {"mode": "character", "bookId": "single-page-smoke", "title": "测试副本", "pdf": "source.pdf",
              "markdownRoot": "notes", "markdownFiles": ["自定义章名.md"], "printedPageOffset": -160,
              "layoutParts": [{"path": "layout.json", "physicalStart": 1}],
              "modelParts": [{"path": "model.json", "physicalStart": 1}], "output": "sync/smoke"}
    write(sandbox / "config.json", json.dumps(config, ensure_ascii=False))
    result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / "pdf_sync.py"), "generate", "--config", str(sandbox / "config.json")],
                            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    report = sync.read_json(sandbox / "sync/smoke/delivery-report.json")
    assert report["blockAcceptance"] == "passed" and report["inputBytesUnchanged"]
    assert report["characterCoveragePercent"] > 85
    assert report["apiRequests"] == report["obsidianActions"] == 0
    assert sync.sha256(original) == before
    print(f"REAL_OCR_SMOKE coverage={report['characterCoveragePercent']} elapsed={report['elapsedSeconds']} report={sandbox / 'sync/smoke/delivery-report.json'}")


def test_cache_defaults_to_machine_local_directory_outside_vault(book):
    path, raw = book
    raw.pop("cache")
    write(path, json.dumps(raw, ensure_ascii=False))
    config = sync.load_config(path)
    assert config["cache"] == sync.default_cache_root()
    assert not config["cache"].is_relative_to(config["vault"])
    assert config["configPath"] == path.resolve()


def test_page_range_offsets_are_validated_and_forwarded(book):
    path, raw = book
    raw["pageRangeOffsets"] = ["自定义章名.md|2-2|5"]
    write(path, json.dumps(raw, ensure_ascii=False))
    config = sync.load_config(path)
    inputs = sync.inspect_inputs(config)
    assert inputs["physicalPages"] == [1, 7]
    args = sync.command_args(config, config["output"], config["cache"], None)
    assert args[args.index("--page-range-offset") + 1] == "自定义章名.md|2-2|5"
    assert "--rebuild-config" not in args  # config lives outside the vault
    raw["pageRangeOffsets"] = ["bad"]
    write(path, json.dumps(raw, ensure_ascii=False))
    with pytest.raises(ValueError, match="page range offset must look like"):
        sync.load_config(path)


def test_offset_entries_must_name_a_real_chapter_and_not_overlap(book):
    path, raw = book
    raw["pageRangeOffsets"] = ["打错的章名.md|2-2|5"]
    write(path, json.dumps(raw, ensure_ascii=False))
    with pytest.raises(ValueError, match="pageRangeOffsets names files that are not"):
        sync.load_config(path)

    raw["pageRangeOffsets"] = ["自定义章名.md|1-5|2", "自定义章名.md|4-9|3"]
    write(path, json.dumps(raw, ensure_ascii=False))
    with pytest.raises(ValueError, match="pageRangeOffsets overlap"):
        sync.load_config(path)

    raw["pageRangeOffsets"] = ["自定义章名.md|1-3|2", "自定义章名.md|4-9|3"]
    raw["fileOffsets"] = ["不存在.md|1"]
    write(path, json.dumps(raw, ensure_ascii=False))
    with pytest.raises(ValueError, match="fileOffsets names files that are not"):
        sync.load_config(path)

    raw["fileOffsets"] = []
    raw["pageOverrides"] = ["不存在.md|1|2"]
    write(path, json.dumps(raw, ensure_ascii=False))
    with pytest.raises(ValueError, match="pageOverrides names files that are not"):
        sync.load_config(path)


def test_config_outside_the_vault_warns_that_rebuild_is_not_recorded(book, capsys):
    path, _ = book
    config = sync.load_config(path)
    args = sync.command_args(config, config["output"], config["cache"], None)

    assert "--rebuild-config" not in args
    warning = capsys.readouterr().err
    assert "WARNING" in warning and "rebuild.config" in warning
    assert warning.isascii()  # Windows consoles are GBK.


def test_failed_acceptance_exits_3_so_usage_errors_keep_2(book, monkeypatch, capsys):
    path, _ = book
    report = {"output": str(path.parent / "sync" / "test-book"), "blockAcceptance": "failed",
              "hashMismatches": []}
    monkeypatch.setattr(sync, "generate", lambda config, pages=None: report)
    monkeypatch.setattr(sys, "argv", ["pdf_sync.py", "generate", "--config", str(path)])

    with pytest.raises(SystemExit) as failure:
        sync.main()
    assert failure.value.code == 3
    assert capsys.readouterr().out.startswith("SYNC_REPORT ")

    # argparse keeps 2 for a wrong command line, which is exactly what 3 has to stay clear of.
    monkeypatch.setattr(sys, "argv", ["pdf_sync.py", "generate", "--config", str(path), "--pages", "1-1"])
    with pytest.raises(SystemExit) as usage:
        sync.main()
    assert usage.value.code == 2


def test_mapper_cli_offers_only_rerun_pages(book, monkeypatch):
    path, _ = book
    config = sync.load_config(path)
    args = sync.command_args(config, config["output"], config["cache"], "2-2")

    assert "--pages" not in args
    assert args[args.index("--rerun-pages") + 1] == "2-2"
    monkeypatch.setattr(sys, "argv", [str(SCRIPTS / "sync_mapper.py"), *args[4:], "--pages", "2-2"])
    with pytest.raises(SystemExit) as usage:
        mapper.main()
    assert usage.value.code == 2


def test_rebuild_config_is_recorded_vault_relative_when_config_inside_vault(book, monkeypatch):
    path, raw = book
    vault = Path(raw["vault"])
    inside = vault / "sync" / "_config" / "test-book.json"
    raw["vault"] = "../.."
    raw["layoutParts"] = [{"path": "../../layout.json", "physicalStart": 1}]
    raw["modelParts"] = [{"path": "../../model.json", "physicalStart": 1}]
    write(inside, json.dumps(raw, ensure_ascii=False))
    config = sync.load_config(inside)
    args = sync.command_args(config, config["output"], config["cache"], None)
    assert args[args.index("--rebuild-config") + 1] == "sync/_config/test-book.json"

    def fake_load(engine, document, page, lines, cache, scale, writing_mode, force, base=None, loader=None):
        return [mapper.OCRChar(c, (0.1 + i * 0.4, 0.1, 0.5 + i * 0.4, 0.3))
                for i, c in enumerate("甲乙" if page == 1 else "丙丁")]

    def run(args, check):
        monkeypatch.setattr(sys, "argv", [str(SCRIPTS / "sync_mapper.py"), *args[4:]])
        mapper.main()
        return subprocess.CompletedProcess(args, 0)

    class FakeOCR:
        backend = "paddle"
        def __init__(self, *args):
            pass

    monkeypatch.setattr(mapper, "PaddleCharacterOCR", FakeOCR)
    monkeypatch.setattr(mapper, "load_or_run_ocr", fake_load)
    monkeypatch.setattr(sync.subprocess, "run", run)
    monkeypatch.setattr(sync, "dependency_report", lambda backend="paddle": {"python311": True, "packages": {"stub": {"available": True}}})
    report = sync.generate(config)
    catalog = sync.read_json(vault / "sync" / "catalog.json")
    entry = next(b for b in catalog["books"] if b["id"] == "test-book")
    assert entry["rebuild"] == {"config": "sync/_config/test-book.json"}
    assert report["blockAcceptance"] == "passed"


def test_footnote_block_without_line_boxes_is_detected_and_read_last(book, monkeypatch):
    """End-to-end wiring: a block MinerU left without line boxes reaches OCR, and always after the body."""
    path, raw = book
    vault = Path(raw["vault"])
    model = [[{"type": "text", "bbox": [0.1, 0.1, 0.9, 0.3], "content": text},
              {"type": "ocr_text", "bbox": [0.1, 0.1, 0.9, 0.3], "text": "", "score": 1},
              {"type": "page_footnote", "bbox": [0.1, 0.8, 0.9, 0.9], "content": "① 脚注"}]
             for text in ("甲乙", "丙丁")]
    write(vault / "model.json", json.dumps(model, ensure_ascii=False))
    config = sync.load_config(path)
    built = []
    seen = []

    class StubDetector:
        def __init__(self, model_name, cpu_threads):
            built.append(model_name)

        def detect(self, crop):
            return [[(0, 11), (168, 11), (168, 18), (0, 18)],
                    [(0, 21), (168, 21), (168, 28), (0, 28)]]

    class FakeOCR:
        backend = "paddle"
        def __init__(self, *args):
            pass
        def recognize(self, image, lines):
            raise AssertionError("recognition is stubbed at load_or_run_ocr")

    def fake_load(engine, document, page, lines, cache, scale, writing_mode, force, base=None, loader=None):
        seen.append((page, list(lines), list(base or [])))
        return [mapper.OCRChar(c, (0.1 + i * 0.4, 0.1, 0.5 + i * 0.4, 0.3))
                for i, c in enumerate("甲乙" if page == 1 else "丙丁")]

    def run(args, check):
        monkeypatch.setattr(sys, "argv", [str(SCRIPTS / "sync_mapper.py"), *args[4:]])
        mapper.main()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(mapper, "PaddleCharacterOCR", FakeOCR)
    monkeypatch.setattr(mapper, "PaddleLineDetector", StubDetector)
    monkeypatch.setattr(mapper, "load_or_run_ocr", fake_load)
    monkeypatch.setattr(sync.subprocess, "run", run)
    monkeypatch.setattr(sync, "dependency_report", lambda backend="paddle": {"python311": True, "packages": {"stub": {"available": True}}})
    sync.generate(config)

    # 检测器懒加载一次，两页复用
    assert built == [mapper.DEFAULT_DETECTION_MODEL]
    assert [page for page, _, _ in seen] == [1, 2]
    for _, lines, base in seen:
        assert base == [(0.1, 0.1, 0.9, 0.3)]
        assert len(lines) == 3
        extra = [rect for rect in lines if rect not in base]
        assert extra == [(0.1, 0.805, 0.89, 0.84), (0.1, 0.855, 0.89, 0.89)]
        assert mapper.lines_follow(base, extra, "horizontal-tb")


# --- ONNX 后端的配置透传与缓存隔离 ---------------------------------------------


def test_onnx_settings_reach_the_mapper_command_line(book, tmp_path):
    path, raw = book
    raw.update(ocrBackend="onnx", onnxModel="PP-OCRv5_server_rec", onnxDevice="dml",
               onnxDeviceId=1, recBatchSize=8)
    write(path, json.dumps(raw, ensure_ascii=False))
    config = sync.load_config(path)

    args = sync.command_args(config, config["output"], tmp_path / "cache", None)

    assert args[args.index("--ocr-backend") + 1] == "onnx"
    assert args[args.index("--onnx-model") + 1] == "PP-OCRv5_server_rec"
    assert args[args.index("--onnx-device") + 1] == "dml"
    assert args[args.index("--onnx-device-id") + 1] == "1"
    assert args[args.index("--rec-batch-size") + 1] == "8"


def test_paddle_runs_carry_no_onnx_flags(book, tmp_path):
    path, raw = book
    config = sync.load_config(path)

    args = sync.command_args(config, config["output"], tmp_path / "cache", None)

    assert args[args.index("--ocr-backend") + 1] == "paddle"
    assert "--onnx-model" not in args and "--onnx-device" not in args


def test_onnx_and_paddle_caches_never_share_a_directory(book, monkeypatch):
    path, raw = book
    monkeypatch.setattr(sync, "onnx_identity", lambda config: {
        "model": "deadbeef", "provider": config.get("onnxDevice", "auto"),
        "deviceId": 0, "batchSize": config.get("recBatchSize", 16)})
    paddle = sync.cache_directory(sync.load_config(path))

    raw.update(ocrBackend="onnx", onnxDevice="cpu")
    write(path, json.dumps(raw, ensure_ascii=False))
    cpu = sync.cache_directory(sync.load_config(path))
    raw.update(onnxDevice="dml")
    write(path, json.dumps(raw, ensure_ascii=False))
    dml = sync.cache_directory(sync.load_config(path))
    raw.update(recBatchSize=8)
    write(path, json.dumps(raw, ensure_ascii=False))
    smaller = sync.cache_directory(sync.load_config(path))

    assert len({paddle, cpu, dml, smaller}) == 4


def test_onnx_dependency_report_requires_onnxruntime(book):
    assert "onnxruntime" not in sync.dependency_report("paddle")["packages"]
    report = sync.dependency_report("onnx")
    assert "onnxruntime" in report["packages"]
    assert report["backend"] == "onnx"


@pytest.mark.parametrize("patch,message", [
    ({"onnxDevice": "cuda"}, "onnxDevice must be"),
    ({"onnxDeviceId": -1}, "onnxDeviceId must be"),
    ({"recBatchSize": 0}, "recBatchSize must be"),
])
def test_rejected_onnx_settings(book, patch, message):
    path, raw = book
    raw.update(ocrBackend="onnx", **patch)
    write(path, json.dumps(raw, ensure_ascii=False))
    with pytest.raises(ValueError, match=message):
        sync.load_config(path)
