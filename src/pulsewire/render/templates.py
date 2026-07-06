"""出图模板:Jinja2 + 锁定视觉「今日剪报本」剪报涂鸦风(见仓库根 STYLE.md,2026-07-04 用户拍板换代)。

三张图共享同一套视觉(新闻纸 + 撕边剪报 + 和纸胶带 + 便利贴批注 + 红笔涂鸦):
- 详读长图(build_html):每条一张撕边剪报卡,headline + tldr(荧光划重点)+ 批注便利贴(insight 全文)。
- 速读卡(build_overview_html):剪报清单,给只想扫一眼的人。
- 中详卡(build_midview_html):速读骨架 + 每条精简批注(2~3 句)。
PNG 是静态截图(无 JS/动画,涂鸦为静态 SVG);网页 App 的动效版在 deliver/webapp.py。
字体走本机系统字体(宋体 Songti SC + 手札体 Hannotate SC + Georgia),不再拉 Google Fonts(离线可渲)。
只渲染 verify 对账后的成稿;needs_review 红笔圈「待核实?」;荧光高亮仅用于已对账数字。
文案铁律(2026-07-04 用户):不写广告语/口号/自夸,版块不编号(平权),只留事实。
"""

from __future__ import annotations

import re

from jinja2 import Environment

# 数字高亮:对带单位的数字加 <strong>(这些数字已过 verify 对账,荧光笔划出安全)
_NUM_HIGHLIGHT_RE = re.compile(
    r"(\d+(?:[,\.]\d+)?(?:\s*(?:亿|万|%|倍|美元|条|分|星|个|人|次|年|月|天|forks?|stars?|points?))*)"
)

# 锁定色(剪报本):新闻纸 #F6F1E2 / 油墨黑 #26221C / 红笔 #D9331F / 便利贴黄 #FFEE9C 粉 #FFD3E1 / 荧光 #FFE05C
_STYLE = """
  :root{--paper:#F6F1E2;--card:#FCFAF3;--ink:#26221C;--ink2:#57524A;--mut:#948D7D;
    --pen:#D9331F;--hlt:#FFE05C;--postit:#FFEE9C;--postit2:#FFD3E1;--tape:rgba(180,215,200,.6);
    --song:'Songti SC','STSong','Noto Serif SC',serif;
    --hand:'Hannotate SC','HanziPen SC','Kaiti SC',cursive;
    --num:Georgia,serif;}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{width:{{ width }}px;background:var(--paper);color:var(--ink);font-family:var(--song);position:relative;}
  body::before{content:'';position:absolute;left:20px;top:0;bottom:0;width:1px;
    background:repeating-linear-gradient(180deg,transparent 0 34px,#cabfa2 34px 35px);opacity:.6;}
  .grain{position:absolute;inset:0;pointer-events:none;opacity:.05;mix-blend-mode:multiply;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}
  svg.doodle{position:absolute;pointer-events:none;overflow:visible;}
  svg.doodle path{fill:none;stroke:var(--pen);stroke-width:3.2;stroke-linecap:round;stroke-linejoin:round;}
  /* ---- 报头 ---- */
  .mast{padding:56px 64px 0;position:relative;}
  .tapecorner{position:absolute;width:120px;height:32px;background:var(--tape);mix-blend-mode:multiply;}
  .tapecorner.tl{left:-26px;top:22px;transform:rotate(-38deg);}
  .tapecorner.tr{right:-26px;top:22px;transform:rotate(38deg);}
  .dateline{font-family:var(--hand);font-size:22px;color:var(--ink2);letter-spacing:.06em;position:relative;display:inline-block;}
  .dateline svg{left:0;right:0;bottom:-10px;height:10px;width:100%;}
  .np{position:relative;display:inline-block;margin-top:16px;}
  .np .en{font-family:var(--num);font-weight:700;font-size:84px;letter-spacing:-.01em;line-height:1;}
  .np .cn{position:relative;font-weight:900;font-size:58px;margin-left:14px;letter-spacing:.1em;}
  .np .circ{left:-16px;top:-18px;width:calc(100% + 32px);height:calc(100% + 34px);}
  .banline{margin-top:26px;display:flex;align-items:baseline;gap:18px;}
  .banline .bt{position:relative;font-weight:900;font-size:40px;letter-spacing:.1em;z-index:1;}
  .banline .bt i{position:absolute;left:-9px;right:-11px;bottom:3px;height:.56em;z-index:-1;background:var(--hlt);opacity:.85;font-style:normal;}
  .banline .bc{font-family:var(--hand);font-size:23px;color:var(--ink2);}
  .banline .bc b{font-family:var(--num);color:var(--pen);font-weight:700;}
  .cutline{margin:26px 64px 0;border:none;border-top:2.5px dashed #b9b09a;position:relative;}
  .cutline::before{content:'✂';position:absolute;left:-6px;top:-15px;font-size:21px;color:var(--mut);transform:rotate(-8deg);}
  .ticker{margin:12px 64px 0;font-family:var(--hand);font-size:17px;color:var(--mut);white-space:nowrap;overflow:hidden;}
  .ticker .tag{color:var(--pen);margin-right:10px;}
  .ticker i{font-style:normal;color:var(--pen);margin:0 11px;}
  /* ---- 编者按便利贴 ---- */
  .edwrap{padding:40px 64px 0;display:flex;justify-content:center;}
  .edpost{position:relative;max-width:76%;background:var(--postit);padding:30px 34px 32px;
    transform:rotate(-1.2deg);box-shadow:0 12px 22px -10px rgba(38,34,28,.35);}
  .edpost::after{content:'';position:absolute;right:0;bottom:0;border:15px solid transparent;
    border-right-color:var(--paper);border-bottom-color:var(--paper);}
  .edpost .pin{position:absolute;top:-12px;left:40px;width:22px;height:22px;border-radius:50%;
    background:radial-gradient(circle at 35% 30%,#ff8a76,var(--pen) 65%);}
  .edpost h5{font-family:var(--hand);font-size:23px;color:var(--pen);margin-bottom:10px;}
  .edpost p{font-family:var(--hand);font-size:22.5px;line-height:1.8;color:#4a431f;text-align:justify;}
  /* ---- 详读:撕边剪报卡列 ---- */
  .dgrid{padding:46px 56px 30px;display:flex;flex-direction:column;gap:44px;}
  .clipwrap{filter:drop-shadow(0 12px 12px rgba(38,34,28,.17));}
  .clip{position:relative;background:var(--card);padding:34px 34px 26px;
    clip-path:polygon(1% 2.5%,7% .5%,15% 2%,26% 0,37% 2.5%,50% .5%,62% 2.5%,73% 0,84% 2%,93% .5%,99.5% 3%,99% 14%,100% 27%,98.5% 40%,100% 53%,99% 66%,100% 79%,98.5% 90%,99.5% 97%,93% 99.5%,82% 98%,70% 100%,59% 98.5%,47% 100%,35% 98%,23% 100%,12% 98.5%,3% 100%,.5% 95%,1.5% 83%,0 71%,1.5% 58%,0 45%,1.5% 32%,.5% 19%,1.5% 8%);}
  .clip.t2{clip-path:polygon(.5% 4%,9% 1%,19% 3%,31% .5%,44% 2.5%,57% 0,69% 2%,80% .5%,90% 2.5%,99% 1%,99.5% 12%,98.5% 25%,100% 38%,99% 51%,100% 64%,98.5% 77%,100% 89%,98% 98%,90% 99.5%,79% 98%,67% 100%,55% 98%,43% 100%,31% 98.5%,19% 100%,9% 98%,1% 99.5%,0 88%,1.5% 76%,.5% 63%,1.5% 50%,0 37%,1.5% 24%,.5% 12%);}
  .tape1{position:absolute;top:-11px;left:38px;width:96px;height:26px;background:var(--tape);
    transform:rotate(-3deg);mix-blend-mode:multiply;}
  .tape2{position:absolute;top:-9px;right:34px;width:76px;height:24px;background:rgba(247,205,150,.55);
    transform:rotate(5deg);mix-blend-mode:multiply;}
  .cutfrom{font-family:var(--hand);font-size:18px;color:var(--mut);}
  .cutfrom i{font-style:normal;color:var(--pen);}
  /* 「追·第N天」红章(已剪记忆②):连续多天在追的事件,剪报上盖一枚歪斜红印 */
  .trkstamp{display:inline-block;margin-left:12px;font-family:var(--hand);font-size:16px;
    color:var(--pen);border:2px solid var(--pen);border-radius:6px;padding:1px 10px 2px;
    transform:rotate(-4deg);opacity:.82;white-space:nowrap;}
  .orow .otrk{font-family:var(--hand);font-size:15px;color:var(--pen);margin-left:10px;white-space:nowrap;}
  .clip h3{position:relative;font-weight:900;font-size:31px;line-height:1.42;margin:10px 0 12px;}
  .leadstar{top:-18px;left:-16px;width:48px;height:48px;z-index:3;}
  .leadline{left:0;right:0;bottom:-9px;height:10px;width:100%;}
  .rvmark{position:absolute;top:14px;right:18px;font-family:var(--hand);font-size:19px;color:var(--pen);
    transform:rotate(7deg);padding:6px 11px;z-index:2;}
  .rvmark svg{left:-4px;top:-4px;width:calc(100% + 8px);height:calc(100% + 8px);}
  .drepo{display:inline-block;font-family:var(--num);font-weight:700;font-size:18px;color:var(--ink);
    background:rgba(38,34,28,.05);border:1.5px dashed #c9c0a8;border-radius:5px;padding:5px 14px;margin:0 0 12px;}
  .drepo::before{content:'✂ ';color:var(--pen);}
  .clip .tldr{font-size:19px;line-height:1.78;text-align:justify;color:var(--ink);margin:0 0 16px;}
  .clip .tldr strong,.snote strong{font-weight:inherit;color:inherit;
    background:linear-gradient(180deg,transparent 42%,var(--hlt) 42% 88%,transparent 88%);padding:0 1px;}
  .snote{position:relative;margin:6px 2px 2px;padding:20px 22px 22px;background:var(--postit);
    transform:rotate(-.8deg);box-shadow:0 8px 14px -7px rgba(38,34,28,.3);
    font-family:var(--hand);font-size:21px;line-height:1.78;color:#4a431f;text-align:justify;}
  .clipwrap:nth-child(even) .snote{background:var(--postit2);color:#6b3a4c;transform:rotate(.9deg);}
  .snote::before{content:'';position:absolute;top:-9px;left:30px;width:66px;height:19px;
    background:var(--tape);transform:rotate(-2deg);}
  .snote .lbl{color:var(--pen);margin-right:8px;}
  .cmeta{display:flex;gap:14px;align-items:center;margin-top:16px;flex-wrap:wrap;
    font-family:var(--hand);font-size:16.5px;color:var(--mut);}
  /* ---- 速读 / 中详:剪报清单 ---- */
  .olist{padding:42px 64px 34px;}
  .orow{position:relative;display:flex;gap:22px;align-items:baseline;padding:24px 4px 22px;}
  .orow::after{content:'';position:absolute;left:0;right:0;bottom:0;height:6px;
    background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='6' viewBox='0 0 120 6'%3E%3Cpath d='M0,3 Q10,0.5 20,3 T40,3 T60,3 T80,3 T100,3 T120,3' fill='none' stroke='%23cabfa2' stroke-width='1.4'/%3E%3C/svg%3E") repeat-x;}
  .orow:last-child::after{display:none;}
  .orow .on{font-family:var(--hand);font-weight:400;font-size:30px;line-height:1;color:var(--pen);min-width:52px;}
  .orow h4{font-weight:900;font-size:24px;line-height:1.4;color:var(--ink);margin-bottom:7px;}
  .orow p{font-size:17px;line-height:1.7;color:var(--ink2);text-align:justify;}
  .orow p strong{font-weight:inherit;color:inherit;
    background:linear-gradient(180deg,transparent 42%,var(--hlt) 42% 88%,transparent 88%);padding:0 1px;}
  .orow .osrc{font-family:var(--hand);font-size:15px;color:var(--mut);margin-left:12px;white-space:nowrap;}
  .orow .orev{font-family:var(--hand);font-size:15px;color:var(--pen);margin-left:10px;white-space:nowrap;}
  .orow .orepo{display:inline-block;font-family:var(--num);font-weight:700;font-size:15.5px;color:var(--ink);
    background:rgba(38,34,28,.05);border:1.5px dashed #c9c0a8;border-radius:5px;padding:3px 11px;margin:7px 0 4px;}
  .orow .orepo::before{content:'✂ ';color:var(--pen);}
  .orow .oins{position:relative;margin-top:10px;padding:13px 16px 14px;background:var(--postit);
    font-family:var(--hand);font-size:17.5px;line-height:1.7;color:#4a431f;text-align:justify;
    box-shadow:0 5px 10px -6px rgba(38,34,28,.3);transform:rotate(-.5deg);}
  .orow .oins strong{font-weight:inherit;color:inherit;
    background:linear-gradient(180deg,transparent 42%,var(--hlt) 42% 88%,transparent 88%);padding:0 1px;}
  .orow .oins .lbl{color:var(--pen);margin-right:6px;}
  /* ---- 落款(只留事实) ---- */
  .foot{margin-top:28px;padding:26px 64px 40px;border-top:2.5px dashed #b9b09a;position:relative;
    display:flex;justify-content:space-between;align-items:baseline;gap:14px;}
  .foot::before{content:'✂';position:absolute;right:40px;top:-15px;font-size:21px;color:var(--mut);transform:rotate(172deg);}
  .foot b{font-family:var(--num);font-weight:700;font-size:24px;color:var(--ink);}
  .foot b .cn{font-family:var(--song);font-weight:900;}
  .foot span{font-family:var(--hand);font-size:16px;color:var(--ink2);}
"""

_HEAD = (
    """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>{{ title }}</title>
<style>""" + _STYLE + """</style></head><body>"""
)

# 报头:日期手写 + 红圈 Pulsewire日报 + 版名荧光 + ✂剪自 ticker。文案只留事实(无口号)。
_HERO = """
<div class="mast">
  <div class="tapecorner tl"></div><div class="tapecorner tr"></div>
  <div class="dateline">{{ date_display }}{% if mode %} · {{ mode }}{% endif %}
    <svg class="doodle" viewBox="0 0 220 10" preserveAspectRatio="none"><path d="M2,6 Q16,1 30,6 T58,6 T86,6 T114,6 T142,6 T170,6 T198,6 T218,6"/></svg>
  </div><br>
  <div class="np"><span class="en">Pulsewire</span><span class="cn">日报
    <svg class="doodle circ" viewBox="0 0 140 74"><path d="M12,40 C8,16 44,6 74,8 C108,10 134,20 130,40 C126,60 88,70 56,66 C26,62 10,54 13,36 C16,22 40,12 68,13"/></svg>
  </span></div>
  <div class="banline"><span class="bt">{{ category }}<i></i></span>
    <span class="bc"><b>{{ items | length }}</b> 张剪报</span></div>
</div>
<hr class="cutline">
<div class="ticker"><span class="tag">✂ 剪自</span>{% for s in sources %}{{ s | e }}{% if not loop.last %}<i>·</i>{% endif %}{% endfor %}</div>"""

_RV_SVG = (
    '<svg class="doodle" viewBox="0 0 90 40"><path d="M8,22 C6,10 28,4 48,5 '
    'C70,6 86,12 84,22 C82,32 60,37 40,35 C20,33 8,30 9,20"/></svg>'
)
_STAR_SVG = (
    '<svg class="doodle leadstar" viewBox="0 0 44 44"><path d="M22,4 L26.5,16 L40,16 '
    'L29.5,24.5 L33.5,38 L22,30 L10.5,38 L14.5,24.5 L4,16 L17.5,16 Z"/></svg>'
)
_WAVE_SVG = (
    '<svg class="doodle leadline" viewBox="0 0 300 10" preserveAspectRatio="none">'
    '<path d="M2,6 Q20,1 38,6 T74,6 T110,6 T146,6 T182,6 T218,6 T254,6 T296,6"/></svg>'
)

_FOOT = """
<footer class="foot"><b>Pulse<span class="cn">wire</span></b><span>{{ footer_info }}</span></footer>
<div class="grain"></div>
</body></html>"""

# 详读长图:每条一张撕边剪报,tldr 荧光划重点 + 批注便利贴(insight 全文)
_DETAIL_SRC = (
    _HEAD + _HERO + """
{% if digest %}
<div class="edwrap"><div class="edpost"><span class="pin"></span>
  <h5>编者按 ——</h5><p>{{ digest | highlight }}</p></div></div>
{% endif %}
<div class="dgrid">
{% for it in items %}
  <div class="clipwrap" style="transform:rotate({{ rots[loop.index0 % 8] }}deg)">
  <div class="clip{% if loop.index0 % 3 == 1 %} t2{% endif %}">
    <div class="tape1"></div>{% if loop.first %}<div class="tape2"></div>""" + _STAR_SVG + """{% endif %}
    {% if it.needs_review %}<span class="rvmark">待核实?""" + _RV_SVG + """</span>{% endif %}
    <div class="cutfrom">✂ 剪自 <i>{{ it.source | e }}</i>{% if it.tracking_days %}<span class="trkstamp">追 · 第{{ it.tracking_days }}天</span>{% endif %}</div>
    <h3>{{ it.headline | e }}{% if loop.first %}""" + _WAVE_SVG + """{% endif %}</h3>
    {% if it.repo %}<span class="drepo">{{ it.repo | e }}</span><br>{% endif %}
    {% if it.tldr %}<div class="tldr">{{ it.tldr | highlight }}</div>{% endif %}
    {% if it.insight %}<div class="snote"><span class="lbl">批注:</span>{{ it.insight | highlight }}</div>{% endif %}
  </div></div>
{% endfor %}
</div>""" + _FOOT
)

# 速读卡:剪报清单(headline + tldr 一句话)
_OVERVIEW_SRC = (
    _HEAD + _HERO + """
<div class="olist">
{% for it in items %}
  <div class="orow">
    <div class="on">{{ loop.index }}.</div>
    <div class="otext">
      <h4>{{ it.headline | e }}{% if it.tracking_days %}<span class="otrk">〔追·第{{ it.tracking_days }}天〕</span>{% endif %}{% if it.needs_review %}<span class="orev">〔待核实?〕</span>{% endif %}</h4>
      <p>{{ it.tldr | highlight }}<span class="osrc">✂ {{ it.source | e }}</span></p>
    </div>
  </div>
{% endfor %}
</div>""" + _FOOT
)

# 中详卡:速读骨架 + 每条精简批注便利贴(2~3 句)
_MIDVIEW_SRC = (
    _HEAD + _HERO + """
<div class="olist">
{% for it in items %}
  <div class="orow">
    <div class="on">{{ loop.index }}.</div>
    <div class="otext">
      <h4>{{ it.headline | e }}{% if it.tracking_days %}<span class="otrk">〔追·第{{ it.tracking_days }}天〕</span>{% endif %}{% if it.needs_review %}<span class="orev">〔待核实?〕</span>{% endif %}</h4>
      {% if it.repo %}<span class="orepo">{{ it.repo | e }}</span><br>{% endif %}
      {% if it.tldr %}<p>{{ it.tldr | highlight }}<span class="osrc">✂ {{ it.source | e }}</span></p>{% endif %}
      {% if it.insight %}<div class="oins"><span class="lbl">批注:</span>{{ it.insight | brief }}</div>{% endif %}
    </div>
  </div>
{% endfor %}
</div>""" + _FOOT
)


def _brief(text: str, max_sents: int = 3, max_chars: int = 165) -> str:
    """批注摘要:取前 max_sents 句(或约 max_chars 字)给中详卡;数字荧光复用 highlight。

    按整句保留(不在句中截断);先 escape 再对已对账数字划荧光,与 _highlight 同样安全。
    """
    from markupsafe import Markup, escape

    segs = re.split(r"(?<=[。!?！?])", (text or "").strip().replace("\n", " "))
    out, n = "", 0
    for seg in segs:
        seg = seg.strip()
        if not seg:
            continue
        if out and (n >= max_sents or len(out) + len(seg) > max_chars):
            break
        out += seg
        n += 1
    safe = str(escape(out))
    return Markup(_NUM_HIGHLIGHT_RE.sub(r"<strong>\1</strong>", safe))


def _highlight(text: str) -> str:
    """先 escape 再对带单位数字加 <strong>(数字已对账,荧光划出安全;样式层渲成荧光笔)。"""
    from markupsafe import Markup, escape

    safe = str(escape(text))
    return Markup(_NUM_HIGHLIGHT_RE.sub(r"<strong>\1</strong>", safe))


_ENV = Environment(autoescape=False)
_ENV.filters["highlight"] = _highlight
_ENV.filters["brief"] = _brief
_DETAIL_TEMPLATE = _ENV.from_string(_DETAIL_SRC)
_OVERVIEW_TEMPLATE = _ENV.from_string(_OVERVIEW_SRC)
_MIDVIEW_TEMPLATE = _ENV.from_string(_MIDVIEW_SRC)

_ROTS = [-1.2, 0.9, -0.7, 1.3, -1.0, 0.7, -1.4, 1.1]


def _sources(items: list[dict]) -> list[str]:
    """信源 ticker(去重保序)。"""
    out: list[str] = []
    for it in items:
        s = it.get("source") or ""
        if s and s not in out:
            out.append(s)
    return out


def build_html(
    *, title: str, date_display: str, category: str, digest: str,
    items: list[dict], footer_info: str, width: int,
) -> str:
    """详读长图:每条一张撕边剪报,headline + tldr + 批注便利贴(insight 全文)。"""
    return _DETAIL_TEMPLATE.render(
        title=title, date_display=date_display, category=category, digest=digest,
        mode="详读", items=items, sources=_sources(items), footer_info=footer_info,
        width=width, rots=_ROTS,
    )


def build_overview_html(
    *, title: str, date_display: str, category: str,
    items: list[dict], footer_info: str, width: int,
) -> str:
    """速读卡:剪报清单,给只想扫一眼的人。"""
    return _OVERVIEW_TEMPLATE.render(
        title=title, date_display=date_display, category=category, digest="",
        mode="速读", items=items, sources=_sources(items), footer_info=footer_info,
        width=width, rots=_ROTS,
    )


def build_midview_html(
    *, title: str, date_display: str, category: str,
    items: list[dict], footer_info: str, width: int,
) -> str:
    """中详卡:headline + tldr + 精简批注(2~3 句),比速读卡丰富、比详读长图短。"""
    return _MIDVIEW_TEMPLATE.render(
        title=title, date_display=date_display, category=category, digest="",
        mode="中详", items=items, sources=_sources(items), footer_info=footer_info,
        width=width, rots=_ROTS,
    )
