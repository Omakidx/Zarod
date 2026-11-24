"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

PLATFORMS = ("x", "instagram")
PLATFORM_NAMES = {"x": "X", "instagram": "Instagram"}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    window_limit: int = 13
    window_seconds: int = 600
    daily_limit: dict[str, int] = field(default_factory=lambda: {"x": 200, "instagram": 200})
    gap_min: float = 35.0
    gap_max: float = 65.0
    max_attempts: int = 3


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    encryption_key: bytes
    db_path: str
    proxies: dict[str, str]
    require_proxy: bool
    user_agent: str
    limits: Limits
    # platform -> path of a cookie export imported at startup
    cookie_files: dict[str, str] = field(default_factory=dict)
    # VPN location used for imported accounts
    import_location: str | None = None
    # platform -> user agent, when cookies for that platform come from a different browser
    user_agents: dict[str, str] = field(default_factory=dict)

    def user_agent_for(self, platform: str) -> str:
        return self.user_agents.get(platform) or self.user_agent


def _parse_proxies(raw: str) -> dict[str, str]:
    """`us=http://vpn-us:8888,de=http://vpn-de:8888` -> {"us": ..., "de": ...}."""
    proxies: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, url = part.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise ConfigError(f"PROXIES entry must look like name=url, got {part!r}")
        proxies[name.strip()] = url.strip()
    return proxies


def _int(env: dict[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float(env: dict[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, default))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)

    missing = [n for n in ("TELEGRAM_BOT_TOKEN", "OWNER_TELEGRAM_ID", "ENCRYPTION_KEY") if not env.get(n)]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

    try:
        key = base64.urlsafe_b64decode(env["ENCRYPTION_KEY"])
    except ValueError as exc:
        raise ConfigError("ENCRYPTION_KEY must be base64 (generate one with `python -m followbot keygen`)") from exc
    if len(key) != 32:
        raise ConfigError("ENCRYPTION_KEY must decode to 32 bytes (generate one with `python -m followbot keygen`)")

    limits = Limits(
        window_limit=_int(env, "WINDOW_LIMIT", 13),
        window_seconds=_int(env, "WINDOW_SECONDS", 600),
        daily_limit={
            "x": _int(env, "DAILY_LIMIT_X", 200),
            "instagram": _int(env, "DAILY_LIMIT_INSTAGRAM", 200),
        },
        gap_min=_float(env, "GAP_MIN_SECONDS", 35),
        gap_max=_float(env, "GAP_MAX_SECONDS", 65),
    )
    if limits.gap_min > limits.gap_max:
        raise ConfigError("GAP_MIN_SECONDS must not exceed GAP_MAX_SECONDS")

    proxies = _parse_proxies(env.get("PROXIES", ""))
    require_proxy = env.get("REQUIRE_PROXY", "true").strip().lower() not in ("0", "false", "no")
    if require_proxy and not proxies:
        raise ConfigError("REQUIRE_PROXY is on but PROXIES is empty")

    secrets_dir = env.get("SECRETS_DIR", "/secrets")
    cookie_files = {}
    for platform, file_var in (("x", "X_COOKIES_FILE"), ("instagram", "INSTAGRAM_COOKIES_FILE")):
        name = env.get(file_var, "").strip()
        if name:
            cookie_files[platform] = os.path.join(secrets_dir, name)
    # Empty: each imported account gets a location of its own, like accounts added in Telegram.
    import_location = env.get("COOKIES_LOCATION", "").strip() or None
    if cookie_files and import_location and import_location not in proxies:
        raise ConfigError(f"COOKIES_LOCATION {import_location!r} is not in PROXIES")

    user_agents = {
        platform: ua
        for platform, ua_var in (("x", "X_USER_AGENT"), ("instagram", "INSTAGRAM_USER_AGENT"))
        if (ua := env.get(ua_var, "").strip())
    }

    return Settings(
        bot_token=env["TELEGRAM_BOT_TOKEN"],
        owner_id=_int(env, "OWNER_TELEGRAM_ID", 0),
        encryption_key=key,
        db_path=env.get("DB_PATH", "/data/followbot.db"),
        proxies=proxies,
        require_proxy=require_proxy,
        user_agent=env.get("BROWSER_USER_AGENT") or DEFAULT_USER_AGENT,
        limits=limits,
        cookie_files=cookie_files,
        import_location=import_location,
        user_agents=user_agents,
    )
