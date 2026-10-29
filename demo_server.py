"""Animefy 2API demo gateway —— OpenAI 图片接口兼容层。

用法：
    set ANIMEFY_MODE=mock   & python demo_server.py     # 离线自测，不打上游
    set ANIMEFY_MODE=real   & python demo_server.py     # 真打 sci.videomate.cc
    python demo_server.py --port 8931

对外接口：
    GET  /                       demo 页
    GET  /healthz                健康检查
    GET  /v1/models              可用"模型"(= effectType) 列表
    GET  /animefy/effects        effectType 注册表（含 verified 标记）
    POST /v1/images/generations  JSON，image 支持 b64 / data URL / http URL
    POST /v1/images/edits        multipart，字段 file + model
    POST /animefy/upload         multipart 直传，返回上游 CDN URL
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageFilter

from animefy_client import EFFECTS, AnimefyClient, AnimefyError

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":  # Windows GBK 控制台
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "static" / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MODE = os.environ.get("ANIMEFY_MODE", "auto").lower()          # auto | real | mock
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "4"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "300"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_N = int(os.environ.get("MAX_N", "4"))
MAX_SIDE = int(os.environ.get("MAX_SIDE", "1536"))

#: 对外暴露的模型名 -> effectType。只暴露真实出过图的 effectType。
MODELS: dict[str, str] = {
    "animefy-enhance": "2402",
    "animefy-gen": "9999",
    "animefy-portrait": "2501",
}

app = FastAPI(title="Animefy 2API demo", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_client: AnimefyClient | None = None
_gate: asyncio.Semaphore | None = None
_stats = {"requests": 0, "ok": 0, "failed": 0, "inflight": 0}


def _get_client() -> AnimefyClient:
    global _client
    if _client is None:
        _client = AnimefyClient()
    return _client


def _get_gate() -> asyncio.Semaphore:
    """懒初始化：TestClient / 未走 startup 钩子的场景同样可用。"""
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(MAX_CONCURRENCY)
    return _gate


@app.on_event("startup")
async def _startup() -> None:
    _get_gate()
    mode = "real" if MODE == "real" else ("mock" if MODE == "mock" else "auto")
    print(f"[animefy-2api] mode={mode} concurrency={MAX_CONCURRENCY} models={list(MODELS)}")


# ------------------------------------------------------------------ helpers

def _effective_mode() -> str:
    if MODE in ("real", "mock"):
        return MODE
    return "real"          # auto: 有网就真打；失败由调用方降级


def _clamp_size(size: str | None) -> tuple[int, int]:
    """size 形如 '1024x1024'；只接受方形/竖版，边长夹在 512..MAX_SIDE。"""
    if not size or size == "auto":
        return 1024, 1024
    try:
        w_s, _, h_s = size.lower().partition("x")
        w, h = int(w_s), int(h_s)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="size must look like 1024x1024")
    if not (512 <= w <= MAX_SIDE and 512 <= h <= MAX_SIDE):
        raise HTTPException(status_code=400, detail=f"size out of range (512..{MAX_SIDE})")
    return (w // 8) * 8, (h // 8) * 8


def _decode_image(value: str) -> bytes:
    """支持 data URL / 纯 base64 / http(s) URL / 本地文件路径。"""
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        import requests

        proxy = os.environ.get("ANIMEFY_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(value, timeout=60, proxies=proxies)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"fetch image failed: http {r.status_code}")
        if len(r.content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image too large")
        return r.content
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    if os.path.exists(value):
        data = Path(value).read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image too large")
        return data
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"image is not url/b64/path: {exc}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image too large")
    return data


def _save(data: bytes, suffix: str = ".jpg") -> str:
    name = f"{int(time.time())}-{uuid.uuid4().hex[:12]}{suffix}"
    (OUT_DIR / name).write_bytes(data)
    return f"/static/out/{name}"


def _mock_transform(src: bytes, effect_type: str, w: int, h: int) -> bytes:
    """离线"占位"效果：确实基于原图做变换，保证 demo 在无网环境也能跑通链路。"""
    try:
        im = Image.open(io.BytesIO(src)).convert("RGB")
    except Exception:
        im = Image.new("RGB", (w, h), (40, 40, 40))
    im = im.resize((w, h), Image.LANCZOS)
    if effect_type == "2402":                      # enhance: 锐化 + 提对比
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=2))
    elif effect_type == "2501":                    # portrait: 柔化 + 轻微等比裁切
        im = im.filter(ImageFilter.SMOOTH_MORE)
    else:                                          # gen: 色阶量化，视觉上明显不同
        im = im.quantize(colors=32).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _models_payload() -> dict:
    out = []
    for name, et in MODELS.items():
        meta = EFFECTS.get(et, {})
        out.append({
            "id": name,
            "object": "model",
            "owned_by": "animefy",
            "effectType": et,
            "engine": meta.get("engine"),
            "description": meta.get("desc"),
            "kind": "image",
        })
    return {"object": "list", "data": out}


# ------------------------------------------------------------------ routes

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "demo.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "mode": _effective_mode(),
        "models": MODELS,
        "inflight": _stats["inflight"],
        "requests": _stats["requests"],
        "ok": _stats["ok"],
        "failed": _stats["failed"],
    }


@app.get("/v1/models")
async def models() -> dict:
    return _models_payload()


@app.get("/animefy/effects")
async def effects() -> dict:
    return {"effectTypes": EFFECTS, "exposed": MODELS}


@app.post("/animefy/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image too large")
    if _effective_mode() == "mock":
        return {"url": _save(data), "mock": True}
    try:
        url = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _get_client().upload_image(data, file.filename or "image.jpg")
        )
    except AnimefyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"url": url}


async def _run_generation(model: str, image: bytes, size: tuple[int, int], response_format: str,
                          extra: dict | None) -> dict:
    effect_type = MODELS.get(model)
    if effect_type is None:
        raise HTTPException(status_code=400, detail=f"unknown model '{model}', see /v1/models")
    w, h = size
    started = time.time()
    _stats["requests"] += 1
    _stats["inflight"] += 1
    try:
        async with _get_gate():                                   # 并发闸门
            if _effective_mode() == "mock":
                data = await asyncio.get_running_loop().run_in_executor(
                    None, _mock_transform, image, effect_type, w, h)
                job_id, elapsed = f"mock-{uuid.uuid4().hex[:8]}", time.time() - started
            else:
                try:
                    data, task = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: _get_client().generate(
                                image, effect_type, width=w, height=h,
                                extra_params=extra, timeout=REQUEST_TIMEOUT),
                        ),
                        timeout=REQUEST_TIMEOUT + 30,
                    )
                except asyncio.TimeoutError as exc:
                    _stats["failed"] += 1
                    raise HTTPException(status_code=504, detail="upstream timeout") from exc
                except AnimefyError as exc:
                    _stats["failed"] += 1
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                except Exception as exc:                       # noqa: BLE001
                    _stats["failed"] += 1
                    raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
                job_id, elapsed = task.job_id, task.elapsed
        _stats["ok"] += 1
    finally:
        _stats["inflight"] -= 1

    item: dict = {"revised_prompt": None}
    if response_format == "b64_json":
        item["b64_json"] = base64.b64encode(data).decode()
    else:
        item["url"] = _save(data)
    return {
        "created": int(time.time()),
        "data": [item],
        "animefy": {"model": model, "effectType": effect_type, "jobId": job_id,
                    "elapsed": round(elapsed, 2), "mode": _effective_mode()},
    }


@app.post("/v1/images/generations")
async def generations(payload: dict) -> JSONResponse:
    model = payload.get("model") or "animefy-enhance"
    n = payload.get("n", 1)
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="n must be an integer")
    if not 1 <= n <= MAX_N:
        raise HTTPException(status_code=400, detail=f"n must be 1..{MAX_N}")
    size = _clamp_size(payload.get("size"))
    fmt = payload.get("response_format", "url")
    if fmt not in ("url", "b64_json"):
        raise HTTPException(status_code=400, detail="response_format must be url or b64_json")

    raw_image = payload.get("image") or payload.get("image_url")
    if not raw_image:
        raise HTTPException(status_code=400, detail="this gateway is image-to-image only: pass 'image' (b64/data-url/http-url)")
    image = _decode_image(raw_image)
    extra = payload.get("extra_params") or None
    if extra is not None and not isinstance(extra, dict):
        raise HTTPException(status_code=400, detail="extra_params must be an object")

    results = [
        await _run_generation(model, image, size, fmt, extra)
        for _ in range(n)
    ]
    first = results[0]
    merged = {"created": first["created"], "data": [r["data"][0] for r in results], "animefy": first["animefy"]}
    return JSONResponse(merged)


@app.post("/v1/images/edits")
async def edits(
    file: UploadFile = File(...),
    model: str = Form("animefy-enhance"),
    size: str = Form("1024x1024"),
    response_format: str = Form("url"),
) -> JSONResponse:
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image too large")
    if response_format not in ("url", "b64_json"):
        raise HTTPException(status_code=400, detail="response_format must be url or b64_json")
    return await _run_generation(model, data, _clamp_size(size), response_format, None)


@app.post("/animefy/raw")
async def raw(request: Request) -> dict:
    """研究用直通：{effectType, image(b64/url), width, height, extraParams}，不经过模型白名单。"""
    payload = await request.json()
    et = str(payload.get("effectType") or "")
    if not et:
        raise HTTPException(status_code=400, detail="effectType required")
    image = _decode_image(payload["image"])
    w = int(payload.get("width") or 1024)
    h = int(payload.get("height") or 1024)
    if _effective_mode() == "mock":
        return {"url": _save(_mock_transform(image, et, w, h)), "mock": True, "effectType": et}
    try:
        data, task = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _get_client().generate(image, et, width=w, height=h,
                                           extra_params=payload.get("extraParams"),
                                           timeout=REQUEST_TIMEOUT),
        )
    except AnimefyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"url": _save(data), "jobId": task.job_id, "elapsed": round(task.elapsed, 2), "effectType": et}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8931"))
    for i, argv in enumerate(sys.argv):
        if argv == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
