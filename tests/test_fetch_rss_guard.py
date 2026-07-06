"""codex② 回归:feed 端 200 返回 HTML(拦截页/同意墙)必须按源失败,绝不静默当"正常 0 条"。"""

from __future__ import annotations

import pytest

from pulsewire.sources import rss


class _Resp:
    def __init__(self, text, not_modified=False):
        self.text, self.not_modified = text, not_modified


class _Client:
    def __init__(self, text):
        self._t = text

    async def get(self, url, headers=None):
        return _Resp(self._t)


class _Src:
    url = "https://x.example/feed"
    user_agent = None


@pytest.mark.asyncio
async def test_html_200_raises_source_failure():
    html = "<!DOCTYPE html><html><body>Access denied</body></html>"
    with pytest.raises(ValueError, match="HTML"):
        await rss.collect(_Src(), _Client(html))


@pytest.mark.asyncio
async def test_empty_but_valid_xml_is_fine():
    xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    assert await rss.collect(_Src(), _Client(xml)) == []


@pytest.mark.asyncio
async def test_real_feed_still_parses():
    xml = ('<?xml version="1.0"?><rss version="2.0"><channel>'
           '<item><title>A</title><link>https://x/a</link><description>b</description></item>'
           "</channel></rss>")
    items = await rss.collect(_Src(), _Client(xml))
    assert len(items) == 1 and items[0].title == "A"


@pytest.mark.asyncio
async def test_valid_xml_head_short_circuits_html_sniff():
    """考官 LOW 封死:0 条 feed 但 channel 描述 CDATA 里藏 <html,合法 <?xml 头必须短路不误报。"""
    xml = ('<?xml version="1.0"?><rss version="2.0"><channel>'
           '<description><![CDATA[<html>embedded</html>]]></description>'
           "</channel></rss>")
    assert await rss.collect(_Src(), _Client(xml)) == []  # 不炸,记空
