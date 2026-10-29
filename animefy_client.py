"""Animefy (com.picastory.ins.editor) cloud-algo protocol client.

逆向来源：Animefy v2.37.10186 (versionCode 10186) APK 静态分析 + 线上黑盒验证。
证据见 docs/REVERSE-REPORT.md。

核心协议
--------
1. 鉴权是「无账号」的：客户端自己生成 userid，签名 = md5(signKey + 秒级时间戳)。
   signKey 在 APK 里是 DES-CBC 加密字面量 aQOWS13Le3U=（key=IM41OUCa, iv=1234567890ABCDEF）。
   服务端只校验签名，不校验 userid 是否存在 —— 这是全链匿名可用的前提。
2. 三步走：multipart 上传图片 -> createAiStyleTask 建任务 -> queryAiStyleTask 轮询。
3. 统一响应信封：{"c": 200, "m": null, "d": {...}}，c != 200 即业务错误。
"""

from __future__ import annotations

import hashlib
import io
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any

import requests

# ---------------------------------------------------------------- 常量（全部来自 APK）

BASE_URL = "https://sci.videomate.cc"
#: DES 解出的 CloudAlgo privateKey（参见 docs/REVERSE-REPORT.md §3.2）
SIGN_KEY = "mhmjYS"
PACKAGE = "com.picastory.ins.editor"
VERSION_CODE = "10186"          # versionName 2.37.10186
PLATFORM = "1"                  # 1 = Android

UPLOAD_PATH = "/algo/v1/aiGenerate-aigc/multipleUpload"
CREATE_PATH = "/algo/v1/faceFusion/createAiStyleTask"
QUERY_PATH = "/algo/v1/faceFusion/queryAiStyleTask"
#: AIGC（新版）风格任务，另有 /algo/v1/faceFusion/aigc/image2image/createImageTask
CREATE_AIGC_PATH = "/algo/v1/faceFusion/aigc/image2image/createAiStyleTask"

#: 已验证可出图的 effectType —— jobId 前缀即上游引擎名
EFFECTS: dict[str, dict[str, Any]] = {
    "2402": {
        "engine": "aiImageResolution/imagesharpen",
        "desc": "AI Enhance：锐化/超分，输出被放大到 1024x1024",
        "verified": True,
        "default_size": (1024, 1024),
    },
    "9999": {
        "engine": "PL_60016_IMG_HA",
        "desc": "AI 生成（写实向），输出竖版 768x1376，忽略 prompt",
        "verified": True,
        "default_size": (768, 1376),
    },
    "2501": {
        "engine": "gx_img2img_speed_2511",
        "desc": "AI 人像/写真 img2img，输出 768x1024~792x1320，忽略 prompt",
        "verified": True,
        "default_size": (768, 1024),
    },
    # ---- 以下仅在「提交被受理」层面验证过，缺参数直接 jobStatus=fail(-1)，未出图 ----
    "2101": {"engine": "22", "desc": "2100 家族（缺参失败）", "verified": False},
    "2201": {"engine": "22", "desc": "2200 家族（缺参失败）", "verified": False},
    "2301": {"engine": "22", "desc": "2300 家族（缺参失败）", "verified": False},
    "2901": {"engine": "4", "desc": "2900 家族（缺参失败）", "verified": False},
    "2701": {"engine": "hairRepaint", "desc": "染发（走视频查询端点）", "verified": False},
}


class AnimefyError(RuntimeError):
    """上游业务错误（信封 c != 200）或网络层错误。"""

    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def new_user_id() -> str:
    """客户端自造 userid（APK 里是 SharedPreferences 首次启动随机生成，服务端不校验）。"""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(20))


def sign(timestamp: str, sign_key: str = SIGN_KEY) -> str:
    """sign = md5(signKey + 秒级时间戳) 小写十六进制。"""
    return hashlib.md5(f"{sign_key}{timestamp}".encode()).hexdigest()


@dataclass
class TaskResult:
    job_id: str
    status: str
    urls: list[str] = field(default_factory=list)
    reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0


class AnimefyClient:
    """同步客户端。线程安全（每线程各自 requests.Session 亦可共享，requests.Session 非线程安全，
    因此这里用 per-thread session）。"""

    def __init__(
        self,
        user_id: str | None = None,
        base_url: str = BASE_URL,
        proxy: str | None = None,
        timeout: float = 60.0,
        retries: int = 2,
    ):
        self.user_id = user_id or os.environ.get("ANIMEFY_USER_ID") or new_user_id()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        proxy = proxy if proxy is not None else os.environ.get("ANIMEFY_PROXY")
        self._proxy = proxy or None
        self._local = __import__("threading").local()

    # ------------------------------------------------------------- 底层
    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.trust_env = False          # 不读环境变量，代理显式传入
            if self._proxy:
                s.proxies = {"http": self._proxy, "https": self._proxy}
            self._local.s = s
        return s

    def _headers(self, json_body: bool) -> dict[str, str]:
        ts = str(int(time.time()))
        h = {
            "userid": self.user_id,
            "userId": self.user_id,      # 上传端点是 userId，任务端点是 userid，两个都带
            "timeStamp": ts,
            "cp": PACKAGE,
            "version": VERSION_CODE,
            "platform": PLATFORM,
            "sign": sign(ts),
        }
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _request(self, method: str, path: str, **kw) -> dict:
        url = self.base_url + path
        timeout = kw.pop("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, timeout=timeout, **kw)
                break
            except requests.RequestException as exc:      # 网络层才重试
                last_exc = exc
                if attempt >= self.retries:
                    raise AnimefyError(f"network error: {exc}") from exc
                time.sleep(0.6 * (attempt + 1))
        else:  # pragma: no cover
            raise AnimefyError(f"network error: {last_exc}")

        if resp.status_code != 200:
            raise AnimefyError(f"http {resp.status_code}: {resp.text[:200]}", code=resp.status_code)
        try:
            body = resp.json()
        except ValueError as exc:
            raise AnimefyError(f"non-json response: {resp.text[:200]}") from exc
        code = body.get("c")
        if code != 200:
            raise AnimefyError(str(body.get("m")), code=code if isinstance(code, int) else None, payload=body)
        return body

    # ------------------------------------------------------------- 业务
    def upload_image(self, data: bytes, filename: str = "image.jpg") -> str:
        """multipart 上传，字段名必须是 files，Content-Type 用 image/jpeg。返回 CDN URL。"""
        body = self._request(
            "POST",
            UPLOAD_PATH,
            headers=self._headers(json_body=False),
            params={"ifHttps": "true"},
            files=[("files", (filename, data, "image/jpeg"))],
            timeout=max(self.timeout, 120),
        )
        urls = body.get("d") or []
        if not urls:
            raise AnimefyError("upload returned empty url list", payload=body)
        return urls[0]

    def create_task(
        self,
        image_url: str,
        effect_type: str,
        width: int | None = None,
        height: int | None = None,
        extra_params: dict[str, Any] | None = None,
        style: str = "aiGenerate",
        level: int = 0,
        token: str = "",
        screen_width: int = 1080,
        path: str = CREATE_PATH,
    ) -> TaskResult:
        """提交风格任务。响应里 jobId 前缀就是上游引擎名（如 aiGenerate / 60019 / PL_60016_IMG_HA）。"""
        import json

        inner: dict[str, Any] = {"effectType": str(effect_type), "oldPicture": "1"}
        if width:
            inner["width"] = int(width)
        if height:
            inner["height"] = int(height)
        if extra_params:
            inner.update(extra_params)

        payload = {
            "styleParams": {
                "style": style,
                "params": json.dumps(inner, separators=(",", ":")),
                "requestUrls": [{"index": 0, "imgUrl": image_url}],
            },
            "level": int(level),
            "token": token,
            "width": int(screen_width),
        }
        body = self._request(
            "POST",
            path,
            headers=self._headers(json_body=True),
            params={"ifWise": "true"},
            data=json.dumps(payload),
        )
        d = body.get("d") or {}
        return TaskResult(
            job_id=d.get("jobId", ""),
            status=d.get("jobStatus", ""),
            urls=d.get("responseUrls") or [],
            reason=d.get("jobReason"),
            raw=d,
        )

    def query_task(self, job_id: str, path: str = QUERY_PATH) -> TaskResult:
        import json

        body = self._request(
            "POST",
            path,
            headers=self._headers(json_body=True),
            params={"ifWise": "true"},
            data=json.dumps({"jobId": job_id}),
        )
        d = body.get("d") or {}
        return TaskResult(
            job_id=d.get("jobId", job_id),
            status=d.get("jobStatus", ""),
            urls=d.get("responseUrls") or [],
            reason=d.get("jobReason"),
            raw=d,
        )

    def wait_task(
        self,
        job_id: str,
        timeout: float = 240.0,
        interval: float = 3.0,
        path: str = QUERY_PATH,
    ) -> TaskResult:
        started = time.time()
        last = TaskResult(job_id=job_id, status="unknown")
        while time.time() - started < timeout:
            last = self.query_task(job_id, path=path)
            last.elapsed = time.time() - started
            if last.status == "success":
                return last
            if last.status in {"fail", "failed", "error"}:
                raise AnimefyError(f"task failed: reason={last.reason}", payload=last.raw)
            time.sleep(interval)
        raise AnimefyError(f"task timeout after {timeout}s (last status={last.status})", payload=last.raw)

    def generate(
        self,
        image: bytes,
        effect_type: str = "2402",
        width: int | None = None,
        height: int | None = None,
        extra_params: dict[str, Any] | None = None,
        timeout: float = 240.0,
        filename: str = "input.jpg",
    ) -> tuple[bytes, TaskResult]:
        """上传 -> 建任务 -> 轮询 -> 下载结果图，返回 (图片字节, 任务信息)。"""
        meta = EFFECTS.get(str(effect_type), {})
        if width is None or height is None:
            dw, dh = meta.get("default_size", (1024, 1024))
            width = width or dw
            height = height or dh
        real_start = time.time()
        url = self.upload_image(image, filename=filename)
        task = self.create_task(url, effect_type, width=width, height=height, extra_params=extra_params)
        if task.status in {"fail", "failed"}:
            raise AnimefyError(f"task rejected: reason={task.reason}", payload=task.raw)
        final = self.wait_task(task.job_id, timeout=timeout)
        if not final.urls:
            raise AnimefyError("task succeeded without urls", payload=final.raw)
        img = self.download(final.urls[0])
        final.elapsed = time.time() - real_start
        return img, final

    def download(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=max(self.timeout, 120))
        if resp.status_code != 200:
            raise AnimefyError(f"download failed http {resp.status_code}")
        return resp.content


def probe() -> None:  # pragma: no cover - 手工探针
    """最小连通性自检：不烧生成配额，只打资源目录接口。"""
    c = AnimefyClient()
    r = c.session.get(
        c.base_url + "/common/vibe/resource",
        headers=c._headers(json_body=False),
        params={"version": VERSION_CODE, "cp": PACKAGE, "platform": PLATFORM,
                "packageLevel": "0", "ifWise": "true", "ifHttps": "true"},
        timeout=40,
    )
    print("userid =", c.user_id)
    print("resource catalog ->", r.status_code, len(r.content), "bytes")
    from PIL import Image
    img = Image.new("RGB", (512, 512), (200, 150, 120))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    url = c.upload_image(buf.getvalue())
    print("upload ok ->", url)


if __name__ == "__main__":  # pragma: no cover
    probe()
