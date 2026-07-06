"""飞书交付(自建应用图片推送):用自建应用机器人把日报 PNG 推到用户 open_id 私信。

webhook 自定义机器人发不了图;自建应用可以:
  app_id/app_secret → tenant_access_token → 上传 PNG 拿 image_key → 发 msg_type=image 到 open_id。
流程:tenant_access_token → 上传图片 → 私信发送;async httpx 实现。

缺三件套(PULSEWIRE_FEISHU_APP_ID / _SECRET / _USER_OPENID)→ skipped,不假装发成功。
任一图上传/发送失败 → 整通道 failed(如实冒泡),不静默吞。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from PIL import Image

from pulsewire.obs import get_logger

from .base import ChannelResult, DeliverPayload

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/images"
_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
_UPLOAD_RETRIES = 3
_RATE_SLEEP = 0.3  # 飞书限流:uploadImage 5次/秒、send 100次/分,逐图小睡兜底
_MAX_UPLOAD_BYTES = 9_500_000  # 飞书图片上限 10MB,留头寸;超限等比缩小重存


def _fit_for_upload(path: Path) -> tuple[Path, bool]:
    """飞书图片上限 10MB:未超限原样返回;超限则等比缩小重存为临时 PNG。

    返回 (可上传路径, 是否临时文件)。压缩失败时清理临时文件并退回原图(该图大概率上传失败,
    但不崩、不留垃圾)。Pillow 解压炸弹保护仅在本函数内临时放宽(长图是本地可信 render 产物),
    finally 复位,避免影响同进程其它 Pillow 调用。
    """
    if path.stat().st_size <= _MAX_UPLOAD_BYTES:
        return path, False
    tmp = Path(tempfile.gettempdir()) / f"pw_feishu_{path.stem}.png"
    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as img:
            w, h = img.size
            scale = 1.0
            for _ in range(6):
                scale *= 0.82
                resized = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
                resized.save(tmp, format="PNG", optimize=True)
                if tmp.stat().st_size <= _MAX_UPLOAD_BYTES:
                    break
        return tmp, True
    except Exception:  # 压缩异常:清理临时文件、退回原图,不崩不泄漏
        tmp.unlink(missing_ok=True)
        return path, False
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit


async def _get_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> str | None:
    resp = await client.post(_TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret})
    data = resp.json()
    return data.get("tenant_access_token") if data.get("code") == 0 else None


async def _upload_image(client: httpx.AsyncClient, token: str, path: Path) -> str | None:
    """上传图片换 image_key,重试 _UPLOAD_RETRIES 次;全失败返回 None。"""
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(_UPLOAD_RETRIES):
        try:
            with path.open("rb") as fh:
                resp = await client.post(
                    _UPLOAD_URL,
                    headers=headers,
                    data={"image_type": "message"},
                    files={"image": (path.name, fh, "image/png")},
                )
            data = resp.json()
            if data.get("code") == 0:
                return data["data"]["image_key"]
        except Exception:  # 网络/解析抖动:重试,不在此处冒泡
            pass
        await asyncio.sleep(_RATE_SLEEP)
    return None


async def _send_message(client: httpx.AsyncClient, token: str, openid: str, content: dict, msg_type: str) -> bool:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    body = {"receive_id": openid, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)}
    try:
        resp = await client.post(_SEND_URL, headers=headers, json=body)
        return resp.json().get("code") == 0
    except Exception:
        return False


async def send_text(text: str, settings: Settings) -> bool:
    """复用自建应用给用户 open_id 私信发一条纯文本(供失败告警走这条"活"通道)。

    缺三件套 → False(交调用方决定是否回退别的通道)。best-effort:任何异常吞成 False,
    绝不抛(告警链不能因告警本身出错而崩)。
    """
    app_id, app_secret, openid = (
        settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_user_openid,
    )
    if not (app_id and app_secret and openid):
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await _get_token(client, app_id, app_secret)
            if not token:
                return False
            return await _send_message(client, token, openid, {"text": text}, "text")
    except Exception as exc:  # noqa: BLE001 — 告警发送失败不再抛(别盖住原始故障)
        log.warning("alert.feishu_app.failed", error=str(exc))
        return False


async def send(payload: DeliverPayload, settings: Settings) -> ChannelResult:
    """复用自建应用把日报推到用户 open_id 私信。

    默认:逐张推 payload.image_paths(长图 PNG)。
    feishu_card_enabled 开(前端方向 A):改推**一条**折叠卡(四板可折叠),替代长图。
    """
    app_id, app_secret, openid = (
        settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_user_openid,
    )
    if not (app_id and app_secret and openid):
        return ChannelResult("feishu", "skipped", "未配置 PULSEWIRE_FEISHU_APP_ID/SECRET/USER_OPENID(.env)")

    # 前端方向 A(剪报卡 v2,默认关):先把各版剪报图传上飞书拿 image_key,再发一条折叠卡
    # (每版=折叠面板:剪报图 + 原文链接;某版图缺/传失败 → 该版文字回退,卡照发)。
    # 发卡整体失败 → **回退**照发逐张 PNG(绝不因新格式漏掉今日日报)。
    if getattr(settings, "feishu_card_enabled", False):
        try:
            from .feishu_card import build_digest_card
            async with httpx.AsyncClient(timeout=60) as client:
                token = await _get_token(client, app_id, app_secret)
                if token:
                    image_keys: dict[str, str] = {}
                    board_imgs = [(d.get("key"), d.get("image_path")) for d in payload.domains]
                    board_imgs.append(("github", payload.github_image_path))
                    for bkey, ipath in board_imgs:
                        if not (bkey and ipath and Path(ipath).exists()):
                            continue
                        fitted, is_tmp = _fit_for_upload(Path(ipath))
                        try:
                            ikey = await _upload_image(client, token, fitted)
                        finally:
                            if is_tmp:
                                fitted.unlink(missing_ok=True)
                        if ikey:
                            image_keys[bkey] = ikey
                        else:
                            log.warning("deliver.feishu_app.card_img_failed", board=bkey,
                                        note="本版图上传失败,该版走文字回退")
                        await asyncio.sleep(_RATE_SLEEP)
                    card = build_digest_card(payload, image_keys=image_keys)
                    if await _send_message(client, token, openid, card, "interactive"):
                        return ChannelResult("feishu", "sent",
                                             extra={"format": "card", "images": len(image_keys)})
            log.warning("deliver.feishu_app.card_failed", note="折叠卡发送失败,回退长图 PNG")
        except Exception as exc:  # noqa: BLE001 — 卡片是增强,任何异常都回退 PNG,绝不让今日日报漏发
            log.warning("deliver.feishu_app.card_error", error=str(exc))

    images = [p for p in (Path(s) for s in payload.image_paths) if p.exists()]
    if not images:
        return ChannelResult("feishu", "skipped", "本次无可推送 PNG(请先 render)")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _get_token(client, app_id, app_secret)
            if not token:
                return ChannelResult("feishu", "failed", "拿 tenant_access_token 失败(检查 app_id/secret)")

            # 日期分隔/标题:best-effort,失败不拖垮推图
            await _send_message(
                client, token, openid,
                {"text": f"pulsewire · {payload.title} · {payload.date_str}"}, "text",
            )

            ok, fail = 0, 0
            for img in images:
                fitted, is_tmp = _fit_for_upload(img)
                try:
                    image_key = await _upload_image(client, token, fitted)
                finally:
                    if is_tmp:
                        fitted.unlink(missing_ok=True)
                if image_key and await _send_message(client, token, openid, {"image_key": image_key}, "image"):
                    ok += 1
                else:
                    fail += 1
                    log.warning("deliver.feishu_app.image_failed", image=img.name)
                await asyncio.sleep(_RATE_SLEEP)
    except Exception as exc:  # 整体网络异常:如实冒泡为 failed,不假装发成功
        return ChannelResult("feishu", "failed", f"请求失败:{exc}")

    if fail == 0:
        return ChannelResult("feishu", "sent", extra={"images": ok})
    return ChannelResult(
        "feishu", "failed", f"推图部分失败:成功 {ok} 失败 {fail}",
        extra={"images_ok": ok, "images_fail": fail},
    )
