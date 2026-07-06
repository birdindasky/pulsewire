"""deliver 测试:渠道开关、飞书卡片/微信 markdown 拼装、webapp 零后端落盘(无网络无库)。"""

from __future__ import annotations

import json

import pytest

from pulsewire.config import get_settings
from pulsewire.config.models import ChannelToggle, DeliverCfg, FeishuCfg, WechatCfg
from pulsewire.deliver import DeliverPayload
from pulsewire.deliver.engine import _enabled_channels, _tracked_clusters
from pulsewire.deliver.feishu import _build_card
from pulsewire.deliver.wechat import _build_markdown
from pulsewire.deliver import webapp

_ITEMS = [
    {"id": "a", "headline": "标题A", "tldr": "速读A 138782星", "insight": "详读A", "source": "github",
     "url": "https://x", "needs_review": False, "category": "devtools"},
    {"id": "b", "headline": "标题B", "tldr": "速读B", "insight": "详读B", "source": "hn",
     "url": "https://y", "needs_review": True, "category": "news"},
]
_PAYLOAD = DeliverPayload(
    interest_key="int_test", title="AI", date_str="2026-06-08",
    digest="今日概述", items=_ITEMS, image_path=None,
)


def test_tracked_clusters_only_threshold_threads():
    """持续关注徽标映射:只有达 min_days 门槛(在 threads 列表里)的线才给徽标;
    未达门槛的簇不进 → 日报徽标与「在追」露出口径一致。"""
    threads = [
        {"thread_id": "t1", "days": 3, "name": "伊朗冲突"},
        {"thread_id": "t2", "days": 2, "name": "OpenAI IPO"},
    ]
    cmap = {"cA": "t1", "cB": "t2", "cC": "t_below_threshold"}
    out = _tracked_clusters(threads, cmap)
    assert out["cA"] == {"thread_id": "t1", "days": 3, "name": "伊朗冲突"}
    assert out["cB"]["days"] == 2
    assert "cC" not in out  # 所属线未达门槛(不在 threads 里)→ 不给徽标
    assert _tracked_clusters([], cmap) == {}  # 无在追线 → 空


def test_enabled_channels_respects_toggles():
    settings = get_settings().model_copy(update={
        "deliver": DeliverCfg(
            feishu=FeishuCfg(enabled=True),
            wechat=WechatCfg(enabled=False),
            webapp=ChannelToggle(enabled=True),
        )
    })
    assert _enabled_channels(settings) == ["feishu", "webapp"]


def test_feishu_card_has_items_and_review_badge():
    card = _build_card(_PAYLOAD)
    assert card["msg_type"] == "interactive"
    blob = json.dumps(card, ensure_ascii=False)
    assert "标题A" in blob and "标题B" in blob
    assert "今日概述" in blob
    assert "待核实" in blob  # needs_review 标注
    assert "原文" in blob


def test_wechat_markdown_lists_items():
    md = _build_markdown(_PAYLOAD)
    assert "今日概述" in md
    assert "01. 标题A" in md and "02. 标题B" in md
    assert "速读A 138782星" in md  # 文字卡用 tldr
    assert "待核实" in md
    assert "https://x" in md


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return _FakeTxn()


@pytest.mark.asyncio
async def test_webapp_exempt_from_idempotency_others_obey(monkeypatch):
    """当天已推过(has_delivery 恒 True):webapp 仍重写(豁免),飞书被幂等挡掉。

    这是『立即重跑后页面/档案不刷新』bug 的修复契约——锁住别回归。
    """
    from pulsewire.deliver import engine
    from pulsewire.deliver.base import ChannelResult

    settings = get_settings().model_copy(update={
        "deliver": DeliverCfg(
            feishu=FeishuCfg(enabled=True),
            wechat=WechatCfg(enabled=False),
            webapp=ChannelToggle(enabled=True),
        )
    })

    async def _fake_payload(*a, **k):
        return _PAYLOAD

    async def _always_delivered(*a, **k):
        return True  # 模拟"今天已推过"

    recorded: list[str] = []

    async def _fake_record(session, *, cluster_id, channel, trigger_type, run_id, status):
        recorded.append(channel)

    async def _fake_send(payload, settings, *a, **k):
        return ChannelResult(channel="webapp", status="sent")

    monkeypatch.setattr(engine, "_build_payload", _fake_payload)
    monkeypatch.setattr(engine, "has_delivery", _always_delivered)
    monkeypatch.setattr(engine, "record_delivery", _fake_record)
    monkeypatch.setattr(engine, "_SENDERS", {"feishu": _fake_send, "webapp": _fake_send})

    res = await engine.run_deliver(
        settings, interest_key="int_test", sessionmaker=lambda: _FakeSession()
    )
    by_channel = {r["channel"]: r["status"] for r in res["results"]}
    assert by_channel["webapp"] == "sent"      # 豁免幂等,重写
    assert by_channel["feishu"] == "skipped"   # 遵守幂等,被挡
    assert "webapp" not in recorded            # 豁免通道不记账(免唯一键冲突)


@pytest.mark.asyncio
async def test_webapp_writes_data_json_and_shell(tmp_path):
    # 写到 tmp,绝不污染真实 web/app 产物
    res = await webapp.send(_PAYLOAD, get_settings(), out_dir=tmp_path)
    assert res.status == "sent"
    app_dir = tmp_path
    data = json.loads((app_dir / "data.json").read_text(encoding="utf-8"))
    assert data["title"] == "AI"
    assert data["digest"] == "今日概述"
    assert len(data["items"]) == 2
    assert data["items"][1]["needs_review"] is True
    assert (app_dir / "index.html").exists()


@pytest.mark.asyncio
async def test_webapp_aggregates_multi_domains(tmp_path):
    """多领域:payload.domains 落进 data.json + 内联进 index.html,App 下拉能切 AI/生物/地缘。"""
    bio_items = [{"id": "x", "headline": "脑机接口新突破", "tldr": "速读bio", "insight": "详读bio",
                  "source": "biorxiv", "url": "https://b", "needs_review": False, "category": "preprints"}]
    geo_items = [{"id": "y", "headline": "某地缘冲突升级", "tldr": "速读geo", "insight": "详读geo",
                  "source": "aljazeera", "url": "https://g", "needs_review": False, "category": "non-western"}]
    payload = DeliverPayload(
        interest_key="int_ai", title="AI", date_str="2026-06-11",
        digest="AI概述", items=_ITEMS,
        domains=[
            {"key": "ai", "label": "AI", "digest": "AI概述", "items": _ITEMS},
            {"key": "bio", "label": "生物医疗", "digest": "生物概述", "items": bio_items},
            {"key": "geo", "label": "国际局势", "digest": "地缘概述", "items": geo_items},
        ],
    )
    res = await webapp.send(payload, get_settings(), out_dir=tmp_path)
    assert res.status == "sent"
    data = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert [d["key"] for d in data["domains"]] == ["ai", "bio", "geo"]
    assert data["domains"][1]["items"][0]["headline"] == "脑机接口新突破"
    # 内联进单文件 App,file:// 双击即看
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "脑机接口新突破" in html and "某地缘冲突升级" in html
    assert "DATA.domains" in html  # 前端按 domains 驱动下拉


@pytest.mark.asyncio
async def test_run_deliver_threads_trigger_type_into_idempotency(monkeypatch):
    """trigger_type 进幂等键:传入 'event' 时 has_delivery/record_delivery 都按 event 查/记,
    不再写死 'daily'(否则 event 触发会撞 daily 槽)。"""
    from pulsewire.deliver import engine
    from pulsewire.deliver.base import ChannelResult

    settings = get_settings().model_copy(update={
        "deliver": DeliverCfg(
            feishu=FeishuCfg(enabled=True),
            wechat=WechatCfg(enabled=False),
            webapp=ChannelToggle(enabled=False),  # 只留飞书:它守幂等,能观察 trigger_type
        )
    })

    seen_has: list[str] = []
    seen_rec: list[str] = []

    async def _fake_payload(*a, **k):
        return _PAYLOAD

    async def _fake_has(session, *, cluster_id, channel, trigger_type):
        seen_has.append(trigger_type)
        return False  # 没推过 → 走到发送 + 记账

    async def _fake_record(session, *, cluster_id, channel, trigger_type, run_id, status):
        seen_rec.append(trigger_type)

    async def _fake_send(payload, settings, *a, **k):
        return ChannelResult(channel="feishu", status="sent")

    monkeypatch.setattr(engine, "_build_payload", _fake_payload)
    monkeypatch.setattr(engine, "has_delivery", _fake_has)
    monkeypatch.setattr(engine, "record_delivery", _fake_record)
    monkeypatch.setattr(engine, "_SENDERS", {"feishu": _fake_send})

    await engine.run_deliver(
        settings, interest_key="int_test", trigger_type="event",
        sessionmaker=lambda: _FakeSession(),
    )
    assert seen_has == ["event"]   # 幂等查询按 event
    assert seen_rec == ["event"]   # 记账按 event


def test_require_domain_keys_rejects_missing():
    """domain spec 缺 key/label/interest_key → 提前抛清晰 ValueError(取代下游裸 KeyError)。"""
    from pulsewire.deliver.engine import _require_domain_keys

    # 三键齐全:放行
    _require_domain_keys([{"key": "ai", "label": "AI", "interest_key": "int_ai"}])
    # 缺 interest_key:报错且点名缺哪个
    with pytest.raises(ValueError, match="interest_key"):
        _require_domain_keys([{"key": "ai", "label": "AI"}])
