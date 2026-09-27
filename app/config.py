"""Runtime settings, read once from the environment."""

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

    @property
    def database_path(self) -> Path:
        return self.data_dir / "plumb.db"

    @property
    def signing_key_path(self) -> Path:
        return self.data_dir / "signing-key.pem"


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
    )
