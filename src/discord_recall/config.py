import stat
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file() -> str | None:
    """Find a readable env file. Prefers .env.local, falls back to .env if it's a regular file."""
    for name in (".env.local", ".env"):
        p = Path(name)
        if p.exists() and stat.S_ISREG(p.stat().st_mode):
            return name
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_env_file(), env_file_encoding="utf-8")

    discord_token: str = ""
    database_url: str = "sqlite+aiosqlite:///discord_recall.db"
    # Generic OpenAI-compatible LLM endpoint. Set llm_base_url to target any
    # compatible gateway (DeepSeek, OmniRoute, 9router, vLLM, llama.cpp, ...).
    # When empty, the OpenRouter settings below are used unchanged.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-3.1-flash-lite"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    server_ids: list[int] = []
    # Comma-separated guild ids. When set, the app refuses to touch anything
    # outside this list: no discovery, no capture, no digests. Empty = allow all.
    guild_whitelist: str = ""
    log_level: str = "INFO"
    backfill_batch_size: int = 100
    backfill_delay_seconds: float = 1.0


def whitelisted_guilds(settings: Settings | None = None) -> set[int]:
    """Parse GUILD_WHITELIST (CSV or JSON) into a set of guild ids."""
    raw = (settings or get_settings()).guild_whitelist.strip()
    if not raw:
        return set()
    if raw.startswith("["):
        raw = raw.strip("[]")
    out: set[int] = set()
    for part in raw.replace('"', "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
