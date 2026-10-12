"""模型 + 推理档位自动选择（主人令 2026-10-13）。

设计哲学：复杂问题用贵的模型、推理拉满；琐事用快模型、推理关到最低——
用户不做选择，Agent 按任务复杂度自己决定。手动选择永远优先：只有会话模型
是 ``auto`` 哨兵时本模块才接管，用户 ``/model <具体模型>`` 即刻退出自动模式。

四档复杂度（classify_complexity）：
  trivial  — 打招呼/确认/一句话问答（"好的""谢谢""在吗"）
  simple   — 单轮简单问题，无关键词，短文本
  standard — 默认档：普通开发/问答任务
  complex  — 多步骤/架构/重构/debug/长文/代码块/多个疑问

路由表（load_auto_policy）全部可配，config.yaml::

    auto_router:
      models:            # tier -> 模型 id（缺省回落到全局 model）
        trivial: ...
        simple: ...
        standard: ...
        complex: ...     # 不配则用 model（安全缺省：不擅自换贵模型）
      efforts:           # tier -> 推理档位（VALID_REASONING_EFFORTS）
        trivial: minimal
        simple: low
        standard: medium
        complex: high

任何异常都安全降级：分类或切换失败时保持当前模型继续跑，绝不让任务卡住。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUTO_MODEL_SENTINEL = "auto"

# 档位 -> 推理 effort 的出厂默认（config auto_router.efforts 可覆盖）
DEFAULT_EFFORTS: dict[str, str] = {
    "trivial": "minimal",
    "simple": "low",
    "standard": "medium",
    "complex": "high",
}

_TIERS = ("trivial", "simple", "standard", "complex")

# 复杂度提升关键词（中英混合）：出现即至少 standard，强信号直接 complex
_STRONG_SIGNALS = re.compile(
    r"架构|重构|设计一|实现一|完整|系统级|排查|调试|定位问题|根因|漏洞|安全|性能|优化|"
    r"规划|方案|步骤|流程|对比|评估|选型|迁移|升级|部署|架构图|原型|框架|引擎|算法|"
    r"报错|错误信息|异常|traceback|exception|error|"
    r"refactor|architect|debug|root cause|optimize|migrat|design|implement|vulnerab|"
    r"并发|分布式|微服务",
    re.IGNORECASE,
)
_MULTI_STEP = re.compile(r"先.{0,40}(然后|接着|再|最后)|第一步|第二步|step \d|phase \d", re.IGNORECASE)
_CODE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bSELECT\b.*\bFROM\b|traceback|stack trace", re.IGNORECASE)
_GREETING = re.compile(
    r"^(hi|hello|hey|yo|ok|okay|thanks|thank you|bye|你好|您好|好的?|收到|嗯+|谢谢|感谢|在吗|在么|"
    r"早上好|晚上好|午安|辛苦了|拜拜|再见|明白了?|懂了)[!。！~.\s]*$",
    re.IGNORECASE,
)


def is_auto_model(model: Any) -> bool:
    """True when the session model is the auto sentinel (case/space tolerant)."""
    return str(model or "").strip().lower() == AUTO_MODEL_SENTINEL


def extract_user_text(user_message: Any) -> str:
    """Latest user text from a turn payload: str, or a list of content blocks
    (``[{"type": "text", "text": ...}, {"type": "image_url", ...}]``)."""
    if user_message is None:
        return ""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, (list, tuple)):
        parts = [
            str(block.get("text") or "")
            for block in user_message
            if isinstance(block, dict) and str(block.get("type") or "") == "text"
        ]

        return "\n".join(p for p in parts if p)

    return str(user_message)


def classify_complexity(user_text: str) -> str:
    """Heuristic tier for the turn's user text. Pure, deterministic, cheap."""
    text = (user_text or "").strip()
    if not text:
        return "simple"

    # 纯打招呼/确认：最短路径，快模型 + 最低推理
    if _GREETING.match(text) and len(text) <= 24:
        return "trivial"

    questions = text.count("?") + text.count("？")
    length = len(text)

    if _CODE.search(text) or _STRONG_SIGNALS.search(text) or _MULTI_STEP.search(text):
        return "complex"
    if length >= 300 or questions >= 3:
        return "complex"
    if length >= 80 or questions >= 1:
        return "standard"

    return "simple"


def load_auto_policy(config: Optional[dict]) -> dict:
    """Resolve the routing table from config (``auto_router`` key).

    Missing tiers fall back to the global ``model`` — out of the box auto mode
    keeps today's default model and only moves the reasoning dial; operators
    opt into cheap/strong splits explicitly.
    """
    cfg = config if isinstance(config, dict) else {}
    router = cfg.get("auto_router") if isinstance(cfg.get("auto_router"), dict) else {}
    models_cfg = router.get("models") if isinstance(router.get("models"), dict) else {}
    efforts_cfg = router.get("efforts") if isinstance(router.get("efforts"), dict) else {}
    default_model = str(cfg.get("model") or "").strip()

    models: dict[str, str] = {}
    for tier in _TIERS:
        picked = str(models_cfg.get(tier) or "").strip() or default_model
        models[tier] = picked

    efforts: dict[str, str] = {}
    for tier in _TIERS:
        picked = str(efforts_cfg.get(tier) or "").strip().lower() or DEFAULT_EFFORTS[tier]
        efforts[tier] = picked

    return {"models": models, "efforts": efforts}


def resolve_auto_route(user_text: str, config: Optional[dict]) -> dict:
    """Classify the turn and resolve the (model, effort) pair. No side effects."""
    policy = load_auto_policy(config)
    tier = classify_complexity(user_text)

    return {
        "tier": tier,
        "model": policy["models"][tier],
        "effort": policy["efforts"][tier],
    }


def maybe_apply_auto_route(agent: Any, user_message: Any) -> Optional[dict]:
    """Turn-start hook: when the session runs the auto sentinel, classify the
    user's message and switch model + reasoning effort in place.

    Returns the applied route (``{"tier", "model", "effort", "switched"}``) or
    ``None`` when auto mode is off / nothing to do. Never raises: any failure
    logs and leaves the agent on its current model (the turn still runs).
    """
    if agent is None or not is_auto_model(getattr(agent, "model", None)):
        return None

    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
    except Exception:
        logger.debug("auto_router: config load failed; keeping current model", exc_info=True)

        return None

    route = resolve_auto_route(extract_user_text(user_message), config)
    target_model = route["model"]
    if not target_model:
        return None

    switched = False
    if str(getattr(agent, "model", "") or "").strip() != target_model:
        try:
            from agent.agent_runtime_helpers import switch_model

            switch_model(agent, target_model, getattr(agent, "provider", "") or "")
            switched = True
        except Exception:
            # 换模型失败绝不卡任务：保持当前模型继续这一轮。
            logger.warning(
                "auto_router: switch to %s failed; staying on %s",
                target_model, getattr(agent, "model", None), exc_info=True,
            )

    # 推理档位：auto 模式每轮按 tier 覆写（手动 /model --reasoning 会退出 auto，
    # 所以这里覆写不会跟用户的明确意图打架）。
    try:
        from hermes_constants import parse_reasoning_effort

        effort_cfg = parse_reasoning_effort(route["effort"])
        if effort_cfg is not None and getattr(agent, "reasoning_config", None) != effort_cfg:
            agent.reasoning_config = effort_cfg
    except Exception:
        logger.debug("auto_router: effort apply failed", exc_info=True)

    result = {"tier": route["tier"], "model": target_model, "effort": route["effort"], "switched": switched}
    logger.info(
        "auto_router: tier=%s model=%s effort=%s switched=%s",
        result["tier"], result["model"], result["effort"], result["switched"],
    )

    return result
