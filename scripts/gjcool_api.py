"""把 PDF 交给古籍酷「Chat」接口（OpenAI 兼容）识别，结果按页存盘。

古籍酷（gj.cool）：注册后到账户页 https://www.gj.cool/account 的「Chat」区块获取
Base URL 与 API Key（试用版每日 100 万 token）。接口 OpenAI 兼容，模型名：
ocr（古籍 OCR）/ punctuate（自动标点）/ extract_text（通用提取）。
官方说明：Chat 的识别能力与网页版、工业版 API「完全一致」。

（工业版 API（/ocr_pro，带逐字坐标）走另一套鉴权：API ID + 密码换 access_token，
将来若要用，另行处理。）

凭据按顺序取：--token-file 文件（两行：base_url、api_key）→ 环境变量
GJCOOL_BASE_URL / GJCOOL_API_KEY → configure 存下的（DPAPI，与 mineru_api.py 同目录）。
凭据不进聊天、不进命令行参数、不进仓库。

用法：
  python gjcool_api.py configure                                    # 隐藏输入，存一次凭据
  python gjcool_api.py submit 书.pdf -o <目录> [--pages 1-10] [--model ocr]

每页一个 pNNNN.json（原始响应）与 pNNNN.txt（正文文本），汇总写 <out>/_run.json。
只有已验证成功的页跳过，中断后重跑即续；失败返回非零退出码。stdout 只打 ASCII 行。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ocr_page_runner import parse_pages, run_pages

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mineru_api import (  # noqa: E402  共用配置目录与 DPAPI
    ClientError,
    _dpapi_protect,
    _dpapi_unprotect,
    config_dir,
    read_json,
    write_json,
)

URL_ENV = "GJCOOL_BASE_URL"
TOKEN_ENV = "GJCOOL_API_KEY"
DEFAULT_MODEL = "ocr"
DEFAULT_PROMPT = "请识别这张古籍图片中的全部文字，按原有阅读顺序输出，保持换行，不要增删任何字。"
RETRY = 2
TIMEOUT = 120


def settings_path() -> Path:
    return config_dir() / "gjcool.json"


def store_credentials(base_url: str, api_key: str) -> None:
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()
    if not base_url or not api_key:
        raise ClientError("Base URL and API key cannot be empty")
    current = read_json(settings_path(), {})
    if os.name == "nt":
        current.update(
            {"token_storage": "windows-dpapi", "api_url": base_url, "token": _dpapi_protect(api_key)}
        )
    else:
        current.update({"token_storage": "restricted-file", "api_url": base_url, "token": api_key})
    current["configured_at"] = dt.datetime.now().astimezone().isoformat()
    write_json(settings_path(), current)


def load_credentials(token_file: Path | None = None) -> tuple[str, str]:
    if token_file:
        lines = [ln.strip() for ln in token_file.expanduser().read_text(encoding="utf-8", errors="strict").splitlines() if ln.strip()]
        if len(lines) < 2:
            raise ClientError(f"Token file must contain two lines (base_url, api_key): {token_file}")
        return lines[0], lines[1]
    env_url = os.environ.get(URL_ENV, "").strip()
    env_key = os.environ.get(TOKEN_ENV, "").strip()
    if env_url and env_key:
        return env_url, env_key
    settings = read_json(settings_path(), {})
    base_url = settings.get("api_url", "")
    token = settings.get("token")
    if not base_url or not token:
        raise ClientError("GJ.cool credentials are not configured. Run 'python gjcool_api.py configure' locally.")
    if settings.get("token_storage") == "windows-dpapi":
        token = _dpapi_unprotect(token)
    return base_url, token


def import_pymupdf():
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # type: ignore
        return fitz
    except ImportError:
        raise ClientError("pymupdf is required. Install: python -m pip install pymupdf")


def render_jpeg(doc: Any, page_index: int, dpi: int, quality: int) -> bytes:
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes(output="jpeg", jpg_quality=quality)


def build_chat_urls(base_url: str) -> list[str]:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return [base]
    if base.endswith("/v1"):
        return [base + "/chat/completions"]
    return [base + "/chat/completions", base + "/v1/chat/completions"]


def call_chat(base_url: str, api_key: str, image: bytes, model: str, prompt: str):
    b64 = base64.b64encode(image).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            }
        ],
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    last_status, last_text, last_url = 0, "", ""
    for url in build_chat_urls(base_url):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {api_key}")
        request.add_header("User-Agent", "md1012-gjcool/0.2")
        last_url = url
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, response.read().decode("utf-8", errors="strict"), url
        except urllib.error.HTTPError as exc:
            last_status, last_text = exc.code, exc.read().decode("utf-8", errors="strict")
            if exc.code != 404:
                return last_status, last_text, url
        except urllib.error.URLError as exc:
            return 0, f"URLError: {exc.reason}", url
        except (TimeoutError, ConnectionError) as exc:
            return 0, type(exc).__name__, url
        except UnicodeDecodeError:
            return 200, "Response is not valid UTF-8", url
    return last_status, last_text, last_url


def extract_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        return "".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return content if isinstance(content, str) else ""


def safe(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def cmd_configure(_: argparse.Namespace) -> int:
    print("GJ.cool Chat API credential setup (stored encrypted on this machine)")
    base_url = input("base url: ").strip()
    api_key = getpass.getpass("api key (hidden input): ").strip()
    store_credentials(base_url, api_key)
    print(f"saved to {settings_path()}")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    base_url, api_key = load_credentials(Path(args.token_file) if args.token_file else None)
    def call(image):
        status, text, _ = call_chat(base_url, api_key, image, args.model, args.prompt)
        return status, text
    metadata = dict(source="gjcool-chat", base_url=base_url, model=args.model, prompt=args.prompt)
    return run_pages(args, metadata, call, extract_content, render_jpeg, parse_pages,
                     import_pymupdf, (api_key,), RETRY)


def main() -> int:
    parser = argparse.ArgumentParser(description="GJ.cool (gj.cool) Chat OCR API helper")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("configure", help="store base url and api key locally (hidden input)")
    p.set_defaults(func=cmd_configure)

    s = sub.add_parser("submit", help="render a PDF and submit it page by page")
    s.add_argument("pdf")
    s.add_argument("-o", "--out", required=True, help="output directory (one pNNNN.json per page)")
    s.add_argument("--pages", default=None, help="e.g. 1-10 or 3,5,7 (1-based, default: all)")
    s.add_argument("--model", default=DEFAULT_MODEL, help="ocr / punctuate / extract_text")
    s.add_argument("--prompt", default=DEFAULT_PROMPT, help="custom instruction sent with the image")
    s.add_argument("--dpi", type=int, default=300, help="render DPI (default 300)")
    s.add_argument("--quality", type=int, default=90, help="JPEG quality (default 90)")
    s.add_argument("--token-file", dest="token_file", default=None, help="file with two lines: base_url, api_key")
    s.add_argument("--force", action="store_true", help="redo pages that already have output")
    s.set_defaults(func=cmd_submit)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except ClientError as exc:
        print(f"ERROR: {safe(str(exc))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
