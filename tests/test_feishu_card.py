"""飞书「今日剪报」卡 v2 测试:折叠壳 + 内嵌剪报图 + 原文链接 / 无图文字回退 / 事实文案。"""

from __future__ import annotations

from pulsewire.deliver.base import DeliverPayload
from pulsewire.deliver.feishu_card import build_digest_card


def _payload():
    return DeliverPayload(
        interest_key="ai", title="AI 编程助手与大语言模型", date_str="2026-07-03",
        digest="今日概览:算力与商业化。",
        items=[{"id": "a1", "headline": "NVIDIA 收入分成", "tldr": "新模式。",
                "insight": "算力工厂化。", "source": "The Verge", "url": "https://x.com/a",
                "needs_review": False}],
        domains=[
            {"key": "ai", "label": "AI", "digest": "AI 概览。", "image_path": "/tmp/ai.png", "items": [
                {"id": "a1", "headline": "NVIDIA 收入分成", "tldr": "新模式。",
                 "source": "The Verge", "url": "https://x.com/a", "needs_review": False},
                {"id": "a2", "headline": "某模型发布", "tldr": "细节待定。",
                 "source": "论坛", "url": "https://x.com/a2", "needs_review": True},
                {"id": "a3", "headline": "无链接条", "tldr": "无 url。",
                 "source": "wire", "url": "", "needs_review": False}]},
            {"key": "bio", "label": "生物医疗", "digest": "bio 概览。", "items": [
                {"id": "b1", "headline": "临床突破", "tldr": "III 期。",
                 "source": "Nature", "url": "https://x.com/b", "needs_review": False}]},
            {"key": "geo", "label": "国际局势", "digest": "geo 概览。", "items": []},
        ],
        github=[{"id": "g1", "headline": "some/repo", "tldr": "工具。",
                 "source": "GitHub", "url": "https://github.com/some/repo",
                 "stars": 12345, "needs_review": False}],
        github_image_path="/tmp/gh.png",
    )


def _panels(card):
    return [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]


def test_card_schema_header_factual_copy():
    card = build_digest_card(_payload())
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "yellow"
    assert card["header"]["title"]["content"] == "Pulsewire · 今日剪报"
    # 副标题只留事实:日期 + 总张数(ai3+bio1+geo0+gh1=5)+ 更新时刻
    assert "5 张" in card["header"]["subtitle"]["content"]
    blob = str(card)
    # 文案铁律:无口号/自夸,版块无编号
    for banned in ("宁缺毋滥", "扔纸篓", "只贴够格", "①", "②", "③", "④"):
        assert banned not in blob, f"口号/编号残留:{banned}"
    assert "数字均对照原文核验" in blob  # 事实性说明保留


def test_one_panel_per_board_first_expanded():
    card = build_digest_card(_payload())
    panels = _panels(card)
    assert len(panels) == 4  # ai / bio / geo / github,一条卡装四版
    assert panels[0]["expanded"] is True
    assert all(p["expanded"] is False for p in panels[1:])
    assert "· 3 张" in panels[0]["header"]["title"]["content"]
    assert "· 0 张" in panels[2]["header"]["title"]["content"]  # 空版也在(0 张)


def test_image_mode_embeds_img_and_links():
    """有 image_key 的版:面板=剪报图 + 原文链接列表(图不能点,链接补手)。"""
    card = build_digest_card(_payload(), image_keys={"ai": "img_k_ai", "github": "img_k_gh"})
    panels = _panels(card)
    ai = panels[0]
    tags = [e.get("tag") for e in ai["elements"]]
    assert "img" in tags, "AI 版应内嵌剪报图"
    img = next(e for e in ai["elements"] if e.get("tag") == "img")
    assert img["img_key"] == "img_k_ai" and img["preview"] is True
    links = str(ai["elements"])
    assert "[NVIDIA 收入分成](https://x.com/a)" in links  # 图下原文直达
    assert "待核实" in links                                # 有链接的待核实条目在链接行带徽标(a2)
    assert "无链接条" not in links                          # 无 url 条目不出链接行(a3,内容在图里)
    # github 版:星数进链接行
    gh = panels[3]
    ghs = str(gh["elements"])
    assert "img_k_gh" in ghs and "★12,345" in ghs


def test_no_image_falls_back_to_text_items():
    """没图的版走文字条目回退(绝不因图缺失漏内容)。"""
    card = build_digest_card(_payload(), image_keys={"ai": "img_k_ai"})  # bio 无图
    panels = _panels(card)
    bio = panels[1]
    tags = [e.get("tag") for e in bio["elements"]]
    assert "img" not in tags
    assert "临床突破" in str(bio["elements"])  # 文字条目在


def test_needs_review_and_empty_board():
    card = build_digest_card(_payload())
    blob = str(card)
    assert "待核实" in blob          # 数字回源铁律:徽标不静默
    assert "[原文]()" not in blob    # 空 url 不产生坏链接
    geo = _panels(card)[2]
    assert geo["elements"], "空版要有占位(飞书要求非空)"


def test_daodu_headlines_present():
    card = build_digest_card(_payload())
    md = next(e for e in card["body"]["elements"] if e.get("tag") == "markdown")
    assert "各版头条" in md["content"]
    assert "NVIDIA 收入分成" in md["content"]


def test_fallback_single_board_when_no_domains():
    p = _payload()
    p.domains = []
    card = build_digest_card(p)
    assert len(_panels(card)) == 2  # 主领域单版 + github(不炸)
