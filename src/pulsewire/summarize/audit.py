"""LLM 断言审计:高风险定性断言闸门的"治本层"。

关键词闸门(verify.RISKY_CLAIM_PATTERNS)是确定性兜底,但换个措辞就漏
("已向 SEC 递交 S-1 文件"没有"上市"二字)。这里让 LLM 用事实核查员视角
复审**单源且关键词闸门放行**的条目成稿,逮"把未经多源证实的重大断言写成
既成事实"的漏网之鱼 → 同样走 needs_review/待核实徽标链路。

边界(防自我背书 + 防拖垮主报):
- 审计用**独立的一次 LLM 调用**(不同提示词/视角),不让总结模型给自己打分。
- 审计只能把条目从 ok 拉到 needs_review,**绝不反向放行**——关键词闸门标的不动。
- 审计失败(重试耗尽)→ 告警 + 降级为只剩关键词闸门,不拖垮日报(次要质量层,
  同"次领域失败不拖垮主报"的口径);metrics 记 audit_failed,不静默。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger

from .backends import complete, parse_json

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_AUDIT_SYSTEM = (
    "你是一名严格的事实核查员,复审中文日报条目。这些条目**全部只有单一信源**,"
    "没有第二家媒体同报佐证。你的任务:找出『把未经证实的重大断言写成既成事实』的条目。\n"
    "重大断言指写错会严重误导读者的:公司上市/IPO/收购合并等资本市场传闻、"
    "融资金额或估值的夸大、性能/效果的营销话术(翻倍、碾压、行业第一)、"
    "医疗领域的治愈/突破宣称、战争伤亡/制裁/封锁等地缘强断言,以及同等级别的其它大话。\n"
    "判定标准:\n"
    "- 条目已用『据报道』『有消息称』『官方宣称』『单一来源称』等降调词的 → 不算,放过。\n"
    "- 常规单源报道(产品发布、普通融资新闻、研究进展)按行业媒体惯例平实陈述的 → 不算,放过。\n"
    "- 只有把传闻/营销/孤证强断言**直接当事实陈述**(无任何降调)才算 risky。\n"
    "宁缺毋滥:拿不准就放过,别把正常新闻刷成嫌疑。严格只输出 JSON。"
)


class AuditVerdict(BaseModel):
    item_id: str
    risky: bool = False
    claims: list[str] = Field(default_factory=list)  # 被判为"传闻当事实"的断言简述


class AuditOutput(BaseModel):
    items: list[AuditVerdict] = Field(default_factory=list)


def _build_audit_prompt(candidates: list[tuple[str, str, str, str]]) -> str:
    """candidates: [(item_id, headline, tldr, insight)],全部为单源条目。"""
    lines = [
        "请逐条复审以下日报条目(均只有单一信源)。对每条判定:是否存在"
        "『未经证实的重大断言被写成既成事实』。",
        "",
        "条目:",
    ]
    for idx, (item_id, headline, tldr, insight) in enumerate(candidates, start=1):
        lines.append(f"[{idx}] item_id={item_id}")
        lines.append(f"    headline: {headline}")
        lines.append(f"    tldr: {tldr}")
        lines.append(f"    insight: {insight}")
    lines.append("")
    lines.append(
        '输出 JSON:{"items":[{"item_id":"<上面的item_id>","risky":true/false,'
        '"claims":["<被当成事实的断言简述,risky=false 时为空>"]}]},为每个 item_id 各一条。'
    )
    return "\n".join(lines)


def audit_single_source_items(
    candidates: list[tuple[str, str, str, str]], settings: Settings
) -> dict[str, list[str]]:
    """LLM 复审单源条目成稿,返回 {item_id: [断言简述,...]}(只含被判 risky 的)。

    JSON 校验失败重试(沿用 summarize.json_schema_retry),耗尽冒泡——由调用方决定降级。
    模型编造的 item_id 一律丢弃(只认 candidates 里的)。
    """
    if not candidates:
        return {}
    user = _build_audit_prompt(candidates)
    valid_ids = {c[0] for c in candidates}
    last_err: Exception | None = None
    attempts = settings.summarize.json_schema_retry + 1
    for i in range(attempts):
        u = user if i == 0 else user + "\n\n上次输出不是合法 JSON 或缺字段,请严格按要求只输出 JSON。"
        try:
            raw = complete(_AUDIT_SYSTEM, u, settings, stage="audit")
            out = AuditOutput.model_validate(parse_json(raw))
            return {
                v.item_id: (v.claims or ["传闻当事实"])
                for v in out.items
                if v.risky and v.item_id in valid_ids
            }
        except PermanentLLMError:
            raise  # 没钱/凭证失效:立即熔断,别追加提示傻重试(2026-07-02 E1)
        except Exception as exc:
            last_err = exc
            log.warning("summarize.audit.retry", attempt=i, error=str(exc))
    raise RuntimeError(f"LLM 断言审计 JSON Schema 校验重试耗尽:{last_err}")
