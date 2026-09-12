#!/usr/bin/env python3
"""Copy only the MinerU images that the finished Markdown actually references.

MinerU zips routinely contain images that never appear in the final Markdown (page
decorations, duplicated crops, discarded blocks). Copying the whole images/ folder
bloats the vault; this script copies referenced files, reports orphans, and never
deletes anything.

    python -X utf8 copy_images.py <MinerU输出目录或其 images 目录> <成品目录> [--dry-run] [--report <json>]

Markdown files under <成品目录> (recursively) are scanned for every common reference
shape: ``![alt](images/x.png "标题")``, ``![alt](<images/x y.png>)``, ``![[x.png|300]]``,
``<img src=images/x.png>``, and reference definitions ``[ref]: images/x.png``.
URL escapes such as ``%20`` are decoded. Existing identical files are skipped, differing
files are reported as conflicts and left untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from urllib.parse import unquote

EXTENSIONS = r"png|jpe?g|gif|webp|bmp|jp2|svg|tiff?|avif"
IMAGE_NAME_RE = re.compile(r"\.(?:" + EXTENSIONS + r")$", re.I)

# One pattern per reference shape; the first non-empty group of a match is the raw destination.
# The alt text may contain "]" (``![图 1] 说明](images/x.png)``), so it is matched lazily up to "](".
REFERENCE_PATTERNS = (
    re.compile(r"!\[[^\[]*?\]\(\s*(?:<([^<>]*)>|([^()\s]+))\s*(?:\"[^\"]*\"|'[^']*'|\([^()]*\))?\s*\)"),
    re.compile(r"!\[\[\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]"),
    re.compile(r"<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I),
    re.compile(r"(?m)^[ ]{0,3}\[[^\]\n]+\]:\s*(?:<([^<>]*)>|(\S+))"),
)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_name(destination: str | None) -> str | None:
    """Reduce a raw reference destination to a bare filename, or None when it is not an image."""
    if not destination:
        return None
    value = unquote(destination.strip())
    name = os.path.basename(value.replace("\\", "/")).strip()
    return name if IMAGE_NAME_RE.search(name) else None


def find_images_dirs(source: str) -> list[str]:
    """Collect every images/ directory under the source.

    A book split across several MinerU submissions produces one images/ per part, and the finished
    Markdown references them all; returning only the first one would report the rest as missing.
    """
    if os.path.basename(os.path.normpath(source)).lower() == "images" and os.path.isdir(source):
        return [os.path.normpath(source)]
    found = []
    for root, dirs, _ in os.walk(source):
        for name in sorted(dirs):
            if name.lower() == "images":
                found.append(os.path.join(root, name))
        # An images/ directory holds files, not further parts; do not descend into it.
        dirs[:] = [name for name in dirs if name.lower() != "images"]
    if not found:
        raise SystemExit("no images/ directory under " + source)
    return found


def collect_sources(images_dirs: list[str]) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Merge the parts' images/ directories, reporting names that two parts disagree on."""
    available: dict[str, str] = {}
    origins: dict[str, list[str]] = {}
    for images_dir in images_dirs:
        for name in sorted(os.listdir(images_dir)):
            path = os.path.join(images_dir, name)
            if not os.path.isfile(path):
                continue
            origins.setdefault(name, []).append(path)
            available.setdefault(name, path)
    duplicates = []
    for name, paths in sorted(origins.items()):
        if len(paths) > 1 and len({sha256(path) for path in paths}) > 1:
            duplicates.append({"name": name, "paths": paths})
    return available, duplicates


def referenced_names(target: str) -> set[str]:
    names: set[str] = set()
    for root, _, files in os.walk(target):
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            with open(os.path.join(root, name), encoding="utf-8", errors="strict") as handle:
                text = handle.read()
            for pattern in REFERENCE_PATTERNS:
                for match in pattern.finditer(text):
                    found = image_name(next((group for group in match.groups() if group), None))
                    if found:
                        names.add(found)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="MinerU output directory (or its images/ directory)")
    parser.add_argument("target", help="Directory holding the finished Markdown; images go to <target>/images")
    parser.add_argument("--dry-run", action="store_true", help="report only, copy nothing")
    parser.add_argument("--report", help="write a UTF-8 JSON report here")
    args = parser.parse_args()

    images_dirs = find_images_dirs(args.source)
    available, source_duplicates = collect_sources(images_dirs)
    wanted = referenced_names(args.target)
    target_images = os.path.join(args.target, "images")
    copied, kept, conflicts, missing = [], [], [], []
    ambiguous = {entry["name"] for entry in source_duplicates}
    for name in sorted(wanted):
        source = available.get(name)
        if source is None:
            missing.append(name)
            continue
        destination = os.path.join(target_images, name)
        if os.path.exists(destination):
            if sha256(destination) == sha256(source):
                kept.append(name)
            else:
                conflicts.append(name)
            continue
        if name in ambiguous:
            # Two parts ship different files under this name; picking one silently would be a guess.
            continue
        if not args.dry_run:
            os.makedirs(target_images, exist_ok=True)
            with open(source, "rb") as src, open(destination, "wb") as dst:
                dst.write(src.read())
        copied.append(name)
    orphans = sorted(set(available) - wanted)
    referenced_duplicates = [entry for entry in source_duplicates if entry["name"] in wanted]
    report = {
        "imagesDirs": images_dirs, "target": target_images, "dryRun": args.dry_run,
        "referenced": len(wanted), "copied": copied, "kept": kept, "conflicts": conflicts,
        "missingInSource": missing, "sourceDuplicates": source_duplicates,
        "orphansNotCopied": orphans,
        "orphanBytes": sum(os.path.getsize(available[name]) for name in orphans),
    }
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    # Windows consoles are GBK: keep stdout ASCII, details live in the report.
    print("IMAGES referenced=%d copied=%d kept=%d conflicts=%d missing=%d dups=%d orphans=%d" % (
        len(wanted), len(copied), len(kept), len(conflicts), len(missing),
        len(referenced_duplicates), len(orphans)))
    # Exit 3 = ran, but something needs a human decision; argparse keeps 2 for usage errors.
    if conflicts or missing or referenced_duplicates:
        sys.exit(3)


if __name__ == "__main__":
    main()
