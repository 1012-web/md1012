"""Shared text revisions for mappings, crops and resumable corrections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def text_hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")


def write_text(path: Path, text: str) -> None:
    """Atomic replacement, preserving the destination's existing newline style."""
    raw = path.read_bytes() if path.exists() else b""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    with temp.open("w", encoding="utf-8", errors="strict", newline=newline) as stream:
        stream.write(text)
    temp.replace(path)


def body_reading(item: dict) -> str:
    """疑字条目的正文现字。2026-10-20 前的清单与判定用旧字段名 minerU，照旧接受。"""
    return item["current"] if "current" in item else item["minerU"]


def upgrade_legacy(item: dict) -> dict:
    """把旧字段名 minerU 就地改成 current。"""
    if "current" not in item and "minerU" in item:
        item["current"] = item.pop("minerU")
    return item


def read_json(path: Path):
    return json.loads(read_text(path))


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def book_file(book: Path, name: str) -> Path:
    path = (book / name).resolve()
    if not path.is_relative_to(book.resolve()):
        raise ValueError(f"File is outside the book directory: {name}")
    return path


def validate_mapping(files: list[Path], mapping: dict, *, require_hash: bool = True) -> None:
    for path in files:
        expected = mapping["files"][path.name].get("hash")
        if not expected:
            if require_hash:
                raise ValueError(f"Missing source hash for {path.name}; regenerate line_map.py output")
        elif text_hash(read_text(path)) != expected:
            raise ValueError(f"Stale line map for {path.name}; regenerate mapping before comparison or cropping")


def page_selection(spec: str | None) -> set[int] | None:
    if spec is None:
        return None
    result = set()
    for part in spec.split(","):
        bounds = part.strip().split("-")
        if len(bounds) > 2 or not all(value.isdigit() for value in bounds):
            raise ValueError("Invalid physical page selection")
        first, last = int(bounds[0]), int(bounds[-1])
        if first < 1 or last < first:
            raise ValueError("Physical page ranges must be positive and ascending")
        result.update(range(first, last + 1))
    if not result:
        raise ValueError("No pages selected")
    return result
