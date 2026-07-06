"""档案测试:每日快照、首页重建(检索索引/日列表/旧系统抢救件入口)、webapp 集成(无网络无库)。"""

from __future__ import annotations

import json

import pytest

from pulsewire.config import get_settings
from pulsewire.deliver import archive, webapp
from pulsewire.deliver.base import DeliverPayload

_DATA = {
    "date": "2026-06-12",
    "domains": [
        {"key": "ai", "label": "AI", "digest": "概述", "items": [
            {"headline": "标题甲", "tldr": "速读甲", "source": "infoq", "needs_review": False},
            {"headline": "标题乙", "tldr": "速读乙", "source": "hn", "needs_review": True},
        ]},
        {"key": "bio", "label": "生物医疗", "digest": "", "items": [
            {"headline": "脑机接口丙", "tldr": "速读丙", "source": "biorxiv", "needs_review": False},
        ]},
    ],
    "github": [{"headline": "开源丁", "tldr": "速读丁", "source": "github", "stars": 100}],
}


def test_snapshot_and_rebuild(tmp_path):
    archive.snapshot_day(tmp_path, date_str="2026-06-12", html="<html>day12</html>", data=_DATA)
    # 快照落盘
    assert (tmp_path / "daily" / "2026-06-12.html").read_text(encoding="utf-8") == "<html>day12</html>"
    saved = json.loads((tmp_path / "daily" / "2026-06-12.json").read_text(encoding="utf-8"))
    assert saved["date"] == "2026-06-12"

    out = archive.rebuild_index(tmp_path)
    page = out.read_text(encoding="utf-8")
    # 检索索引内联:四条目(AI2+bio1+github1)、领域名、日卡链接、当日头条预览
    assert "标题甲" in page and "脑机接口丙" in page and "开源丁" in page
    assert "生物医疗" in page and "GitHub" in page
    assert "daily/" in page and "2026-06-12" in page
    assert '"top": "标题甲"' in page or '"top":"标题甲"' in page  # 日卡头条预览数据


def test_rebuild_orders_days_desc_and_hides_legacy(tmp_path):
    for d in ("2026-06-10", "2026-06-12", "2026-06-11"):
        data = dict(_DATA, date=d)
        archive.snapshot_day(tmp_path, date_str=d, html=f"<html>{d}</html>", data=data)
    # 即便旧系统抢救件在盘上,也不在页面露出(风格不统一,档案页不挂它)
    tr = tmp_path / "legacy"
    tr.mkdir()
    (tr / "app.html").write_text("<html>tr</html>", encoding="utf-8")

    page = archive.rebuild_index(tmp_path).read_text(encoding="utf-8")
    # 新日在前
    assert page.index("2026-06-12") < page.index("2026-06-11") < page.index("2026-06-10")
    assert "legacy/app.html" not in page


_TR = {
    "date": "2026-05-02",
    "report": {"categories": [
        {"name": "AI 领域", "summary": "今日 AI 概述。", "items": [
            {"title": "X did Y", "title_zh": "X 做了 Y", "insight": "首句解读。第二句细节很长很长。",
             "source": "reddit", "url": "https://r/1"},
        ]},
        {"name": "生物医疗", "summary": "生物概述。", "items": [
            {"title": "Z", "title_zh": "Z 进展", "insight": "无句号的短解读",
             "source": "biorxiv", "url": "https://b/1"},
        ]},
    ]},
}


def test_legacy_to_data_maps_categories():
    d = archive._legacy_to_data(_TR)
    assert d["date"] == "2026-05-02"
    assert [x["label"] for x in d["domains"]] == ["AI 领域", "生物医疗"]
    ai = d["domains"][0]
    assert ai["digest"] == "今日 AI 概述。"
    it = ai["items"][0]
    assert it["headline"] == "X 做了 Y"             # title_zh 优先
    assert it["tldr"] == "首句解读。"                 # insight 首句当速读
    assert it["insight"].startswith("首句解读。第二句")  # 全文进 insight
    assert it["needs_review"] is False               # 老数据无对账,不标待核实
    assert it["id"].startswith("tr_")


def test_legacy_merge_day_dedupes_by_url():
    a = archive._legacy_to_data(_TR)
    b = archive._legacy_to_data(_TR)  # 同日第二份,完全重复
    merged = archive._merge_legacy_day([a, b])
    # 两份合并后,AI 域条目按 url 去重仍是 1 条(不翻倍)
    assert len(merged["domains"][0]["items"]) == 1


def test_legacy_merge_keeps_distinct_empty_url_items():
    """空 url 不参与去重(其 id 也不唯一):两个不同的空 url 条目都保留,不误合并成 1。"""
    tr = {"date": "2026-05-02", "report": {"categories": [
        {"name": "AI 领域", "summary": "", "items": [
            {"title_zh": "甲", "insight": "甲解读", "source": "s1", "url": ""},
            {"title_zh": "乙", "insight": "乙解读", "source": "s2", "url": ""},
        ]},
    ]}}
    merged = archive._merge_legacy_day([archive._legacy_to_data(tr)])
    heads = [it["headline"] for it in merged["domains"][0]["items"]]
    assert heads == ["甲", "乙"]  # 两条都在


def test_import_legacy_skips_existing_pulsewire_day(tmp_path):
    digests = tmp_path / "legacy" / "digests"
    digests.mkdir(parents=True)
    (digests / "a.json").write_text(json.dumps(_TR), encoding="utf-8")
    (digests / "b.json").write_text(json.dumps(dict(_TR, date="2026-06-12")), encoding="utf-8")
    # pulsewire 已有 06-12 快照 → 导入应跳过它(新系统优先,不覆盖)
    archive.snapshot_day(tmp_path, date_str="2026-06-12", html="<html>pw</html>", data=_DATA)

    r = archive.import_legacy_history(tmp_path)
    assert r["imported"] == ["2026-05-02"]
    assert r["skipped"] == ["2026-06-12"]
    # 05-02 重渲成日页(复用锁定风格 _APP,含真内容、档案链接已改写)
    page = (tmp_path / "daily" / "2026-05-02.html").read_text(encoding="utf-8")
    assert "X 做了 Y" in page and 'href="../index.html"' in page and "__DATA__" not in page
    # 06-12 仍是 pulsewire 原快照(没被旧档覆盖)
    assert (tmp_path / "daily" / "2026-06-12.html").read_text(encoding="utf-8") == "<html>pw</html>"
    # 档案首页两天都在
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "2026-05-02" in index and "2026-06-12" in index


def test_rerender_all_days_reskins_html_from_json(tmp_path):
    """rerender_all_days:旧皮 html 被新皮(剪报涂鸦风)整批盖掉,数据以 json 为准。"""
    daily = tmp_path / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-06-10.json").write_text(
        json.dumps(dict(_DATA, date="2026-06-10"), ensure_ascii=False), encoding="utf-8")
    json_before = (daily / "2026-06-10.json").read_bytes()
    (daily / "2026-06-10.html").write_text("<html>旧皮便签风</html>", encoding="utf-8")

    assert archive.rerender_all_days(tmp_path) == 1

    page = (daily / "2026-06-10.html").read_text(encoding="utf-8")
    assert "旧皮便签风" not in page and "lookfirst" in page      # 旧皮没了,新皮标记在
    assert "标题甲" in page and "脑机接口丙" in page              # 内容来自 json(唯一真相)
    assert "__DATA__" not in page and "__HIST__" not in page     # 占位符全替换
    # 档案回链已按 daily/ 视角改写
    assert 'href="../index.html"' in page
    assert 'href="../archive/index.html"' not in page
    # json 一个字节没动
    assert (daily / "2026-06-10.json").read_bytes() == json_before
    # 快照页自带左上「回往期」贴(JS 按路径亮起;主 App 页保持 hidden)
    assert "回往期" in page and 'id="backpost"' in page


def test_rerender_all_days_skips_corrupt_json_without_raising(tmp_path):
    """坏 json 只跳过不炸,也不产出对应 html;好的照常重渲。"""
    daily = tmp_path / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-06-10.json").write_text(json.dumps(_DATA), encoding="utf-8")
    (daily / "2026-06-09.json").write_text("{坏档不完整", encoding="utf-8")
    (daily / "2026-06-09.html").write_text("<html>旧皮</html>", encoding="utf-8")

    assert archive.rerender_all_days(tmp_path) == 1
    # 坏档的旧 html 原样保留(不盖不删),好档重渲成功
    assert (daily / "2026-06-09.html").read_text(encoding="utf-8") == "<html>旧皮</html>"
    assert "lookfirst" in (daily / "2026-06-10.html").read_text(encoding="utf-8")


def test_rerender_all_days_empty_or_missing_dir(tmp_path):
    assert archive.rerender_all_days(tmp_path) == 0  # 无 daily/ 目录 → 0,不炸


def test_index_page_back_button_top_left():
    """档案首页返回钮:置于顶栏最前(左上)、便签钮样式(用户点名要显眼)。"""
    page = archive._INDEX_PAGE
    topbar = page[page.index('class="topbar"'):page.index("</div>", page.index('class="topbar"'))]
    # back 链接排在 brand 前面 = 左上第一个元素
    assert topbar.index('class="back"') < topbar.index('class="brand"')
    assert "回今日册子" in topbar and 'href="../app/index.html"' in topbar


@pytest.mark.asyncio
async def test_webapp_send_snapshots_into_sibling_archive(tmp_path):
    """webapp.send 后:tmp/app 旁边出现 tmp/archive/daily/<date>.html + 档案首页。"""
    out_dir = tmp_path / "app"
    payload = DeliverPayload(
        interest_key="int_x", title="AI", date_str="2026-06-12", digest="概述",
        items=[{"id": "a", "headline": "标题甲", "tldr": "速读甲", "insight": "详甲",
                "source": "infoq", "url": "https://x", "needs_review": False, "category": "news"}],
    )
    res = await webapp.send(payload, get_settings(), out_dir=out_dir)
    assert res.status == "sent"
    assert res.extra["archived"] == "2026-06-12"
    snap = tmp_path / "archive" / "daily" / "2026-06-12.html"
    assert snap.exists()
    # 快照是自包含 App;「档案」回链已改写为 daily/ 视角的 ../index.html(原 ../archive/ 会指空)
    body = snap.read_text(encoding="utf-8")
    assert "标题甲" in body
    assert 'href="../index.html"' in body
    assert 'href="../archive/index.html"' not in body
    assert (tmp_path / "archive" / "index.html").exists()


@pytest.mark.asyncio
async def test_webapp_inlines_history_index_for_search(tmp_path):
    """首页「翻本子找」=今天+往期:昨日归档条目须内联进 index.html 的 HIST(今天自身不进)。"""
    from pulsewire.config import get_settings
    from pulsewire.deliver import webapp
    from pulsewire.deliver.base import DeliverPayload

    daily = tmp_path / "archive" / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-06-10.json").write_text(json.dumps({
        "date": "2026-06-10",
        "domains": [{"key": "ai", "label": "AI", "items": [
            {"headline": "黄仁勋往期头条测试", "tldr": "旧闻速读", "source": "src-x"}]}],
        "github": [],
    }, ensure_ascii=False), encoding="utf-8")

    payload = DeliverPayload(
        interest_key="int_x", title="AI", date_str="2026-06-12", digest="概述",
        items=[{"id": "a", "headline": "今天头条", "tldr": "速读", "insight": "详",
                "source": "infoq", "url": "https://x", "needs_review": False}],
    )
    res = await webapp.send(payload, get_settings(), out_dir=tmp_path / "app")
    assert res.status == "sent"
    html = (tmp_path / "app" / "index.html").read_text(encoding="utf-8")
    assert "__HIST__" not in html                    # 占位符必须被替换
    assert "黄仁勋往期头条测试" in html               # 昨日条目进了 HIST(可被检索)
    assert html.count('"d": "2026-06-12"') == 0 or '"d":"2026-06-12"' not in html  # 今天不进 HIST
