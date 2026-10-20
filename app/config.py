"""Runtime settings, read once from the environment."""

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    demo: bool
    fixtures_path: Path
    base_url: str
    secure_cookies: bool
    admin_email: str | None
    admin_password: str | None
    webhooks: bool = True
    rate_limits: str = ""

    @property
    def database_path(self) -> Path:
        return self.data_dir / "plumb.db"

    @property
    def signing_key_path(self) -> Path:
        return self.data_dir / "signing-key.pem"


def forwarded_problem(value: str | None, demo: bool) -> str | None:
    """Why FORWARDED_ALLOW_IPS (read by uvicorn, not by Plumb) would quietly
    break the rate limits, or None. uvicorn accepts addresses and networks
    (172.16.0.0/12) but turns anything malformed into a literal that never
    matches, so a typo makes every visitor look like the proxy: one address,
    and the per-address limits lock out the whole venue. "*" lets any client
    name its own address and walk around those limits."""
    if not value:
        return None
    for part in (p.strip() for p in value.split(",")):
        if not part:
            continue
        if part == "*":
            if demo:
                continue
            return ('FORWARDED_ALLOW_IPS="*" lets any client choose its own address and walk around the rate '
                    "limits; set it to your reverse proxy's address or network")
        try:
            ipaddress.ip_network(part) if "/" in part else ipaddress.ip_address(part)
        except ValueError:
            return (f"FORWARDED_ALLOW_IPS: {part!r} is not an address or a network (like 127.0.0.1 or "
                    "172.16.0.0/12); uvicorn would ignore it silently")
    return None


def boot_problems(settings: Settings) -> list[str]:
    """Settings a real (non-demo) instance must not start with. Each is a
    contradiction that fails quietly in use, so refusing at boot is kinder
    than finding out at the event."""
    if settings.demo:
        return []
    problems = []
    scheme = settings.base_url.split("://", 1)[0].lower() if settings.base_url else ""
    if scheme == "https" and not settings.secure_cookies:
        problems.append("PLUMB_BASE_URL is https but PLUMB_SECURE_COOKIES is off, so session cookies would also be "
                        "sent over plain http; set PLUMB_SECURE_COOKIES=1")
    if scheme == "http" and settings.secure_cookies:
        problems.append("PLUMB_SECURE_COOKIES is on but PLUMB_BASE_URL is http, so browsers would never send the "
                        "session cookie back and nobody could stay logged in; serve over https or turn it off")
    if settings.base_url and scheme not in ("http", "https"):
        problems.append(f"PLUMB_BASE_URL must start with http:// or https://, not {settings.base_url!r}")
    return problems


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    return Settings(
        data_dir=Path(os.environ.get("PLUMB_DATA_DIR", root / "data")),
        demo=_flag("PLUMB_DEMO", False),
        fixtures_path=Path(os.environ.get("PLUMB_FIXTURES", root / "fixtures.json")),
        base_url=(os.environ.get("PLUMB_BASE_URL") or "").rstrip("/"),
        secure_cookies=_flag("PLUMB_SECURE_COOKIES", False),
        admin_email=os.environ.get("PLUMB_ADMIN_EMAIL") or None,
        admin_password=os.environ.get("PLUMB_ADMIN_PASSWORD") or None,
        webhooks=_flag("PLUMB_WEBHOOKS", True),
        rate_limits=os.environ.get("PLUMB_RATE_LIMITS", ""),
    )
