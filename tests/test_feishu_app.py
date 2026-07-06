"""飞书自建应用图片推送测试:无凭证 skip、无图 skip、MockTransport 推 N 图 happy path(无真网络)。"""

from __future__ import annotations

import httpx
import pytest
from PIL import Image

from pulsewire.config import Settings
from pulsewire.deliver import feishu_app
from pulsewire.deliver.base import DeliverPayload


def _png(tmp_path, name: str):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")
    return str(p)


def _real_png(tmp_path, name: str = "big.png", size=(64, 64)):
    p = tmp_path / name
    Image.new("RGB", size, (200, 100, 50)).save(p, format="PNG")
    return p


@pytest.mark.asyncio
async def test_skip_when_creds_missing(tmp_path):
    s = Settings(feishu_app_id=None, feishu_app_secret=None, feishu_user_openid=None)
    payload = DeliverPayload(
        interest_key="k", title="AI", date_str="2026-06-11", digest="", items=[],
        image_paths=[_png(tmp_path, "a.png")],
    )
    res = await feishu_app.send(payload, s)
    assert res.status == "skipped"


@pytest.mark.asyncio
async def test_skip_when_no_images(tmp_path):
    s = Settings(feishu_app_id="id", feishu_app_secret="sec", feishu_user_openid="ou_x", feishu_card_enabled=False)
    payload = DeliverPayload(
        interest_key="k", title="AI", date_str="2026-06-11", digest="", items=[], image_paths=[],
    )
    res = await feishu_app.send(payload, s)
    assert res.status == "skipped"


@pytest.mark.asyncio
async def test_pushes_all_images(tmp_path, monkeypatch):
    """token→上传→发图全 code=0 → sent;断言上传次数=图数、发消息次数=图数+1(含日期分隔文字)。"""
    calls = {"token": 0, "upload": 0, "send_image": 0, "send_text": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tenant_access_token" in url:
            calls["token"] += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-abc"})
        if url.endswith("/im/v1/images"):
            calls["upload"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_k"}})
        if "/im/v1/messages" in url:
            body = request.content.decode("utf-8")
            if '"image"' in body:
                calls["send_image"] += 1
            else:
                calls["send_text"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "m1"}})
        return httpx.Response(404, json={"code": 1, "msg": "unexpected"})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(feishu_app.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(feishu_app, "_RATE_SLEEP", 0)  # 测试不真睡

    s = Settings(feishu_app_id="id", feishu_app_secret="sec", feishu_user_openid="ou_x", feishu_card_enabled=False)
    payload = DeliverPayload(
        interest_key="k", title="AI", date_str="2026-06-11", digest="", items=[],
        image_paths=[_png(tmp_path, "a.png"), _png(tmp_path, "b.png"), _png(tmp_path, "c.png")],
    )
    res = await feishu_app.send(payload, s)
    assert res.status == "sent"
    assert res.extra["images"] == 3
    assert calls == {"token": 1, "upload": 3, "send_image": 3, "send_text": 1}


@pytest.mark.asyncio
async def test_failed_when_token_fails(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99, "msg": "app disabled"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feishu_app.httpx, "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    s = Settings(feishu_app_id="id", feishu_app_secret="sec", feishu_user_openid="ou_x", feishu_card_enabled=False)
    payload = DeliverPayload(
        interest_key="k", title="AI", date_str="2026-06-11", digest="", items=[],
        image_paths=[_png(tmp_path, "a.png")],
    )
    res = await feishu_app.send(payload, s)
    assert res.status == "failed"


@pytest.mark.asyncio
async def test_failed_when_image_upload_rejected(tmp_path, monkeypatch):
    """token 成功但上传返回非 0(如缺权限)→ 该图失败 → 整通道 failed,不假装成功。"""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tenant_access_token" in url:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t"})
        if url.endswith("/im/v1/images"):
            return httpx.Response(200, json={"code": 99, "msg": "no permission"})
        if "/im/v1/messages" in url:
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 1})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        feishu_app.httpx, "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr(feishu_app, "_RATE_SLEEP", 0)
    s = Settings(feishu_app_id="id", feishu_app_secret="sec", feishu_user_openid="ou_x", feishu_card_enabled=False)
    payload = DeliverPayload(
        interest_key="k", title="AI", date_str="2026-06-11", digest="", items=[],
        image_paths=[_png(tmp_path, "a.png")],
    )
    res = await feishu_app.send(payload, s)
    assert res.status == "failed"
    assert res.extra.get("images_fail") == 1


def test_fit_for_upload_passthrough_when_small(tmp_path):
    """小图(未超限)原样返回,不生成临时文件。"""
    p = _real_png(tmp_path)
    out, is_tmp = feishu_app._fit_for_upload(p)
    assert out == p and is_tmp is False


def test_fit_for_upload_compresses_and_restores_limit(tmp_path, monkeypatch):
    """超限走压缩:生成临时 PNG;且 Pillow 解压炸弹保护用后复位(不残留为 None)。"""
    p = _real_png(tmp_path, size=(400, 400))
    monkeypatch.setattr(feishu_app, "_MAX_UPLOAD_BYTES", 1)  # 强制走压缩分支
    out, is_tmp = feishu_app._fit_for_upload(p)
    try:
        assert is_tmp is True
        assert out != p and out.exists()
    finally:
        out.unlink(missing_ok=True)
    assert Image.MAX_IMAGE_PIXELS is not None  # finally 已复位,未泄漏为 None


@pytest.mark.asyncio
async def test_card_failure_falls_back_to_png(tmp_path, monkeypatch):
    """卡开关开 + 发卡链路炸 → 必须回退逐张 PNG(今日日报绝不因新格式漏发)。"""
    calls = {"send_image": 0, "interactive": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tenant_access_token" in url:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t"})
        if url.endswith("/im/v1/images"):
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_k"}})
        if "/im/v1/messages" in url:
            body = request.content.decode("utf-8")
            if '"interactive"' in body:
                calls["interactive"] += 1
                return httpx.Response(200, json={"code": 99, "msg": "card rejected"})  # 卡被拒
            if '"image"' in body:
                calls["send_image"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "m"}})
        return httpx.Response(404, json={"code": 1})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        feishu_app.httpx, "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr(feishu_app, "_RATE_SLEEP", 0)
    s = Settings(feishu_app_id="id", feishu_app_secret="sec", feishu_user_openid="ou_x",
                 feishu_card_enabled=True)  # 开卡
    payload = DeliverPayload(
        interest_key="k", title="AI", date_str="2026-07-04", digest="", items=[],
        image_paths=[_png(tmp_path, "a.png"), _png(tmp_path, "b.png")],
    )
    res = await feishu_app.send(payload, s)
    assert res.status == "sent"                # 回退成功
    assert res.extra.get("format") != "card"   # 不是卡,是长图
    assert calls["interactive"] >= 1           # 确实先试了卡
    assert calls["send_image"] == 2            # 两张 PNG 都发了
