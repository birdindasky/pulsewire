"""飞书「今日剪报」卡(前端方向 A v2):折叠壳 + 每版内嵌自渲剪报图 + 图下原文链接。

架构(2026-07-04 用户拍板):
- 壳 = 折叠卡:一天一条消息,各版收进各自的 collapsible_panel(不刷屏、不用划长图到底);
- 肉 = 每版内嵌一张**自家渲染的剪报图**(render/templates.py 剪报涂鸦风 PNG,像素全控,
  撕边/胶带/手写批注/荧光全保留;飞书卡片字体改不了,图不受限);
- 手 = 图不能点,图下列**原文链接**逐条直达。
无图的版(渲染失败/图不新鲜)自动退回文字条目列表——绝不因图缺失漏内容。
文案铁律(2026-07-04 用户):不写广告语/口号,版块不编号(平权),只留事实。
开关 settings.feishu_card_enabled(默认关);开则 feishu_app 发这张卡替代逐张长图,发卡失败回退长图。
数字回源铁律不变:needs_review 打「待核实」,不静默当真。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import DeliverPayload

_MAX_ITEMS_PER_BOARD = 12   # 文字回退模式下单版最多列多少条(控卡片长度)
_MAX_LINKS_PER_BOARD = 15   # 图模式下原文链接列表上限
_HEADLINE_TRUNC = 26        # 链接行标题截断


def _esc_md(s: str) -> str:
    """卡片 markdown 转义:去掉会破坏排版/注入链接语法的字符(标题/正文来自 LLM,保守清洗)。"""
    return (s or "").replace("\\", "＼").replace("[", "〔").replace("]", "〕").replace("\n", " ").strip()


def _item_md(idx: int, it: dict, *, gh: bool = False) -> str:
    """文字回退模式一条:序号 + 标题(+待核实)+ 一句话 + 来源(+★)+ 原文链接。"""
    review = " `待核实`" if it.get("needs_review") else ""
    star = ""
    if gh and it.get("stars") is not None:
        try:
            star = f" · ★{int(it['stars']):,}"
        except (TypeError, ValueError):
            star = ""
    head = f"**{idx:02d}. {_esc_md(it.get('headline', ''))}**{review}"
    tldr = _esc_md(it.get("tldr", ""))
    src = _esc_md(it.get("source", "") or "—")
    url = (it.get("url") or "").strip()
    link = f" · [原文]({url})" if url.startswith(("http://", "https://")) else ""
    return f"{head}\n{tldr}\n<font color='grey'>✂ {src}{star}</font>{link}"


def _links_md(items: list[dict], *, gh: bool = False) -> str:
    """图模式:图下的原文链接列表(逐条可点)。无链接的条目跳过。"""
    lines: list[str] = []
    for i, it in enumerate(items[:_MAX_LINKS_PER_BOARD], 1):
        url = (it.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        h = _esc_md(it.get("headline", ""))
        if len(h) > _HEADLINE_TRUNC:
            h = h[:_HEADLINE_TRUNC] + "…"
        review = " `待核实`" if it.get("needs_review") else ""
        # 「追·第N天」章(已剪记忆②):连续多天在追的事件,链接行带小标(与 PNG/webapp 口径一致)
        track = f" `追·第{it['tracking_days']}天`" if it.get("tracking_days") else ""
        star = ""
        if gh and it.get("stars") is not None:
            try:
                star = f" ★{int(it['stars']):,}"
            except (TypeError, ValueError):
                star = ""
        lines.append(f"**{i:02d}** [{h}]({url}){star}{track}{review}")
    return "\n".join(lines)


def _panel(label: str, digest: str, items: list[dict], *,
           expanded: bool, gh: bool, image_key: str | None) -> dict:
    """一个版 = 一个 collapsible_panel。有剪报图 → 图 + 原文链接;无图 → 文字条目回退。"""
    elements: list[dict] = []
    if image_key:
        elements.append({
            "tag": "img", "img_key": image_key, "preview": True,
            "alt": {"tag": "plain_text", "content": f"{label} 剪报图"},
        })
        links = _links_md(items, gh=gh)
        if links:
            elements.append({"tag": "markdown", "content": f"<font color='grey'>原文直达</font>\n{links}"})
    else:
        if digest:
            elements.append({"tag": "markdown", "content": f"<font color='grey'>{_esc_md(digest)}</font>"})
        for i, it in enumerate(items[:_MAX_ITEMS_PER_BOARD], 1):
            elements.append({"tag": "markdown", "content": _item_md(i, it, gh=gh)})
        if len(items) > _MAX_ITEMS_PER_BOARD:
            elements.append({"tag": "markdown",
                             "content": f"<font color='grey'>…另有 {len(items) - _MAX_ITEMS_PER_BOARD} 条(见网页版)</font>"})
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": f"**{_esc_md(label)}** · {len(items)} 张"},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                     "color": "grey", "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": elements or [{"tag": "markdown", "content": "<font color='grey'>本版今日从缺,不硬凑</font>"}],
    }


def _boards_from_payload(payload: "DeliverPayload") -> list[dict]:
    """从 payload 取各版:domains(新闻)+ github;domains 空则回退主领域单版。

    每版:{key,label,digest,items,gh,image_path?}(image_path 由 deliver/engine 附上,新鲜才有)。
    """
    boards: list[dict] = []
    if payload.domains:
        for d in payload.domains:
            boards.append({"key": d.get("key", ""), "label": d.get("label", ""),
                           "digest": d.get("digest", ""), "items": d.get("items", []),
                           "gh": False, "image_path": d.get("image_path")})
    elif payload.items:
        boards.append({"key": "main", "label": payload.title, "digest": payload.digest,
                       "items": payload.items, "gh": False, "image_path": payload.image_path})
    if payload.github:
        boards.append({"key": "github", "label": "开源摘星", "gh": True,
                       "digest": "今日 GitHub 上升最快的 AI 相关开源项目。",
                       "items": payload.github, "image_path": payload.github_image_path})
    return boards


def build_digest_card(payload: "DeliverPayload", *, image_keys: dict[str, str] | None = None) -> dict:
    """构造飞书卡(schema 2.0):header + 各版头条导读 + 编辑概览 + 各版折叠面板(图 or 文字)。

    ``image_keys``:{版 key: 已上传的飞书 image_key}(feishu_app 上传后传入);缺谁谁走文字回退。
    首版默认展开(点开即见头条),其余折叠。文案只留事实。
    """
    image_keys = image_keys or {}
    boards = _boards_from_payload(payload)
    total = sum(len(b["items"]) for b in boards)

    elements: list[dict] = []
    # 各版头条导读(收起状态也能一眼扫全)
    daodu_lines = []
    for b in boards:
        h = _esc_md(b["items"][0].get("headline", "")) if b["items"] else "(本版今日从缺)"
        if len(h) > 22:
            h = h[:22] + "…"
        daodu_lines.append(f"**{_esc_md(b['label'])}** {h}")
    if daodu_lines:
        elements.append({"tag": "markdown", "content": "📌 **各版头条**\n" + "\n".join(daodu_lines)})
    if payload.digest:
        elements.append({"tag": "markdown", "content": f"**编辑概览**\n{_esc_md(payload.digest)}"})
    elements.append({"tag": "hr"})
    for bi, b in enumerate(boards):
        elements.append(_panel(b["label"], b.get("digest", ""), b["items"],
                               expanded=(bi == 0), gh=b["gh"],
                               image_key=image_keys.get(b["key"])))
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown",
                     "content": "<font color='grey'>数字均对照原文核验 · 拿不准的已标「待核实」</font>"})

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Pulsewire · 今日剪报"},
            "subtitle": {"tag": "plain_text",
                         "content": f"{payload.date_str} · {total} 张 · 每日 15:00 更新"},
            "template": "yellow",
        },
        "body": {"elements": elements},
    }
