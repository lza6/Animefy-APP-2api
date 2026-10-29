"""demo 页真实浏览器 E2E：上传 -> 选模型 -> 生成 -> 收到图片，并截图。

    python verify_demo.py                       # 默认打 http://127.0.0.1:8931 (mock)
    python verify_demo.py --url http://127.0.0.1:8937   # 打 real 模式网关

依赖 playwright（chromium 已安装）。
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


def fixture(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    im = Image.new("RGB", (512, 512), (210, 160, 130))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    path.write_bytes(buf.getvalue())
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8931")
    ap.add_argument("--model", default="animefy-enhance")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()
    img = fixture(ROOT / "fixtures" / "input.jpg")

    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: None)
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector("#model option", state="attached", timeout=15000)
        models = page.eval_on_selector_all("#model option", "els => els.map(e => e.value)")
        print("models in page:", models)
        if args.model not in models:
            failures.append(f"model {args.model} not in dropdown {models}")

        page.select_option("#model", args.model)
        page.set_input_files("#file", str(img))
        page.wait_for_function("document.getElementById('in').src.startsWith('blob:')", timeout=10000)
        print("input preview ok")

        page.click("#go")
        page.wait_for_function(
            "document.getElementById('out').src.includes('/static/out/')",
            timeout=args.timeout * 1000,
        )
        out_src = page.eval_on_selector("#out", "e => e.src")
        cost = page.inner_text("#cost")
        print("output:", out_src, "cost:", cost)
        if "/static/out/" not in out_src:
            failures.append("output src not pointing to gateway")

        page.screenshot(path=str(ROOT / f"browser_demo_{args.model}.png"), full_page=True)
        browser.close()

    print("E2E", "PASS" if not failures else f"FAIL {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
