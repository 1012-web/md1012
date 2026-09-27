"""用 PaddleOCR-VL（视觉语言模型）把 PDF 逐页再读一遍，结果按页存盘，供补块与三方比对用。

它在本 skill 里的角色是「第三个读者」，不是底稿：
- 版面分析（PP-DocLayoutV3）能找回 MinerU 整块漏掉的块（署名、页下注、目录页）→ vl_supplement.py；
- 它按上下文「写」字，标点、注号、模糊字读得比逐字识别全，可做疑字来源 → vl_compare.py；
- 但它会把原书异体「规范化」（原书「遥」写成「遙」），也不给逐字坐标，所以不直接进正文。

识别后端是本机 llama.cpp 服务（llama-server，Vulkan 版可用 AMD/Intel/NVIDIA 显卡）。给了 --llama-dir 与
--model-dir 时本脚本自己起服务（挑专用显存最大的 Vulkan 设备，跑完关掉）；也可以 --server 接一个已开着的。
每页一个 pNNNN.json（物理页，1 起），已存在就跳过，中断后重跑即续。
在 OCR 隔离环境（~/.mineru-pdf-sync/venv，需 paddleocr、paddlex[ocr]、openai）里运行。

用法：
  <隔离环境Python> -X utf8 vl_parse.py --pdf 书.pdf --pages 1-497 --out <书>/_数据/VL \
      --llama-dir ~/.mineru-pdf-sync/llama.cpp/<版本> --model-dir ~/.mineru-pdf-sync/models/PaddleOCR-VL-1.6-GGUF
stdout 只打 ASCII 行；汇总写进 <out>/_run.json。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

RENDER_DPI = 144  # 2 倍（PDF 1pt = 1/72 inch）；VL 版面分析与识别在这个分辨率上实测够用


def parse_pages(value: str, count: int) -> list[int]:
    match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?", value)
    if not match:
        raise SystemExit("--pages must look like 17 or 17-20 (1-based physical pages)")
    start, end = int(match[1]), int(match[2] or match[1])
    if start > end or end > count:
        raise SystemExit(f"--pages {value} is outside 1-{count}")
    return list(range(start, end + 1))


def pick_vulkan_device(server_exe: Path) -> str | None:
    """llama-server --list-devices 里挑专用显存最大的 Vulkan 设备（0 号常是核显）。"""
    out = subprocess.run([str(server_exe), "--list-devices"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    best, size = None, -1
    for name, mib in re.findall(r"(Vulkan\d+):[^(]*\((\d+) MiB", out):
        if int(mib) > size:
            best, size = name, int(mib)
    return best


def wait_healthy(url: str, seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url.rsplit("/v1", 1)[0] + "/health", timeout=2) as response:
                if json.loads(response.read().decode("utf-8")).get("status") == "ok":
                    return
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit("llama-server did not become healthy")


def start_server(llama_dir: Path, model_dir: Path, port: int, device: str | None) -> tuple[subprocess.Popen, str, str]:
    exe = llama_dir / ("llama-server.exe" if sys.platform == "win32" else "llama-server")
    model = next(p for p in model_dir.glob("*.gguf") if "mmproj" not in p.name)
    mmproj = next(model_dir.glob("*mmproj*.gguf"))
    device = device or pick_vulkan_device(exe) or "none"
    args = [str(exe), "-m", str(model), "--mmproj", str(mmproj), "--temp", "0",
            "--host", "127.0.0.1", "--port", str(port), "--ctx-size", "16384"]
    args += ["--device", "none", "-ngl", "0", "--no-mmproj-offload"] if device == "none" else ["--device", device, "-ngl", "99"]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/v1"
    wait_healthy(url)
    return process, url, device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--pages", required=True, help="physical pages, 1-based: 17 or 17-20")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--server", help="an already running llama-server, e.g. http://127.0.0.1:18080/v1")
    parser.add_argument("--llama-dir", type=Path, help="folder holding llama-server; the script starts and stops it")
    parser.add_argument("--model-dir", type=Path, help="folder holding the PaddleOCR-VL GGUF and its mmproj")
    parser.add_argument("--device", help="llama.cpp device (Vulkan1, none = CPU); default: largest Vulkan device")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--pipeline-version", default="v1.6")
    parser.add_argument("--dpi", type=int, default=RENDER_DPI,
                        help="页面渲染分辨率；注号这类小字读丢时可放大到 288 重读（更慢）")
    args = parser.parse_args()

    import pymupdf
    from paddleocr import PaddleOCRVL

    doc = pymupdf.open(args.pdf)
    pages = parse_pages(args.pages, doc.page_count)
    args.out.mkdir(parents=True, exist_ok=True)
    todo = [page for page in pages if not (args.out / f"p{page:04d}.json").exists()]
    process = None
    device = "external"
    url = args.server
    if todo and not url:
        if not (args.llama_dir and args.model_dir):
            raise SystemExit("give --server, or --llama-dir and --model-dir so the script can start llama-server")
        process, url, device = start_server(args.llama_dir, args.model_dir, args.port, args.device)
    timing: dict[str, float] = {}
    try:
        if todo:
            pipe = PaddleOCRVL(pipeline_version=args.pipeline_version, vl_rec_backend="llama-cpp-server",
                               vl_rec_server_url=url, use_doc_orientation_classify=False, use_doc_unwarping=False)
            images = args.out / "_images"
            images.mkdir(exist_ok=True)
            for page in todo:
                image = images / f"p{page:04d}.png"
                doc[page - 1].get_pixmap(dpi=args.dpi).save(image)
                started = time.perf_counter()
                result = next(iter(pipe.predict(str(image))))
                payload = result.json.get("res", result.json) if hasattr(result, "json") else dict(result)
                payload = {"page": page, "renderDpi": args.dpi, "width": payload.get("width"), "height": payload.get("height"),
                           "parsing_res_list": [
                               {k: b.get(k) for k in ("block_label", "block_bbox", "block_content")}
                               for b in payload.get("parsing_res_list", [])
                           ]}
                temporary = args.out / f"p{page:04d}.json.tmp"
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(args.out / f"p{page:04d}.json")
                image.unlink(missing_ok=True)
                timing[str(page)] = round(time.perf_counter() - started, 2)
                print(f"VL page={page} seconds={timing[str(page)]}", flush=True)
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=30)
    summary = {"pdf": args.pdf.name, "pages": args.pages, "done": len(pages) - len(todo) + len(timing),
               "skippedExisting": len(pages) - len(todo), "device": device, "secondsPerPage": timing,
               "pipelineVersion": args.pipeline_version}
    (args.out / "_run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"VL_DONE pages={len(pages)} new={len(timing)} device={device}")


if __name__ == "__main__":
    main()
