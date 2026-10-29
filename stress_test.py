"""本地网关压测 —— 只压自己的 demo_server，不压上游第三方服务。

    python stress_test.py --requests 200 --concurrency 10
    python stress_test.py --steps 1,5,10,20 --requests 40        # 阶梯并发
    python stress_test.py --model animefy-portrait --requests 20 # 换模型
    python stress_test.py --real-sample 3                        # 真实上游小样本(会烧上游配图额度)

输出：分并发级别的 成功率 / RPS / p50 / p95 / p99 / 错误分类。
"""

from __future__ import annotations

import argparse
import io
import json
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def make_fixture(seed: int) -> bytes:
    rnd = random.Random(seed)
    im = Image.new("RGB", (512, 512), (rnd.randrange(60, 220), rnd.randrange(60, 220), rnd.randrange(60, 220)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def call_once(base: str, model: str, size: str, payload: bytes, timeout: float) -> tuple[bool, float, str]:
    started = time.perf_counter()
    try:
        r = requests.post(
            base + "/v1/images/edits",
            files={"file": ("t.jpg", payload, "image/jpeg")},
            data={"model": model, "size": size, "response_format": "url"},
            timeout=timeout,
        )
        cost = time.perf_counter() - started
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:120]
            return False, cost, f"http{r.status_code}:{detail}"[:140]
        body = r.json()
        if not body.get("data"):
            return False, cost, "empty_data"
        return True, cost, ""
    except requests.Timeout:
        return False, time.perf_counter() - started, "timeout"
    except Exception as exc:                     # noqa: BLE001
        return False, time.perf_counter() - started, f"exc:{type(exc).__name__}"


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return ordered[idx]


def run_level(base: str, model: str, size: str, total: int, concurrency: int, timeout: float) -> dict:
    payloads = [make_fixture(i) for i in range(min(concurrency, 8))]
    latencies: list[float] = []
    errors: dict[str, int] = {}
    ok = 0
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(call_once, base, model, size, payloads[i % len(payloads)], timeout)
            for i in range(total)
        ]
        for fut in as_completed(futures):
            success, cost, err = fut.result()
            latencies.append(cost)
            if success:
                ok += 1
            else:
                key = err.split(":")[0] if err else "unknown"
                errors[key] = errors.get(key, 0) + 1
    wall = time.perf_counter() - wall_start
    return {
        "concurrency": concurrency,
        "total": total,
        "ok": ok,
        "fail": total - ok,
        "wall_s": round(wall, 2),
        "rps": round(total / wall, 2) if wall else 0.0,
        "p50_ms": round(pct(latencies, 50) * 1000, 1),
        "p95_ms": round(pct(latencies, 95) * 1000, 1),
        "p99_ms": round(pct(latencies, 99) * 1000, 1),
        "mean_ms": round(statistics.fmean(latencies) * 1000, 1) if latencies else 0.0,
        "errors": errors,
    }


def health(base: str) -> dict:
    r = requests.get(base + "/healthz", timeout=10)
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8931")
    ap.add_argument("--model", default="animefy-enhance")
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--steps", default="", help="逗号分隔的并发阶梯，如 1,5,10,20")
    ap.add_argument("--timeout", type=float, default=320.0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    info = health(base)
    print(f"gateway mode={info['mode']} models={','.join(info['models'])}")
    if info["mode"] == "real" and not args.steps:
        print("!! 当前是 real 模式：压测量会真实打到上游，请确认配额可控")

    steps = [int(s) for s in args.steps.split(",") if s.strip()] or [args.concurrency]
    results = []
    for conc in steps:
        print(f"\n== concurrency={conc} requests={args.requests} model={args.model} ==")
        res = run_level(base, args.model, args.size, args.requests, conc, args.timeout)
        results.append(res)
        print(
            f"  ok={res['ok']}/{res['total']} fail={res['fail']} "
            f"rps={res['rps']} p50={res['p50_ms']}ms p95={res['p95_ms']}ms p99={res['p99_ms']}ms wall={res['wall_s']}s"
        )
        if res["errors"]:
            print("  errors:", json.dumps(res["errors"], ensure_ascii=False))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"health": info, "results": results}, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json_out}")

    total_fail = sum(r["fail"] for r in results)
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
