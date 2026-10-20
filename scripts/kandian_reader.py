"""Read successful Kandian OCR pages for text comparison, without reusing its coordinates."""
from __future__ import annotations

import json
from pathlib import Path

from kandian_api import extract_content


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8", errors="strict"))


def kandian_reader(specs: list[str]) -> dict[int, str]:
    """Read directory|first-physical-page, or directory|107,142,... for extracted samples.

    Page filenames retain their local PDF page numbers. With first=21, p0003
    maps to original PDF page 23. Explicit lists map files in filename order.
    Keep API line order, omit only lines explicitly classified as header/gutter,
    and retain all original JSON on disk for subsequent structure checks.
    """
    pages = {}
    for spec in specs:
        directory, _, page_spec = spec.partition("|")
        folder = Path(directory)
        paths = sorted(folder.glob("p[0-9][0-9][0-9][0-9].json"))
        if not paths:
            raise ValueError(f"No Kandian page JSON files in {folder}")
        if "," in page_spec:
            numbers = [int(number) for number in page_spec.split(",")]
            if len(numbers) != len(paths):
                raise ValueError("Explicit physical page list must match the number of page JSON files")
        else:
            first = int(page_spec or 1)
            numbers = [first + int(path.stem[1:]) - 1 for path in paths]
        if any(number < 1 for number in numbers):
            raise ValueError("Physical page numbers must be positive")
        run_path = folder / "_run.json"
        run = read_json(run_path) if run_path.exists() else None
        if run is not None and run.get("source") != "kandianguji":
            raise ValueError("Kandian input directory has a different OCR source")
        for number, path in zip(numbers, paths):
            if number in pages:
                raise ValueError(f"Duplicate physical page {number} in Kandian inputs")
            if run is not None and not run.get("pages", {}).get(str(int(path.stem[1:])), {}).get("ok"):
                continue
            payload = read_json(path)
            if not isinstance(payload, dict) or payload.get("message") != "success":
                continue
            data = payload.get("data")
            lines = data.get("text_lines") if isinstance(data, dict) else None
            if isinstance(lines, list) and lines:
                text = "\n".join(line["text"] for line in lines
                                 if isinstance(line, dict) and isinstance(line.get("text"), str)
                                 and str(line.get("layout_classes_id", 0)) != "1")
            else:
                text = extract_content(payload)
            if text.strip():
                pages[number] = text
    return pages
