"""把 PDF 交给 PaddleOCR 官方 API（百度飞桨星河社区）用 PaddleOCR-VL 解析，结果按页存盘。

用户什么都不用装、不挑显卡。代价：PDF 会上传到百度服务器；每日有页数上限。

每页一个 pNNNN.json（物理页，1 起；字段 page、width、height、parsing_res_list），
下游 vl_build.py、vl_compare.py、line_map.py 照常读。已存在的页跳过，中断后重跑即续。
原始返回（JSONL）另存在 <out>/_raw/，汇总写 <out>/_run.json。

令牌按顺序取：--token-file 指的文件 → 环境变量 PADDLEOCR_ACCESS_TOKEN → configure 存下的
（Windows 用 DPAPI 加密，与 mineru_api.py 同一个配置目录）。令牌不进聊天、不进命令行参数、不进仓库。

用法：
  python paddle_api.py configure                                   # 隐藏输入，存一次令牌
  python paddle_api.py submit 书.pdf -o <书>/_数据/VL [--pages 1-497] [--model PaddleOCR-VL-1.6]
  python paddle_api.py check-models [--force]                      # 看有没有更新的模型（最多每 30 天一次）
stdout 只打 ASCII 行。
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mineru_api import (  # noqa: E402  共用配置目录与 DPAPI
    ClientError,
    _dpapi_protect,
    _dpapi_unprotect,
    config_dir,
    read_json,
    write_json,
)

API_BASE = "https://paddleocr.aistudio-app.com/api/v2/ocr"
TOKEN_ENV = "PADDLEOCR_ACCESS_TOKEN"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
MAX_UPLOAD_BYTES = 45 * 1024 * 1024  # 官方本地上传上限 50 MB，留余量
CHUNK_PAGES = 100                     # 官方单次上限 1000 页；分小块，失败只重交一块
MODEL_CHECK_DAYS = 30
UNKNOWN_MODEL_CODE = "10007"  # 线上对不存在的模型名回的错误码
OPTIONAL_PAYLOAD = {"useDocOrientationClassify": False, "useDocUnwarping": False, "useChartRecognition": False}


def settings_path() -> Path:
    return config_dir() / "paddleocr.json"


def store_token(token: str) -> None:
    token = token.strip()
    if not token:
        raise ClientError("Token cannot be empty")
    current = read_json(settings_path(), {})
    if os.name == "nt":
        current.update({"token_storage": "windows-dpapi", "token": _dpapi_protect(token)})
    else:
        current.update({"token_storage": "restricted-file", "token": token})
    current["configured_at"] = dt.datetime.now().astimezone().isoformat()
    write_json(settings_path(), current)


def load_token(token_file: Path | None = None) -> str:
    if token_file:
        value = token_file.expanduser().read_text(encoding="utf-8").strip()
        if not value:
            raise ClientError(f"Token file is empty: {token_file}")
        return value
    from_env = os.environ.get(TOKEN_ENV, "").strip()
    if from_env:
        return from_env
    settings = read_json(settings_path(), {})
    value = settings.get("token")
    if not value:
        raise ClientError("PaddleOCR token is not configured. Run 'python paddle_api.py configure' locally.")
    return _dpapi_unprotect(value) if settings.get("token_storage") == "windows-dpapi" else value


def api_url(path: str = "") -> str:
    base = os.environ.get("PADDLEOCR_API_BASE_URL", API_BASE).rstrip("/")
    return f"{base}/{path.lstrip('/')}" if path else base


def _read_json_response(request: urllib.request.Request, timeout: int = 120) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ClientError(f"PaddleOCR HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"PaddleOCR request failed: {exc.reason}") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ClientError(f"PaddleOCR returned non-JSON data: {body[:300]}") from exc
    if result.get("code") not in (0, None):
        raise ClientError(f"PaddleOCR API error {result.get('code')}: {result.get('msg')}")
    return result


def submit_file(token: str, pdf: Path, model: str) -> str:
    """multipart 上传本地文件，返回 jobId。只用标准库。"""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("model", model), ("optionalPayload", json.dumps(OPTIONAL_PAYLOAD))):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"part.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n".encode("utf-8") + pdf.read_bytes() + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    request = urllib.request.Request(
        api_url("jobs"), data=b"".join(parts), method="POST",
        headers={"Authorization": f"bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    job_id = (_read_json_response(request).get("data") or {}).get("jobId")
    if not job_id:
        raise ClientError("PaddleOCR accepted the request but returned no jobId")
    return str(job_id)


def poll_job(token: str, job_id: str, interval: float, timeout: float,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        request = urllib.request.Request(api_url(f"jobs/{job_id}"), headers={"Authorization": f"bearer {token}"})
        data = _read_json_response(request, timeout=60).get("data") or {}
        state = data.get("state")
        if state == "done":
            return data
        if state == "failed":
            raise ClientError(f"PaddleOCR job {job_id} failed: {data.get('errorMsg')}")
        if time.monotonic() > deadline:
            raise ClientError(f"PaddleOCR job {job_id} still {state} after {timeout:.0f}s")
        sleep(interval)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "md1012-paddle-client"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read().decode("utf-8")
    except (OSError, urllib.error.URLError) as exc:
        raise ClientError(f"Download failed: {url}: {exc}") from exc


def page_records(jsonl: str) -> list[dict[str, Any]]:
    """JSONL → 逐页结果（顺序即上传文件里的页序）。只留下游要的字段，丢掉图片链接等。"""
    pages: list[dict[str, Any]] = []
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        for result in json.loads(line)["result"]["layoutParsingResults"]:
            pruned = result["prunedResult"]
            pages.append({
                "width": pruned["width"], "height": pruned["height"],
                "parsing_res_list": [
                    {"block_label": block.get("block_label"), "block_bbox": block.get("block_bbox"),
                     "block_content": block.get("block_content") or ""}
                    for block in pruned.get("parsing_res_list", [])
                ],
            })
    return pages


def parse_pages(value: str | None, count: int) -> list[int]:
    if not value:
        return list(range(1, count + 1))
    match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?", value)
    if not match:
        raise ClientError("--pages must look like 17 or 17-20 (1-based physical pages)")
    start, end = int(match[1]), int(match[2] or match[1])
    if start > end or end > count:
        raise ClientError(f"--pages {value} is outside 1-{count}")
    return list(range(start, end + 1))


def chunks(pages: list[int], size: int) -> list[list[int]]:
    """连续页切块；不连续处（已存在的页被跳过）另起一块。"""
    out: list[list[int]] = []
    for page in pages:
        if out and page == out[-1][-1] + 1 and len(out[-1]) < size:
            out[-1].append(page)
        else:
            out.append([page])
    return out


def write_chunk_pdf(source: Path, pages: list[int], destination: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in pages:
        writer.add_page(reader.pages[page - 1])
    with destination.open("wb") as handle:
        writer.write(handle)


def run_submit(pdf: Path, out: Path, pages_arg: str | None, model: str, token: str,
               interval: float = 5, timeout: float = 3600) -> dict[str, Any]:
    from pypdf import PdfReader

    count = len(PdfReader(str(pdf)).pages)
    wanted = parse_pages(pages_arg, count)
    todo = [page for page in wanted if not (out / f"p{page:04d}.json").exists()]
    out.mkdir(parents=True, exist_ok=True)
    (out / "_raw").mkdir(exist_ok=True)
    jobs = []
    started = time.monotonic()
    queue = chunks(todo, CHUNK_PAGES)
    with tempfile.TemporaryDirectory(prefix="md1012-paddle-") as temp:
        while queue:
            group = queue.pop(0)
            part = Path(temp) / f"p{group[0]:04d}-{group[-1]:04d}.pdf"
            write_chunk_pdf(pdf, group, part)
            if part.stat().st_size > MAX_UPLOAD_BYTES and len(group) > 1:
                half = len(group) // 2
                queue[:0] = [group[:half], group[half:]]
                continue
            job_id = submit_file(token, part, model)
            print(f"SUBMITTED pages={group[0]}-{group[-1]} job={job_id}")
            data = poll_job(token, job_id, interval, timeout)
            jsonl = fetch_text(data["resultUrl"]["jsonUrl"])
            (out / "_raw" / f"p{group[0]:04d}-{group[-1]:04d}.jsonl").write_text(jsonl, encoding="utf-8")
            records = page_records(jsonl)
            if len(records) != len(group):
                raise ClientError(f"job {job_id}: sent {len(group)} pages, got {len(records)} back")
            for page, record in zip(group, records):
                record = {"page": page, "source": "paddleocr-api", "model": model, **record}
                write_json(out / f"p{page:04d}.json", record)
            jobs.append({"pages": [group[0], group[-1]], "jobId": job_id,
                         "extractProgress": data.get("extractProgress")})
            record_usage(len(group))
    summary = {"pdf": str(pdf), "model": model, "source": "paddleocr-api", "requested": len(wanted),
               "skippedExisting": len(wanted) - len(todo), "parsed": len(todo), "jobs": jobs,
               "seconds": round(time.monotonic() - started, 1),
               "finishedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    previous = read_json(out / "_run.json", {})
    summary["history"] = (previous.get("history") or []) + [{k: summary[k] for k in ("model", "parsed", "finishedAt")}]
    write_json(out / "_run.json", summary)
    return summary


def record_usage(pages: int) -> None:
    """本机记当日已交的页数（官方没有用量查询端点；看不到别的设备和网页上的消耗）。"""
    settings = read_json(settings_path(), {})
    today = dt.date.today().isoformat()
    usage = settings.get("usage") or {}
    if usage.get("date") != today:
        usage = {"date": today, "pages": 0}
    usage["pages"] += pages
    settings["usage"] = usage
    write_json(settings_path(), settings)


def next_versions(model: str) -> list[str]:
    """PaddleOCR-VL-1.6 → 候选 1.7、1.8、2.0；无版本号的名字（PaddleOCR-VL）→ 1.0 起。"""
    match = re.fullmatch(r"(.*?)-(\d+)\.(\d+)", model)
    if not match:
        return [f"{model}-1.0"]
    stem, major, minor = match[1], int(match[2]), int(match[3])
    return [f"{stem}-{major}.{minor + 1}", f"{stem}-{major}.{minor + 2}", f"{stem}-{major + 1}.0"]


def blank_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)


def check_models(token: str, current: str, force: bool = False,
                 submit: Callable[[str, Path, str], str] = submit_file) -> dict[str, Any]:
    """拿一张空白页按更高的版本号试交：被接受就说明线上有新模型。最多每 30 天查一次（--force 例外）。
    线上对不存在的模型名当场回 HTTP 400、code 10007（2026-10-16 实测），不花额度；被接受的那个花 1 页。
    只报告，不自动换——换模型要用户点头（use-model）。"""
    settings = read_json(settings_path(), {})
    last = settings.get("model_checked_at")
    if last and not force:
        age = (dt.date.today() - dt.date.fromisoformat(last)).days
        if age < MODEL_CHECK_DAYS:
            return {"skipped": True, "lastChecked": last, "daysAgo": age, "current": current,
                    "newer": settings.get("model_newer") or []}
    newer = []
    with tempfile.TemporaryDirectory(prefix="md1012-paddle-") as temp:
        probe = Path(temp) / "blank.pdf"
        blank_pdf(probe)
        for candidate in next_versions(current):
            try:
                submit(token, probe, candidate)
            except ClientError as exc:
                if UNKNOWN_MODEL_CODE in str(exc):
                    continue
                raise  # 断网、令牌失效等不能当成「没有新模型」
            newer.append(candidate)
    settings.update({"model_checked_at": dt.date.today().isoformat(), "model_newer": newer})
    write_json(settings_path(), settings)
    return {"skipped": False, "current": current, "newer": newer}


def current_model(explicit: str | None) -> str:
    return explicit or read_json(settings_path(), {}).get("model") or DEFAULT_MODEL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="store the access token locally (hidden input)")
    s = sub.add_parser("submit", help="parse a PDF and write one pNNNN.json per page")
    s.add_argument("pdf", type=Path)
    s.add_argument("-o", "--out", type=Path, required=True)
    s.add_argument("--pages", help="1-based physical pages: 17 or 17-20 (default: all)")
    s.add_argument("--model", help=f"default: the configured model, else {DEFAULT_MODEL}")
    s.add_argument("--token-file", type=Path)
    s.add_argument("--interval", type=float, default=5)
    s.add_argument("--timeout", type=float, default=3600)
    c = sub.add_parser("check-models", help="probe for a newer model (at most every 30 days)")
    c.add_argument("--model")
    c.add_argument("--token-file", type=Path)
    c.add_argument("--force", action="store_true")
    u = sub.add_parser("use-model", help="make a model the default for later submits")
    u.add_argument("model")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "configure":
            store_token(getpass.getpass("PaddleOCR access token (input hidden): "))
            print("CONFIGURED")
        elif args.command == "submit":
            summary = run_submit(args.pdf, args.out, args.pages, current_model(args.model),
                                 load_token(args.token_file), args.interval, args.timeout)
            print(f"OK model={summary['model']} parsed={summary['parsed']} skipped={summary['skippedExisting']} "
                  f"seconds={summary['seconds']}")
        elif args.command == "check-models":
            result = check_models(load_token(args.token_file), current_model(args.model), args.force)
            state = "SKIPPED" if result["skipped"] else "CHECKED"
            print(f"{state} current={result['current']} newer={','.join(result['newer']) or 'none'}")
        elif args.command == "use-model":
            settings = read_json(settings_path(), {})
            settings["model"] = args.model
            write_json(settings_path(), settings)
            print(f"MODEL {args.model}")
    except ClientError as exc:
        print("ERROR " + str(exc).encode("ascii", "replace").decode("ascii"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
