"""LLM client — any OpenAI-compatible chat-completions endpoint.

OpenRouter remains the default. Point ``LLM_BASE_URL`` at any compatible
gateway to use something else (DeepSeek, OmniRoute, 9router, vLLM,
llama.cpp, ...):

    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_MODEL=deepseek-chat
    LLM_API_KEY=sk-...

The base URL may be given with or without the ``/v1`` suffix and with or
without ``/chat/completions``; both are normalized.
"""

import asyncio

import httpx
from loguru import logger

from discord_recall.config import get_settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Statuses worth retrying: rate limits, transient upstream failures.
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class _TransientError(Exception):
    """A failure worth another attempt (rate limit, 5xx, empty completion)."""


def resolve_endpoint() -> tuple[str, str, str]:
    """Return ``(url, api_key, model)`` for the configured endpoint."""
    settings = get_settings()

    if settings.llm_base_url:
        base = settings.llm_base_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        api_key = settings.llm_api_key or settings.openrouter_api_key
        model = settings.llm_model or settings.openrouter_model
        if not api_key:
            raise SystemExit("LLM_API_KEY (or OPENROUTER_API_KEY) is not set in .env")
        return url, api_key, model

    if not settings.openrouter_api_key:
        raise SystemExit("LLM_API_KEY or OPENROUTER_API_KEY must be set in .env")
    return OPENROUTER_URL, settings.openrouter_api_key, settings.openrouter_model


def _extract_text(message: dict) -> str:
    """Pull text out of a chat-completions message, tolerating list content."""
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not content:
        # Some gateways expose only the reasoning trace for thinking models.
        content = message.get("reasoning_content") or ""
    return (content or "").strip()


async def complete(system: str, user_prompt: str, max_tokens: int | None = None) -> str:
    """Call the configured chat completions API, retrying transient failures."""
    url, api_key, model = resolve_endpoint()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens or 4096,
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code in RETRY_STATUSES:
                raise _TransientError(f"HTTP {response.status_code}: {response.text[:200]}")
            response.raise_for_status()

            data = response.json()
            choice = data["choices"][0]
            text = _extract_text(choice.get("message") or {})
            usage = data.get("usage") or {}
            logger.debug(
                f"LLM: {usage.get('prompt_tokens', '?')} in / "
                f"{usage.get('completion_tokens', '?')} out "
                f"({data.get('model') or model})"
            )
            if not text:
                raise _TransientError("empty completion content")
            return text
        except (httpx.TransportError, httpx.TimeoutException, _TransientError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"LLM call failed after {MAX_ATTEMPTS} attempts: {exc}"
                ) from exc
            delay = 2**attempt
            logger.warning(
                f"LLM call failed (attempt {attempt}/{MAX_ATTEMPTS}): {exc} "
                f"— retrying in {delay}s"
            )
            await asyncio.sleep(delay)
        except httpx.HTTPStatusError as exc:
            # Permanent upstream errors (401, 402, 404, 422, ...) fail fast.
            raise RuntimeError(
                f"LLM request rejected: HTTP {exc.response.status_code} "
                f"{exc.response.text[:200]}"
            ) from exc

    raise RuntimeError("unreachable")  # pragma: no cover
