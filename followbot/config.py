"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from dataclasses import dataclass, field, replace

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
    # Warm-up: an account's daily cap on its first day in the bot, doubling each day up to
    # daily_limit. 0 turns it off. X locked brand-new accounts after ~35 follows in their first hour.
    warmup_start: dict[str, int] = field(default_factory=lambda: {"x": 0, "instagram": 0})
    # Per-platform pace overrides (window_limit, window_seconds, gap_min, gap_max) on top of the above.
    # load_settings gives X 10 per 6 min with 25-40 s gaps (WINDOW_LIMIT_X etc.).
    pace: dict[str, dict[str, float]] = field(default_factory=dict)

    def for_platform(self, platform: str) -> "Limits":
        """These limits with that platform's pace overrides applied."""
        extra = self.pace.get(platform)
        if not extra:
            return self
        return replace(self, window_limit=int(extra.get("window_limit", self.window_limit)),
                       window_seconds=int(extra.get("window_seconds", self.window_seconds)),
                       gap_min=float(extra.get("gap_min", self.gap_min)),
                       gap_max=float(extra.get("gap_max", self.gap_max)))


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
    # VPN location -> country code (from railway/vpn-locations.txt). An account only moves between
    # locations in the same country: a sudden jump abroad is what makes X and Instagram ask "is this you?".
    location_country: dict[str, str] = field(default_factory=dict)
    # At most this many accounts share one VPN location (one exit IP).
    max_per_ip: int = 3

    def user_agent_for(self, platform: str) -> str:
        return self.user_agents.get(platform) or self.user_agent


LOCATIONS_FILE = Path(__file__).resolve().parent.parent / "railway" / "vpn-locations.txt"
FIRST_PORT = 8881


def load_locations(path: str | Path) -> list[tuple[str, str]]:
    """(name, country) per line of vpn-locations.txt, in port order (line 1 is port 8881)."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if parts:
            if len(parts) < 4:
                raise ConfigError(f"{path}: a line must be 'name country server_ip public_key', got {line!r}")
            out.append((parts[0], parts[1].upper()))
    return out


_RANGE = re.compile(r"^([A-Za-z_]*?)(\d+)-\1(\d+)$")
_PORT = re.compile(r"^(.*:)(\d+)(/?)$")


def _parse_proxies(raw: str) -> dict[str, str]:
    """`us=http://vpn-us:8888,de=http://vpn-de:8888` -> {"us": ..., "de": ...}.

    A range `ash6-ash20=http://host:8886` stands for ash6 on port 8886, ash7 on 8887, … ash20 on 8900
    (one tunnel per port in the vpn service).
    """
    proxies: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, url = part.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ConfigError(f"PROXIES entry must look like name=url, got {part!r}")
        span = _RANGE.match(name)
        if span:
            prefix, first, last = span.group(1), int(span.group(2)), int(span.group(3))
            port = _PORT.match(url)
            if last < first or not port:
                raise ConfigError(f"PROXIES range must look like ash6-ash20=http://host:8886, got {part!r}")
            for i in range(first, last + 1):
                proxies[f"{prefix}{i}"] = f"{port.group(1)}{int(port.group(2)) + i - first}{port.group(3)}"
        else:
            proxies[name] = url
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
        warmup_start={
            "x": _int(env, "WARMUP_START_X", 0),
            "instagram": _int(env, "WARMUP_START_INSTAGRAM", 0),
        },
    )
    limits = replace(limits, pace={"x": {
        "window_limit": _int(env, "WINDOW_LIMIT_X", 10),
        "window_seconds": _int(env, "WINDOW_SECONDS_X", 360),
        "gap_min": _float(env, "GAP_MIN_SECONDS_X", 25),
        "gap_max": _float(env, "GAP_MAX_SECONDS_X", 40),
    }})
    for platform in ("x", "instagram"):
        own = limits.for_platform(platform)
        if own.gap_min > own.gap_max:
            raise ConfigError("GAP_MIN_SECONDS must not exceed GAP_MAX_SECONDS (also the _X ones)")

    # Every location in vpn-locations.txt, when VPN_PROXY_BASE says how to reach the vpn service
    # (e.g. http://bot:PASS@vpn.railway.internal; location N is on port 8880+N). PROXIES and
    # PROXIES_EXTRA can still add or override single locations.
    locations = load_locations(env.get("VPN_LOCATIONS_FILE") or LOCATIONS_FILE)
    base = env.get("VPN_PROXY_BASE", "").strip().rstrip("/")
    proxies = {name: f"{base}:{FIRST_PORT + i}" for i, (name, _) in enumerate(locations)} if base else {}
    proxies.update(_parse_proxies(env.get("PROXIES", "")))
    proxies.update(_parse_proxies(env.get("PROXIES_EXTRA", "")))
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
        location_country=dict(locations),
        max_per_ip=_int(env, "MAX_ACCOUNTS_PER_IP", 3),
    )
