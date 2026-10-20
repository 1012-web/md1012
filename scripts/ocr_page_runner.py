"""Checkpoint page OCR runs; HTTP 200 alone is not an OCR success."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from mineru_api import ClientError, read_json, write_json


def parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(total))
    indices = []
    try:
        for part in spec.split(","):
            part = part.strip()
            if "-" in part:
                first, last = map(int, part.split("-"))
            else:
                first = last = int(part)
            if first < 1 or last < first or last > total:
                raise ValueError()
            indices.extend(range(first - 1, last))
    except ValueError as exc:
        raise ClientError(f"Invalid page selection (PDF has {total} pages): {spec}") from exc
    return list(dict.fromkeys(indices))


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def decode_response(status: int, text: str, extract: Callable[[Any], str]):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, "", "http-error" if status != 200 else "not-json"
    if status != 200:
        return payload, "", "http-error"
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("message") == "error":
            return payload, "", "api-error"
    content = extract(payload)
    if not isinstance(content, str) or not content.strip():
        return payload, "", "empty-or-invalid-ocr"
    return payload, content, ""


def run_pages(
    args: Any, metadata: dict, call: Callable[[bytes], tuple[int, str]],
    extract: Callable[[Any], str], render: Callable, parse_pages: Callable,
    import_pdf: Callable, secrets: tuple[str, ...], retries: int = 2,
) -> int:
    pdf = Path(args.pdf).resolve()
    if not pdf.is_file():
        raise ClientError(f"PDF not found: {pdf}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    identity = dict(metadata, pdf=str(pdf), pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    dpi=args.dpi, quality=args.quality)
    run_path = out / "_run.json"
    summary = read_json(run_path, {})
    if summary and "pdf_sha256" not in summary and any(p.get("ok") for p in summary.get("pages", {}).values()):
        raise ClientError("Legacy output cannot be verified against this PDF; use a new output directory.")
    if summary and any(key in summary and summary[key] != value for key, value in identity.items()):
        raise ClientError("Output belongs to a different PDF or OCR configuration; use a new output directory.")
    summary.update(identity)
    summary.setdefault("pages", {})
    failed = []
    doc = import_pdf().open(str(pdf))
    try:
        indices = parse_pages(args.pages, doc.page_count)
        if not indices:
            raise ClientError("No pages selected")
        summary["requested_pages"] = [i + 1 for i in indices]
        summary["finished_at"] = None
        summary["complete"] = False
        write_json(run_path, summary)
        for i in indices:
            page_no = i + 1
            target = out / f"p{page_no:04d}.json"
            previous = summary["pages"].get(str(page_no), {})
            if target.exists() and previous.get("ok") and not args.force:
                saved = target.read_text(encoding="utf-8", errors="strict")
                _, content, error = decode_response(200, saved, extract)
                if not error:
                    (out / f"p{page_no:04d}.txt").write_text(content, encoding="utf-8", errors="strict")
                    print(f"page {page_no}: skip (validated success)", flush=True)
                    continue
            image = render(doc, i, args.dpi, args.quality)
            started = time.monotonic()
            for attempt in range(1, retries + 2):
                status, text = call(image)
                text = redact(text, secrets)
                payload, content, error = decode_response(status, text, extract)
                # Auth errors and application-level errors cannot be fixed by repeating a paid request.
                if not error or status not in (0, 408, 429, 500, 502, 503, 504) or attempt > retries:
                    break
                print(f"page {page_no}: retry {attempt} status={status}", flush=True)
                time.sleep(3 * attempt)
            elapsed = round(time.monotonic() - started, 2)
            entry = dict(ok=not bool(error), status=status, seconds=elapsed, attempts=attempt,
                         image_bytes=len(image), content_chars=len(content))
            if error:
                failed.append(page_no)
                entry["error"] = error
                # Keep failed bodies separate from successful OCR output.
                failure_path = out / f"p{page_no:04d}.failure.json"
                write_json(failure_path, dict(status=status, error=error, response=payload if payload is not None else text))
                entry["response_file"] = failure_path.name
                print(f"page {page_no}: FAILED status={status} error={error}", flush=True)
            else:
                write_json(target, payload)
                (out / f"p{page_no:04d}.txt").write_text(content, encoding="utf-8", errors="strict")
                if isinstance(payload, dict) and "usage" in payload:
                    entry["usage"] = payload["usage"]
                print(f"page {page_no}: ok {elapsed:.2f}s chars={len(content)}", flush=True)
            summary["pages"][str(page_no)] = entry
            write_json(run_path, summary)
            if status in (401, 403) or error == "api-error":
                print("Stopped after API rejection; inspect the failure response before resuming.", flush=True)
                break
        pending = [i + 1 for i in indices if not summary["pages"].get(str(i + 1), {}).get("ok")]
        summary.update(failed_pages=failed, pending_pages=pending, complete=not pending,
                       finished_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat())
        write_json(run_path, summary)
        return 2 if pending else 0
    finally:
        doc.close()
