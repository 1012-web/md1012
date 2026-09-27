"""File-only, opt-in PaddleOCR mapping; no API calls or Obsidian control."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
# 只有坐标口径验证过的后端能从这个入口跑：两者共用同一套「列区间→字框」的换算，
# 在 world-ancient-lower 物理页 41-60 上逐行比对过，683 行字符串全等、字框坐标零偏差。
SUPPORTED_BACKENDS = frozenset({"paddle", "onnx"})
# ONNX 后端的默认识别模型，与 sync_mapper.DEFAULT_ONNX_MODEL 同义；
# 这里不 import sync_mapper 是因为 command_args 要在没装 onnxruntime 的机器上也能拼出命令。
DEFAULT_ONNX_MODEL = "PP-OCRv6_medium_rec"


def read_json(path: Path):
    with path.open(encoding="utf-8", errors="strict") as handle:
        return json.load(handle)


def write_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8", errors="strict", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def within(root: Path, name: str, allow_root: bool = False) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root) or (path == root and not allow_root):
        raise ValueError(f"Path must be below {root}: {name}")
    return path


# Mapping data lives in the book folder itself: <book>/_数据/映射 (index + chapters), next to
# <book>/_数据/book.json (the entry the Obsidian plugin discovers) and <book>/_数据/config.json.
DATA_DIR = "_数据"
DEFAULT_OUTPUT = f"{DATA_DIR}/映射"


def default_cache_root() -> Path:
    """OCR caches are machine-local; keep them out of the vault unless the config says otherwise.

    ``MINERU_SYNC_HOME`` overrides the base directory; the default is ``~/.mineru-pdf-sync`` on
    every platform (a plain home dotfolder, unlike %LOCALAPPDATA%, is visible to every process).
    """
    base = os.environ.get("MINERU_SYNC_HOME")
    return (Path(base) if base else Path.home() / ".mineru-pdf-sync") / "cache"


def validate_offsets(raw: dict, files: list[Path]) -> None:
    """Reject pagination entries the mapper would silently ignore.

    A misspelled filename or a stray range never raised before: the mapper simply found no matching
    chapter and every page kept the book-wide offset, which only shows up as wrong coordinates.
    """
    import sync_mapper as mapper

    known = {source.relative_to(raw["markdownRoot"]).as_posix() for source in files}
    offsets, overrides, ranges = mapper.parse_offset_config(
        raw.get("fileOffsets", []), raw.get("pageOverrides", []), raw.get("pageRangeOffsets", [])
    )
    for label, names in (("fileOffsets", offsets), ("pageOverrides", {name for name, _ in overrides}),
                         ("pageRangeOffsets", ranges)):
        unknown = sorted(set(names) - known)
        if unknown:
            raise ValueError(f"{label} names files that are not among the Markdown inputs: {unknown}")
    for name, spans in ranges.items():
        ordered = sorted(spans)
        for (start, end, _), (next_start, next_end, _) in zip(ordered, ordered[1:]):
            if next_start <= end:
                raise ValueError(
                    f"pageRangeOffsets overlap in {name}: {start}-{end} and {next_start}-{next_end}"
                )


def load_config(path: Path) -> dict:
    raw = read_json(path)
    if raw.get("mode") != "character":
        raise ValueError("Explicit mode=character is required; ordinary conversion uses mineru_api.py")
    backend = raw.get("ocrBackend", "paddle")
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            "This entry accepts the coordinate-verified engines only "
            f"({', '.join(sorted(SUPPORTED_BACKENDS))}); others require separate coordinate validation"
        )
    if raw.get("onnxDevice", "auto") not in {"cpu", "dml", "auto"}:
        raise ValueError("onnxDevice must be cpu, dml or auto")
    device_id = raw.get("onnxDeviceId")
    if device_id is not None and (not isinstance(device_id, int) or isinstance(device_id, bool) or device_id < 0):
        raise ValueError("onnxDeviceId must be a non-negative integer")
    batch = raw.get("recBatchSize", 16)
    if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= 256:
        raise ValueError("recBatchSize must be an integer between 1 and 256")
    for key in ("bookId", "title", "pdf", "markdownRoot", "layoutParts", "modelParts", "printedPageOffset"):
        if key not in raw:
            raise ValueError(f"Missing config field: {key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", raw["bookId"]):
        raise ValueError("bookId must use lowercase ASCII letters, digits and hyphens")
    vault = (path.resolve().parent / raw.get("vault", ".")).resolve()
    if not vault.is_dir():
        raise ValueError("Vault directory is missing")
    raw["vault"] = vault
    # markdownRoot is the book folder; "." when the config's vault already is the book folder
    # (the portable layout: <book>/_数据/config.json with "vault": "..").
    raw["markdownRoot"] = within(vault, raw["markdownRoot"], allow_root=True)
    raw["pdf"] = within(vault, raw["pdf"])
    if not raw["pdf"].is_file() or not raw["markdownRoot"].is_dir():
        raise ValueError("PDF or Markdown directory is missing")
    book = raw["markdownRoot"]
    if not raw["pdf"].is_relative_to(book):
        raise ValueError("The PDF must lie inside the book folder (markdownRoot)")
    raw["output"] = (within(vault, raw["output"]) if raw.get("output")
                     else (book / DEFAULT_OUTPUT).resolve())
    if not raw["output"].is_relative_to(book / DATA_DIR) or raw["output"] == (book / DATA_DIR).resolve():
        raise ValueError(f"Mapping output must be a directory under the book's {DATA_DIR}/ folder")
    raw["cache"] = (path.resolve().parent / raw["cache"]).resolve() if raw.get("cache") else default_cache_root()
    for key in ("layoutParts", "modelParts"):
        if not raw[key]:
            raise ValueError(f"{key} must contain source paths and 1-based physical starts")
        parts = []
        for part in raw[key]:
            source = (path.resolve().parent / part["path"]).resolve()
            start = part["physicalStart"]
            if not source.is_file() or not isinstance(start, int) or isinstance(start, bool) or start < 1:
                raise ValueError(f"Invalid source part: {part}")
            parts.append((source, start))
        raw[key] = parts
    names = raw.get("markdownFiles")
    files = [within(raw["markdownRoot"], name) for name in names] if names is not None else sorted(raw["markdownRoot"].glob("[0-9][0-9]-*.md"))
    if not files or any(not p.is_file() or p.suffix.lower() != ".md" for p in files):
        raise ValueError("No valid chapters; specify markdownFiles for custom filenames")
    if len(set(files)) != len(files):
        raise ValueError("Duplicate Markdown inputs")
    for source in files:
        with source.open(encoding="utf-8", errors="strict") as handle:
            handle.read()
    if raw.get("writingMode", "horizontal-tb") not in {"horizontal-tb", "vertical-rl"}:
        raise ValueError("Unsupported writingMode")
    variants = raw.get("variantEquivalents", [])
    # 本书字形表：每组一个字符串，第一个字是原书字形，如 ["黄黃", "説說"]；一个字只能出现在一组里
    if not isinstance(variants, list) or not all(isinstance(group, str) for group in variants):
        raise ValueError("variantEquivalents must be a list of strings such as '黄黃'")
    seen: dict[str, int] = {}
    for number, group in enumerate(variants):
        chars = [char for char in group if not char.isspace()]
        if len(chars) < 2:
            raise ValueError(f"variantEquivalents group {group!r} needs the printed form and at least one variant")
        for char in chars:
            if seen.get(char, number) != number:
                raise ValueError(f"variantEquivalents: {char!r} appears in two groups")
            seen[char] = number
    if not isinstance(raw["printedPageOffset"], int):
        raise ValueError("printedPageOffset must be an integer")
    validate_offsets(raw, files)
    scale = raw.get("renderScale", 2.0)
    if not isinstance(scale, (int, float)) or not 0 < scale <= 8:
        raise ValueError("renderScale must be > 0 and <= 8")
    raw["files"] = files
    raw["configPath"] = path.resolve()
    return raw


def package_version(names: tuple[str, ...]) -> str | None:
    """同一个模块可能由不同发行包提供（onnxruntime / onnxruntime-directml），逐个试。"""
    for name in names:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def dependency_report(backend: str = "paddle") -> dict:
    # PaddleOCR 始终要装：即便识别走 ONNX，补行框用的检测模型仍是 PaddleOCR 的。
    modules = {"paddleocr": "paddleocr", "paddlepaddle": "paddle", "PyMuPDF": "pymupdf",
               "numpy": "numpy", "Pillow": "PIL"}
    distributions = {"paddleocr": ("paddleocr",), "paddlepaddle": ("paddlepaddle",),
                     "PyMuPDF": ("PyMuPDF",), "numpy": ("numpy",), "Pillow": ("Pillow",)}
    if backend == "onnx":
        modules["onnxruntime"] = "onnxruntime"
        distributions["onnxruntime"] = ("onnxruntime-directml", "onnxruntime", "onnxruntime-gpu")
    out = {}
    for package, module in modules.items():
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            available = False
        version = package_version(distributions[package])
        out[package] = ({"available": True, "version": version}
                        if available and version else {"available": False})
    return {"python": sys.version.split()[0], "python311": sys.version_info[:2] == (3, 11),
            "backend": backend, "packages": out,
            "modelWeights": "Not loaded by check; first generation may download model weights.",
            "requirements": str(ROOT / "sync-requirements.txt")}


def inspect_inputs(config: dict) -> dict:
    import sync_mapper as mapper
    layout = mapper.load_layout_parts(config["layoutParts"])
    _, lines = mapper.load_model_parts(config["modelParts"])
    if not layout or not lines or not any(lines.values()):
        raise ValueError("Missing layout/visual line boxes; obtain complete MinerU layout or middle JSON and supported model JSON")
    # Parse once without OCR to detect impossible pagination before a long run.
    offsets, overrides, ranges = mapper.parse_offset_config(
        config.get("fileOffsets", []), config.get("pageOverrides", []), config.get("pageRangeOffsets", [])
    )
    pages = set()
    for source in config["files"]:
        _, blocks = mapper.parse_markdown_file(source, config["markdownRoot"], mapper.page_mapper_for_file(
            source.relative_to(config["markdownRoot"]).as_posix(), config["printedPageOffset"], offsets, overrides, ranges))
        pages.update(c.physical_page for b in blocks for c in b.chars)
    if not pages or min(pages) < 1:
        raise ValueError("No mapped text or invalid physical page; check printed page offsets/overrides")
    return {"chapters": len(config["files"]), "layoutPages": len(layout), "visualLinePages": len(lines),
            "physicalPages": sorted(pages), "missingVisualLinePages": sorted(pages - set(lines))}


def onnx_identity(config: dict) -> dict:
    """ONNX 后端里会改变识别结果的东西：模型权重、实际落到哪个 provider、批量大小。

    provider 要解析过才知道（auto 可能退回 CPU），所以这里真的去问一次 onnxruntime。
    """
    import sync_mapper as mapper

    directory = mapper.onnx_model_directory(config.get("onnxModel", DEFAULT_ONNX_MODEL))
    import onnxruntime

    provider, device_id, _ = mapper.resolve_onnx_device(
        onnxruntime, config.get("onnxDevice", "auto"), config.get("onnxDeviceId")
    )
    return {"model": sha256(directory / "inference.onnx"), "provider": provider,
            "deviceId": device_id, "batchSize": config.get("recBatchSize", 16)}


def cache_directory(config: dict) -> Path:
    # Isolate cached positions by the image and recognition inputs, not just page number.
    backend = config.get("ocrBackend", "paddle")
    identity = {"pdf": sha256(config["pdf"]), "model": config.get("recognitionModel", "PP-OCRv5_mobile_rec"),
                "scale": config.get("renderScale", 2.0), "writingMode": config.get("writingMode", "horizontal-tb"),
                "lineSources": [(sha256(p), start) for p, start in config["modelParts"]],
                "mapper": sha256(ROOT / "sync_mapper.py"),
                "backend": backend, "batchSize": config.get("recBatchSize", 16),
                "packages": dependency_report(backend)["packages"]}
    if backend == "onnx":
        identity["onnx"] = onnx_identity(config)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return config["cache"] / config["bookId"] / key


def command_args(config: dict, output: Path, cache: Path, rerun: str | None) -> list[str]:
    vault = config["vault"]
    args = [sys.executable, "-X", "utf8", str(ROOT / "sync_mapper.py"),
            "--vault", str(vault), "--book-id", config["bookId"], "--title", config["title"],
            "--pdf", config["pdf"].relative_to(vault).as_posix(),
            "--markdown-root", config["markdownRoot"].relative_to(vault).as_posix(),
            "--offset", str(config["printedPageOffset"]), "--output", output.relative_to(vault).as_posix(),
            "--cache", str(cache), "--ocr-backend", config.get("ocrBackend", "paddle"),
            "--writing-mode", config.get("writingMode", "horizontal-tb"),
            "--rec-model", config.get("recognitionModel", "PP-OCRv5_mobile_rec"),
            "--rec-batch-size", str(config.get("recBatchSize", 16)),
            "--render-scale", str(config.get("renderScale", 2.0))]
    for group in config.get("variantEquivalents", []):
        args.extend(["--variant-equivalent", group])
    if config.get("ocrBackend", "paddle") == "onnx":
        args.extend(["--onnx-model", config.get("onnxModel", DEFAULT_ONNX_MODEL),
                     "--onnx-device", config.get("onnxDevice", "auto")])
        if config.get("onnxDeviceId") is not None:
            args.extend(["--onnx-device-id", str(config["onnxDeviceId"])])
    for key, flag in (("layoutParts", "--layout-part"), ("modelParts", "--model-part")):
        for source, start in config[key]:
            args.extend([flag, f"{source}|{start}"])
    for source in config["files"]:
        args.extend(["--markdown-file", source.relative_to(config["markdownRoot"]).as_posix()])
    for key, flag in (("fileOffsets", "--file-offset"), ("pageOverrides", "--page-override"),
                      ("pageRangeOffsets", "--page-range-offset")):
        for value in config.get(key, []):
            args.extend([flag, value])
    config_path = config.get("configPath")
    book = config["markdownRoot"]
    if config_path is not None and config_path.is_relative_to(book):
        # Recorded in _数据/book.json so the Obsidian plugin can rerun pages without machine paths.
        args.extend(["--rebuild-config", config_path.relative_to(book).as_posix()])
    elif config_path is not None:
        # Machine paths never enter the vault, so a config outside the book folder is not recorded.
        # Windows consoles are GBK: keep this ASCII, like every other line this entry point prints.
        print("WARNING: the config file is outside the book folder, so _data/book.json records no "
              "rebuild.config and the plugin cannot rerun pages; move the config to "
              "<book>/_data/config.json (the folder is named with the Chinese word for data) to record it.",
              file=sys.stderr)
    if rerun:
        args.extend(["--rerun-pages", rerun])
    return args


def audit_output(book: Path, index_path: Path) -> dict:
    """Paths inside index.json are relative to the book folder."""
    index = read_json(index_path)
    gaps, unlocated_gaps, block_gaps, mismatch = [], [], [], []
    mapped = total = blocks = unlocated = 0
    for filename, entry in index["chapters"].items():
        with gzip.open(within(book, entry["map"]), "rt", encoding="utf-8", errors="strict") as handle:
            chapter = json.load(handle)
        with within(book, filename).open(encoding="utf-8", errors="strict") as handle:
            text = handle.read().replace("\r\n", "\n")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if chapter["hash"] != digest or entry["hash"] != digest:
            mismatch.append(filename)
        mapped += entry["mappedCharacters"]
        total += entry["totalCharacters"]
        for block in chapter["blocks"]:
            blocks += 1
            pages = sorted({r["page"] for r in block["regions"]})
            expected = block.get("expectedPages", pages)
            missing_pages = sorted(set(expected) - set(pages))
            if (not pages or missing_pages) and block["kind"] in {"paragraph", "footnote", "heading"}:
                block_gaps.append({"file": filename, "kind": block["kind"], "range": [block["from"], block["to"]], "physicalPages": missing_pages})
            if block["missing"]:
                gaps.append({"file": filename, "physicalPages": expected, "ranges": block["missing"]})
                # 有候选坐标的字位置已知，只是读法待定；重跑 OCR 帮不了它们，补修只看真正没有坐标的字。
                placed = {candidate["from"] for candidate in block.get("candidates", [])}
                bare = [span for span in block["missing"] if span[0] not in placed]
                if bare:
                    unlocated += len(bare)
                    unlocated_gaps.append({"file": filename, "physicalPages": expected, "ranges": bare})
    return {"version": 1, "chapters": len(index["chapters"]), "blocks": blocks,
            "mappedCharacters": mapped, "totalCharacters": total,
            "characterCoveragePercent": round(100 * mapped / max(1, total), 2),
            "unlocatedCharacters": unlocated,
            "blockAcceptance": "passed" if not block_gaps and blocks else "failed",
            "unmappedBlocks": block_gaps, "missingCharacterRanges": gaps,
            "unlocatedCharacterRanges": unlocated_gaps, "hashMismatches": mismatch,
            "additionalRepair": {"requiresUserChoice": bool(unlocated_gaps or block_gaps),
                                 "physicalPages": sorted({p for gap in unlocated_gaps + block_gaps for p in gap["physicalPages"]}),
                                 "localOCRMinerUQuota": 0, "extraMinerUSubmission": "requires separate user choice and quota estimate",
                                 "timeEstimate": "Use measured seconds/page and affected pages; no fixed completion guarantee."}}


def generate(config: dict, rerun: str | None = None) -> dict:
    inputs = inspect_inputs(config)
    dependencies = dependency_report(config.get("ocrBackend", "paddle"))
    if not dependencies["python311"] or not all(p["available"] for p in dependencies["packages"].values()):
        raise ValueError("Use isolated Python 3.11 with sync-requirements.txt; run check for details")
    import pymupdf
    with pymupdf.open(config["pdf"]) as doc:
        page_count = len(doc)
    if max(inputs["physicalPages"]) > page_count:
        raise ValueError("Markdown pagination exceeds PDF; fix physical page offsets before OCR")
    if rerun:
        match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", rerun)
        if not match or not 1 <= int(match[1]) <= int(match[2]) <= page_count:
            raise ValueError("--pages must be a valid inclusive 1-based physical range, e.g. 201-201")
    protected = [config["pdf"], *config["files"]]
    before = {str(p): sha256(p) for p in protected}
    output = config["output"]
    if output.exists():
        output = output.with_name(output.name + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    cache = cache_directory(config)
    start = time.monotonic()
    result = subprocess.run(command_args(config, output, cache, rerun), check=False)
    after = {str(p): sha256(p) for p in protected}
    if before != after:
        raise RuntimeError("Input changed during generation; do not accept this mapping")
    if result.returncode:
        raise RuntimeError(f"Mapper failed ({result.returncode}); earlier output directories are preserved")
    report = audit_output(config["markdownRoot"], output / "index.json")
    report.update({"elapsedSeconds": round(time.monotonic() - start, 2), "inputHashes": before,
                   "inputBytesUnchanged": True, "output": str(output), "cache": str(cache),
                   "rerunPhysicalPages": rerun, "apiRequests": 0, "obsidianActions": 0})
    write_json(output / "delivery-report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "generate", "rerun"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pages", help="For rerun only: inclusive physical page range")
    args = parser.parse_args()
    if (args.action == "rerun") != bool(args.pages):
        parser.error("rerun requires --pages; other actions do not accept --pages")
    config = load_config(args.config)
    if args.action == "check":
        print(json.dumps({"inputs": inspect_inputs(config), "dependencies": dependency_report(config.get("ocrBackend", "paddle"))}, ensure_ascii=True))
        return
    report = generate(config, args.pages)
    print("SYNC_REPORT " + str(Path(report["output"]) / "delivery-report.json").encode("ascii", "backslashreplace").decode("ascii"))
    if report["blockAcceptance"] != "passed" or report["hashMismatches"]:
        # 3, not 2: argparse already exits 2 for a usage error, and a caller must be able to tell
        # "the mapping ran and failed acceptance" apart from "the command line was wrong".
        raise SystemExit(3)


if __name__ == "__main__":
    main()
