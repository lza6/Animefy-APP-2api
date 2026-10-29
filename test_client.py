"""不依赖网络的回归测试：签名、尺寸钳制、图片解码、mock 变换、模型表。

    pytest -q test_client.py
"""

from __future__ import annotations

import base64
import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("ANIMEFY_MODE", "mock")

from animefy_client import EFFECTS, AnimefyClient, new_user_id, sign  # noqa: E402
import demo_server as srv  # noqa: E402


def _jpeg(size=(64, 64), color=(10, 20, 30)) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def test_sign_is_md5_of_key_plus_seconds():
    # md5("mhmjYS" + "1700000000")
    assert sign("1700000000") == "de1b2d5b6d0f0b0d1d1c3f8c0e6f3a4e" or len(sign("1700000000")) == 32
    import hashlib

    expected = hashlib.md5(b"mhmjYS1700000000").hexdigest()
    assert sign("1700000000") == expected


def test_user_id_shape():
    uid = new_user_id()
    assert len(uid) == 20 and uid.isalnum() and uid.islower()


def test_effects_registry_marks_verified():
    verified = [k for k, v in EFFECTS.items() if v.get("verified")]
    assert set(verified) == {"2402", "9999", "2501"}


def test_clamp_size_accepts_and_rejects():
    assert srv._clamp_size("1024x1024") == (1024, 1024)
    assert srv._clamp_size("768x1376") == (768, 1376)
    assert srv._clamp_size(None) == (1024, 1024)
    assert srv._clamp_size("auto") == (1024, 1024)
    for bad in ("100x100", "5000x5000", "abc", "1024"):
        with pytest.raises(Exception):
            srv._clamp_size(bad)


def test_decode_image_from_b64_and_data_url():
    raw = _jpeg()
    b64 = base64.b64encode(raw).decode()
    assert srv._decode_image(b64) == raw
    assert srv._decode_image("data:image/jpeg;base64," + b64) == raw
    with pytest.raises(Exception):
        srv._decode_image("not-an-image")


def test_mock_transform_size():
    out = srv._mock_transform(_jpeg(), "2402", 256, 320)
    im = Image.open(io.BytesIO(out))
    assert im.size == (256, 320)


def test_gateway_mock_e2e():
    client = TestClient(srv.app)
    assert client.get("/healthz").json()["status"] == "ok"
    models = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert models == ["animefy-enhance", "animefy-gen", "animefy-portrait"]

    files = {"file": ("t.jpg", _jpeg(), "image/jpeg")}
    data = {"model": "animefy-enhance", "size": "512x512", "response_format": "url"}
    r = client.post("/v1/images/edits", files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"][0]["url"].startswith("/static/out/")
    assert body["animefy"]["effectType"] == "2402"

    # 未知模型 -> 400
    r = client.post("/v1/images/edits", files={"file": ("t.jpg", _jpeg(), "image/jpeg")},
                    data={"model": "nope", "size": "512x512"})
    assert r.status_code == 400

    # generations 缺 image -> 400
    r = client.post("/v1/images/generations", json={"model": "animefy-enhance", "size": "512x512"})
    assert r.status_code == 400


def test_client_headers_contract():
    c = AnimefyClient(user_id="probe0000000000000001")
    h = c._headers(json_body=True)
    assert h["cp"] == "com.picastory.ins.editor"
    assert h["version"] == "10186"
    assert h["platform"] == "1"
    assert h["sign"] == sign(h["timeStamp"])
    assert h["Content-Type"] == "application/json"
