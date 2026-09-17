"""User-facing copy for gateway refusals and session-level failures.

One place for the sentences the TUI, Desktop and dashboard print verbatim, so the
"what happened / what to do" shape stays consistent and the slash commands cited
(`/model`, `/new`, `/sessions`, `/retry`) exist on EVERY client this gateway serves (TUI,
Desktop, dashboard) — no client-only forms (`/sessions new`, `/setup`) and no single-surface
gestures stated as fact (Ctrl+C is copy on Desktop). Lead phrases that clients pattern-match
on (``Session busy``) are part of the wire contract — keep them.
"""

from __future__ import annotations

from typing import Any

from agent.i18n import t

# Provider-layer failure codes → (title, hint). Codes are ``agent.error_classifier.FailoverReason``
# values carried in ``error_surface.code``; anything unlisted falls back on the layer table.
_TURN_ERROR_CODE_COPY: dict[str, tuple[str, str]] = {
    "auth": ("The model provider rejected the API key", "Fix the key with /model, then /retry."),
    "auth_permanent": ("The model provider rejected the API key", "Fix the key with /model, then /retry."),
    "billing": ("The model provider reports no credit left", "Top up the account or switch with /model."),
    "billing_unverified": ("The model provider reports no credit left", "Top up the account or switch with /model."),
    "rate_limit": ("The model provider is rate-limiting requests", "Wait a moment, then /retry."),
    "upstream_rate_limit": ("The model provider is rate-limiting requests", "Wait a moment, then /retry."),
    "upstream_blocked": ("A firewall/CDN in front of the model provider blocked the request", "Set a User-Agent via the provider's extra_headers, or switch with /model."),
    "overloaded": ("The model provider is overloaded", "Wait a moment, then /retry."),
    "server_error": ("The model provider had an internal error", "Wait a moment, then /retry."),
    "timeout": ("The model provider did not answer in time", "Try /retry; if it keeps happening, switch with /model."),
    "context_overflow": ("The conversation is too long for this model", "Run /compress, then /retry."),
    "payload_too_large": ("The request was too large for this model", "Run /compress, then /retry."),
    "model_not_found": ("The model provider does not know this model", "Pick another model with /model."),
    "content_policy_blocked": ("The model provider refused this request (content policy)", "Rephrase and send again."),
    "provider_policy_blocked": ("The model provider refused this request (account policy)", "Switch with /model."),
    "format_error": ("The model provider rejected the request format", "Try /retry; if it persists, switch with /model."),
    "ssl_cert_verification": ("The connection to the model provider could not be verified (TLS)",
                              "Check the endpoint's certificate, then /retry."),
}


_DETAIl_LIMIT = 400


def _identity(surface: dict | None) -> str:
    provider = str((surface or {}).get("provider") or "").strip()
    return f" ({provider})" if provider else ""


def turn_error_title(surface: dict | None) -> str:
    """Plain title for a failed turn, picked from ``error_surface`` code then layer."""
    surface = surface if isinstance(surface, dict) else {}
    code = str(surface.get("code") or "")
    layer = str(surface.get("layer") or "")
    if code:
        title = t(f"turn_errors.code.{code}.title")
    elif layer:
        title = t(f"turn_errors.layer.{layer}.title")
    else:
        title = t("turn_errors.default.title")
    return f"{title}{_identity(surface)}"


def turn_error_hint(surface: dict | None, recoverable: bool = True) -> str:
    surface = surface if isinstance(surface, dict) else {}
    code = str(surface.get("code") or "")
    layer = str(surface.get("layer") or "")
    if code:
        hint = t(f"turn_errors.code.{code}.hint")
    elif layer:
        hint = t(f"turn_errors.layer.{layer}.hint")
    else:
        hint = t("turn_errors.default.hint")
    if not recoverable:
        # Replace retry hints with model-switch hints in both languages
        hint = hint.replace("Send /retry", "Pick another model with /model").replace(
            "发送 /retry 重试", "请用 /model 选择其他模型"
        ).replace("尝试 /retry", "请用 /model 选择其他模型").replace(
            "稍等片刻，然后 /retry 重试", "请用 /model 选择其他模型"
        ).replace("尝试 /retry；若持续出现，请用 /model 切换。", "请用 /model 选择其他模型。")
    return hint


def turn_error_text(error: Any, surface: dict | None = None, *, recoverable: bool = True) -> str:
    """The assistant-slot text for a turn that produced no reply: title, what it means for the
    user, the raw provider detail on its own dimmed-able line, and the next step."""
    detail = " ".join(str(error or "").split())
    if len(detail) > _DETAIl_LIMIT:
        detail = detail[:_DETAIl_LIMIT - 1] + "…"
    lines = [f"{turn_error_title(surface)}. {t('turn_errors.message_not_answered')}"]
    if detail:
        lines.append(f"{t('turn_errors.details_prefix')}: {detail}")
    lines.append(turn_error_hint(surface, recoverable))
    return "\n".join(lines)


def busy_message(command: str) -> str:
    """4009 refusal for a history-mutating command while a reply is streaming. There is no
    ``/interrupt`` slash command on any client: Desktop has a Stop button, the terminal TUI uses
    Ctrl+C — name both without assuming which one the reader has."""
    return t("turn_errors.busy_message", command=command.lstrip("/"))


def agent_init_failed_message(exc: Any) -> str:
    return t("turn_errors.agent_init_failed", exc=str(exc))


AGENT_STILL_STARTING = t("turn_errors.agent_still_starting")

# A deferred build that finished WITHOUT attaching an agent (its session record was replaced or
# closed while it ran) leaves ``agent_ready`` set and ``agent`` None; this is the recorded cause.
AGENT_BUILD_ABANDONED = t("turn_errors.agent_build_abandoned")
# Turn refusal when the record still has no agent at admission time (reason unknown).
AGENT_MISSING_FOR_TURN = t("turn_errors.agent_missing_for_turn")


def resume_failed_message(exc: Any) -> str:
    return t("turn_errors.resume_failed", exc=str(exc))