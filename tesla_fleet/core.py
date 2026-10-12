"""The core: every rule and every call to Tesla lives here.

- One token owner: TokenStore is the only reader/writer of tokens.json, and a
  file lock serializes refreshes so two processes can't burn the same
  single-use refresh token.
- Wake guard: an asleep car is reported as asleep unless wake=True.
- Spend ledger: every Fleet API request is counted before it is sent.
- Compact responses: about ten fields, never the full vehicle payload.
- Read-only: there is no command method and no raw passthrough.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .config import (
    AUTH_BASE,
    COST_PER_DATA_REQUEST,
    COST_PER_WAKE,
    MONTHLY_CREDIT_USD,
    Settings,
    load_settings,
)

WAKE_RETRIES = 6
WAKE_RETRY_SECONDS = 5.0
REFRESH_MARGIN_SECONDS = 60


class FleetError(Exception):
    """A Fleet API problem worth explaining to the user."""


class LoginRequired(FleetError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "Tesla login required. Run: python -m tesla_fleet.login" + (f" ({detail})" if detail else "")
        )


class VehicleUnavailable(FleetError):
    pass


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


class TokenStore:
    """Sole owner of tokens.json."""

    def __init__(self, settings: Settings, http: httpx.Client, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.http = http
        self.clock = clock

    def save(self, token_response: dict[str, Any], previous_refresh: str | None = None) -> None:
        refresh = token_response.get("refresh_token") or previous_refresh
        if not refresh:
            raise FleetError("Tesla returned no refresh token. Was the offline_access scope granted?")
        _write_private_json(
            self.settings.token_path,
            {
                "access_token": token_response["access_token"],
                "refresh_token": refresh,
                "expires_at": self.clock() + int(token_response.get("expires_in", 0)),
            },
        )

    def access_token(self, force_refresh: bool = False) -> str:
        with file_lock(self.settings.lock_path):
            tokens = _read_json(self.settings.token_path)
            if not tokens.get("refresh_token"):
                raise LoginRequired("no tokens saved yet")
            fresh = tokens.get("expires_at", 0) - REFRESH_MARGIN_SECONDS > self.clock()
            if fresh and not force_refresh:
                return tokens["access_token"]
            response = self.http.post(
                f"{AUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.client_id,
                    "refresh_token": tokens["refresh_token"],
                },
            )
            if response.status_code in (400, 401):
                raise LoginRequired("refresh token expired or already used")
            response.raise_for_status()
            # Save the rotated refresh token before anything else can fail.
            self.save(response.json(), previous_refresh=tokens["refresh_token"])
            return response.json()["access_token"]


class Ledger:
    """Counts Fleet API requests per month for spend estimates."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.clock = clock

    def _month(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m")

    def record(self, kind: str) -> None:
        with file_lock(self.settings.lock_path.with_name(".ledger.lock")):
            ledger = _read_json(self.settings.ledger_path)
            month = ledger.setdefault(self._month(), {"data": 0, "wake": 0})
            month[kind] = month.get(kind, 0) + 1
            _write_private_json(self.settings.ledger_path, ledger)

    def summary(self, month: str | None = None) -> dict[str, Any]:
        month = month or self._month()
        counts = _read_json(self.settings.ledger_path).get(month, {"data": 0, "wake": 0})
        spend = counts.get("data", 0) * COST_PER_DATA_REQUEST + counts.get("wake", 0) * COST_PER_WAKE
        return {
            "month": month,
            "data_requests": counts.get("data", 0),
            "wake_requests": counts.get("wake", 0),
            "estimated_spend_usd": round(spend, 4),
            "monthly_credit_usd": MONTHLY_CREDIT_USD,
            "estimated_credit_left_usd": round(MONTHLY_CREDIT_USD - spend, 4),
            "note": "Estimate from this machine's own ledger, using published per-request prices.",
        }


def _status_fields(vin: str, data: dict[str, Any], include_location: bool) -> dict[str, Any]:
    charge = data.get("charge_state") or {}
    vehicle = data.get("vehicle_state") or {}
    climate = data.get("climate_state") or {}
    status = {
        "vin": vin,
        "name": data.get("display_name"),
        "state": data.get("state", "online"),
        "battery_percent": charge.get("battery_level"),
        "range_miles": charge.get("battery_range"),
        "charging_state": charge.get("charging_state"),
        "charge_limit_percent": charge.get("charge_limit_soc"),
        "minutes_to_full": round(charge["time_to_full_charge"] * 60) if charge.get("time_to_full_charge") else None,
        "locked": vehicle.get("locked"),
        "odometer_miles": round(vehicle["odometer"]) if vehicle.get("odometer") is not None else None,
        "inside_temp_c": climate.get("inside_temp"),
        "outside_temp_c": climate.get("outside_temp"),
        "climate_on": climate.get("is_climate_on"),
    }
    if include_location:
        drive = data.get("drive_state") or {}
        status["latitude"] = drive.get("latitude")
        status["longitude"] = drive.get("longitude")
    return status


def _session_fields(session: dict[str, Any]) -> dict[str, Any]:
    fees = session.get("fees") or []
    cost = sum(fee.get("totalDue") or 0 for fee in fees)
    energy = sum(
        fee.get("usageBase") or 0
        for fee in fees
        if fee.get("feeType") == "CHARGING" and str(fee.get("uom", "")).lower() == "kwh"
    )
    currency = next((fee.get("currencyCode") for fee in fees if fee.get("currencyCode")), None)
    return {
        "start": session.get("chargeStartDateTime"),
        "end": session.get("chargeStopDateTime"),
        "site": session.get("siteLocationName"),
        "vin": session.get("vin"),
        "energy_kwh": round(energy, 2) if energy else None,
        "cost": round(cost, 2),
        "currency": currency,
    }


def _as_iso(day_or_iso: str, end_of_day: bool) -> str:
    if "T" in day_or_iso:
        return day_or_iso
    datetime.strptime(day_or_iso, "%Y-%m-%d")  # validates
    return f"{day_or_iso}T23:59:59Z" if end_of_day else f"{day_or_iso}T00:00:00Z"


class FleetClient:
    def __init__(
        self,
        settings: Settings | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or load_settings()
        self.http = http or httpx.Client(timeout=30)
        self.sleep = sleep
        self.tokens = TokenStore(self.settings, self.http, clock)
        self.ledger = Ledger(self.settings, clock)

    def _request(self, method: str, path: str, kind: str = "data", **kwargs: Any) -> dict[str, Any]:
        url = f"{self.settings.api_base}{path}"
        for attempt in range(2):
            token = self.tokens.access_token(force_refresh=attempt == 1)
            self.ledger.record(kind)
            response = self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code != 401:
                break
        if response.status_code == 401:
            raise LoginRequired("access token rejected")
        if response.status_code == 408:
            raise VehicleUnavailable("Vehicle is offline or asleep.")
        if response.status_code == 429:
            raise FleetError("Tesla rate limit reached. Try again later.")
        if response.status_code >= 400:
            raise FleetError(f"Tesla API error {response.status_code}: {response.text[:200]}")
        return response.json()

    def list_vehicles(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/api/1/vehicles")
        return [
            {"vin": v.get("vin"), "name": v.get("display_name"), "state": v.get("state")}
            for v in body.get("response", [])
        ]

    def _vehicle_data(self, vin: str, include_location: bool) -> dict[str, Any]:
        endpoints = "charge_state;climate_state;vehicle_state"
        if include_location:
            endpoints += ";drive_state;location_data"
        return self._request("GET", f"/api/1/vehicles/{vin}/vehicle_data", params={"endpoints": endpoints})[
            "response"
        ]

    def vehicle_status(self, vin: str, wake: bool = False, include_location: bool = False) -> dict[str, Any]:
        vehicles = {v["vin"]: v for v in self.list_vehicles()}
        if vin not in vehicles:
            raise FleetError(f"No vehicle with VIN {vin} on this account.")
        state = vehicles[vin]["state"]
        if state != "online":
            if not wake:
                return {
                    "vin": vin,
                    "name": vehicles[vin]["name"],
                    "state": state,
                    "note": "Car is not awake. Ask again with wake=True to wake it; a wake costs about 10x a read.",
                }
            self._request("POST", f"/api/1/vehicles/{vin}/wake_up", kind="wake")
            for _ in range(WAKE_RETRIES):
                self.sleep(WAKE_RETRY_SECONDS)
                try:
                    return _status_fields(vin, self._vehicle_data(vin, include_location), include_location)
                except VehicleUnavailable:
                    continue
            raise VehicleUnavailable("Car did not wake in time. Try again in a minute.")
        return _status_fields(vin, self._vehicle_data(vin, include_location), include_location)

    def charging_history(self, start: str, end: str, vin: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "startTime": _as_iso(start, end_of_day=False),
            "endTime": _as_iso(end, end_of_day=True),
            "pageSize": 50,
        }
        if vin:
            params["vin"] = vin
        body = self._request("GET", "/api/1/dx/charging/history", params=params)
        sessions = [_session_fields(s) for s in body.get("data", [])]
        totals: dict[str, float] = {}
        for s in sessions:
            if s["currency"]:
                totals[s["currency"]] = round(totals.get(s["currency"], 0) + s["cost"], 2)
        return {
            "start": start,
            "end": end,
            "sessions": sessions,
            "total_cost": totals,
            "note": "Supercharger sessions only; home charging is not reported by the Fleet API.",
        }

    def spend(self, month: str | None = None) -> dict[str, Any]:
        return self.ledger.summary(month)


_client: FleetClient | None = None


def _default_client() -> FleetClient:
    global _client
    if _client is None:
        _client = FleetClient()
    return _client


def list_vehicles() -> list[dict[str, Any]]:
    return _default_client().list_vehicles()


def vehicle_status(vin: str, wake: bool = False, include_location: bool = False) -> dict[str, Any]:
    return _default_client().vehicle_status(vin, wake=wake, include_location=include_location)


def charging_history(start: str, end: str) -> dict[str, Any]:
    return _default_client().charging_history(start, end)


def spend_ledger(month: str | None = None) -> dict[str, Any]:
    # Reads the local ledger only; needs no Tesla credentials beyond settings.
    return Ledger(load_settings()).summary(month)
