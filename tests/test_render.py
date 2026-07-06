"""render 纯函数测试:HTML 模板拼装、数字高亮、转义、needs_review 徽标(无 Chrome 无库)。

外加 _screenshot 超时重试逻辑测试:用假 playwright(不真启 Chrome),只验重试/冒泡行为。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from pulsewire.render import build_html, build_overview_html
from pulsewire.render.engine import _screenshot
from pulsewire.render.templates import _highlight


def test_highlight_bolds_numbers_with_units():
    out = _highlight("获得 138782星 和 22995forks")
    assert "<strong>138782星</strong>" in out
    assert "<strong>22995forks</strong>" in out


def test_highlight_escapes_html():
    out = _highlight("<script>alert(1)</script> 5%")
    assert "<script>" not in out  # 原始标签被转义
    assert "&lt;script&gt;" in out
    assert "<strong>5%</strong>" in out


_ITEMS = [
    {"headline": "标题A", "tldr": "速读A", "insight": "详读A 138782星", "source": "github",
     "url": "https://x", "needs_review": False},
    {"headline": "标题B", "tldr": "速读B", "insight": "详读B", "source": "hn",
     "url": "https://y", "needs_review": True},
]


def test_build_html_includes_brand_and_items():
    html = build_html(
        title="t", date_display="2026 · 06 · 08", category="AI",
        digest="今日概述", items=_ITEMS, footer_info="footer", width=1080,
    )
    assert "Pulsewire" in html and '<span class="cn">日报' in html  # 剪报本报头
    assert "今日概述" in html
    assert "标题A" in html and "标题B" in html
    assert "速读A" in html and "详读A" in html  # tldr + insight 批注便利贴
    assert "<strong>138782星</strong>" in html  # 数字荧光高亮(已对账)
    assert "待核实" in html  # needs_review 红笔圈
    assert html.count('class="clipwrap"') == 2  # 两张撕边剪报
    assert "✂ 剪自" in html  # 剪报来源行


def test_build_html_no_digest_omits_summary_block():
    html = build_html(
        title="t", date_display="d", category="c", digest="",
        items=[{"headline": "h", "tldr": "t", "insight": "s", "source": "x", "url": "#", "needs_review": False}],
        footer_info="f", width=1080,
    )
    assert 'class="edpost"' not in html  # digest 为空时不出编者按便利贴


def test_build_overview_lists_tldr():
    html = build_overview_html(
        title="t", date_display="d", category="AI", items=_ITEMS, footer_info="f", width=1080,
    )
    assert "速读A" in html and "速读B" in html  # 速读卡用 tldr
    assert "详读A" not in html  # 速读卡不含 insight 正文
    assert html.count('class="orow"') == 2
    assert "待核实" in html


# ── _screenshot 超时重试(假 playwright,不真启 Chrome)──────────────────────

class _FakePage:
    """只实现 _screenshot 用到的接口;按 fail_times 模拟前几次截图超时。"""

    def __init__(self, counter: dict, fail_times: int):
        self._counter = counter
        self._fail_times = fail_times

    def set_default_timeout(self, ms): pass
    def set_default_navigation_timeout(self, ms): pass
    async def set_content(self, html, wait_until=None): pass
    async def wait_for_timeout(self, ms): pass

    async def screenshot(self, path=None, full_page=False):
        self._counter["shots"] += 1
        if self._counter["shots"] <= self._fail_times:
            raise TimeoutError("simulated render timeout")  # 模拟机器忙时截图撑爆
        Path(path).write_bytes(b"PNG-bytes")  # 成功:落个文件


class _FakeBrowser:
    def __init__(self, page): self._page = page
    async def new_page(self, viewport=None): return self._page
    async def close(self): pass


class _FakeCM:
    """async_playwright() 返回的异步上下文管理器;每次进入给一个新 pw。"""

    def __init__(self, page): self._page = page
    async def __aenter__(self):
        chromium = types.SimpleNamespace(launch=self._launch)
        return types.SimpleNamespace(chromium=chromium)
    async def __aexit__(self, *exc): return False
    async def _launch(self, **kwargs): return _FakeBrowser(self._page)


def _patch_playwright(monkeypatch, page):
    # _screenshot 内部 `from playwright.async_api import async_playwright`,故 patch 模块属性即可
    import playwright.async_api as pw_mod
    monkeypatch.setattr(pw_mod, "async_playwright", lambda: _FakeCM(page))


def _settings(retries: int):
    return types.SimpleNamespace(render=types.SimpleNamespace(
        width=1080, timeout_ms=1000, retries=retries, use_system_chrome=False, settle_ms=0,
    ))


async def test_screenshot_retries_then_succeeds(tmp_path, monkeypatch):
    """第一次截图超时,重试第二次成功 → 不抛、产物落地、共截 2 次。"""
    counter = {"shots": 0}
    _patch_playwright(monkeypatch, _FakePage(counter, fail_times=1))
    out = tmp_path / "card.png"
    await _screenshot("<html></html>", out, _settings(retries=2))
    assert counter["shots"] == 2  # 1 次失败 + 1 次成功
    assert out.read_bytes() == b"PNG-bytes"


async def test_screenshot_raises_after_exhausting_retries(tmp_path, monkeypatch):
    """一直超时 → 跑满 retries+1 次后冒泡(不静默产空图)。"""
    counter = {"shots": 0}
    _patch_playwright(monkeypatch, _FakePage(counter, fail_times=99))
    out = tmp_path / "card.png"
    with pytest.raises(TimeoutError):
        await _screenshot("<html></html>", out, _settings(retries=1))
    assert counter["shots"] == 2  # retries=1 → 共 2 次尝试全失败
    assert not out.exists()  # 没产出半成品
