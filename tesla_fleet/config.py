"""Settings, endpoints and price constants.

Secrets never live in the repository. Everything private is read from the
private folder (default ``~/.tesla-fleet``; override with TESLA_FLEET_HOME).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

AUTH_BASE = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3"
AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
API_BASES = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
}
REDIRECT_URI = "http://localhost:8080/callback"

# Read-only scopes. offline_access is what makes Tesla issue a refresh token.
USER_SCOPES = "openid offline_access vehicle_device_data vehicle_location vehicle_charging_cmds"
PARTNER_SCOPES = "openid vehicle_device_data vehicle_charging_cmds"

# Spend estimates only (USD). Check Tesla's current pricing page; these are
# values, not logic, so adjust them here if Tesla changes them.
COST_PER_DATA_REQUEST = 0.002  # $1 per 500 data requests
COST_PER_WAKE = 0.02  # $1 per 50 wakes
MONTHLY_CREDIT_USD = 10.0


class ConfigError(Exception):
    """Raised when required local configuration is missing."""


def private_home() -> Path:
    return Path(os.environ.get("TESLA_FLEET_HOME", Path.home() / ".tesla-fleet")).expanduser()


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    domain: str
    region: str
    home: Path

    @property
    def api_base(self) -> str:
        try:
            return API_BASES[self.region]
        except KeyError:
            raise ConfigError(f"Unknown TESLA_REGION '{self.region}'. Use one of: {', '.join(API_BASES)}.")

    @property
    def token_path(self) -> Path:
        return self.home / "tokens.json"

    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.json"

    @property
    def lock_path(self) -> Path:
        return self.home / ".lock"


def load_settings(home: Path | None = None) -> Settings:
    home = home or private_home()
    file_values = load_env_file(home / ".env")

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_values.get(key) or default

    client_id = get("TESLA_CLIENT_ID")
    if not client_id:
        raise ConfigError(f"TESLA_CLIENT_ID is not set. Add it to {home / '.env'}.")
    return Settings(
        client_id=client_id,
        client_secret=get("TESLA_CLIENT_SECRET"),
        domain=get("TESLA_DOMAIN"),
        region=get("TESLA_REGION", "na").lower(),
        home=home,
    )
