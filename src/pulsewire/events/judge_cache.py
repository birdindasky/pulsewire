"""判决缓存(S1)哈希键 + 共享工具。

判官的裁决由两样决定:喂给 LLM 的**确切输入文本** + 判官的 **口径(system prompt + 影响
裁决的运行时参数:模型 / max_tokens / 票数)**。两者各哈希成 item_hash / prompt_hash;同
(item_hash, judge_name, prompt_hash) → 裁决可逐字复用(下一轮同条目、口径没改就读缓存不调 LLM)。
口径改(换模型 / 改 prompt / 改票数)→ prompt_hash 变 → 换 key 自然失效。

⚠️ **失效键铁律(2026-07-03 考官在 magnitude 上逮到的 bug 类)**:prompt_hash **必须**把
「换了它裁决就会变」的运行时参数全折进去——只哈希静态 _SYSTEM 会漏掉换模型(flash→pro 治抽风)、
改 max_tokens、改票数,导致拿旧模型的旧裁决当新读。所有判官一律走 `prompt_hash_of` 这一个函数
构造失效键,杜绝各判官手搓时再犯同一个漏。
"""

from __future__ import annotations

import hashlib

# 字段分隔符:落在正常文本里概率为 0 的控制符,避免 "a"+"b" 与 "ab"+"" 撞哈希。
_SEP = "\x1f"


def hash_input(text: str) -> str:
    """判官输入文本的内容哈希(item_hash,64 hex)。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def hash_prompt(prompt: str) -> str:
    """口径材料哈希(prompt_hash,16 hex)= 失效键。一般经 `prompt_hash_of` 间接调用。"""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16]


def prompt_hash_of(
    system: str, *, model: str, max_tokens: int, votes: int = 1, extra: str = ""
) -> str:
    """判官失效键 = system prompt + **影响裁决的运行时口径**(模型 / max_tokens / 票数 / 额外)。

    所有判官统一走本函数(唯一事实源),换任一口径 → 键变 → 旧裁决自然失效。
    ``extra``:个别判官有额外影响裁决的常量(如同事件判官的候选/截断口径),折进来。
    """
    material = _SEP.join([system, str(model), str(max_tokens), str(votes), extra])
    return hash_prompt(material)


def make_row(item_hash: str, judge_name: str, prompt_hash: str, verdict: dict) -> dict:
    """构造一行待写回的判决缓存记录(与 repo.upsert_judgments / tables.Judgment 对齐)。"""
    return {
        "item_hash": item_hash,
        "judge_name": judge_name,
        "prompt_hash": prompt_hash,
        "verdict": verdict,
    }
