"""Core behavior with mocked HTTP. No live Tesla calls."""

from __future__ import annotations

import json
import stat

import httpx
import pytest

from tesla_fleet import core
from tesla_fleet.config import load_settings

NOW = 1_790_000_000.0  # fixed clock
VIN = "5YJ3TEST0000000001"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_DOMAIN", "TESLA_REGION"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("TESLA_CLIENT_ID=cid\nTESLA_CLIENT_SECRET=secret\nTESLA_DOMAIN=example.github.io\n")
    return load_settings(tmp_path)


def save_tokens(settings, access="at-1", refresh="rt-1", expires_at=NOW + 3600):
    settings.token_path.write_text(json.dumps({"access_token": access, "refresh_token": refresh, "expires_at": expires_at}))


class FakeTesla:
    """Routes requests to canned responses and records what was called."""

    def __init__(self, vehicle_state="online"):
        self.calls: list[str] = []
        self.vehicle_state = vehicle_state
        self.data_unavailable_times = 0
        self.refresh_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path.endswith("/oauth2/v3/token"):
            self.refresh_count += 1
            n = self.refresh_count
            return httpx.Response(200, json={"access_token": f"at-new-{n}", "refresh_token": f"rt-new-{n}", "expires_in": 28800})
        if path == "/api/1/vehicles":
            return httpx.Response(200, json={"response": [{"vin": VIN, "display_name": "Blue", "state": self.vehicle_state}]})
        if path.endswith("/wake_up"):
            self.vehicle_state = "online"
            return httpx.Response(200, json={"response": {"state": "online"}})
        if path.endswith("/vehicle_data"):
            if self.data_unavailable_times:
                self.data_unavailable_times -= 1
                return httpx.Response(408, json={"error": "vehicle unavailable"})
            return httpx.Response(200, json={"response": {
                "display_name": "Blue", "state": "online",
                "charge_state": {"battery_level": 82, "battery_range": 240.4, "charging_state": "Disconnected",
                                 "charge_limit_soc": 80, "time_to_full_charge": 0.0},
                "vehicle_state": {"locked": True, "odometer": 12345.6},
                "climate_state": {"inside_temp": 21.5, "outside_temp": 12.0, "is_climate_on": False},
                "drive_state": {"latitude": 42.27, "longitude": -71.69},
            }})
        if path == "/api/1/dx/charging/history":
            return httpx.Response(200, json={"data": [{
                "vin": VIN, "siteLocationName": "Worcester, MA",
                "chargeStartDateTime": "2026-10-03T14:00:00Z", "chargeStopDateTime": "2026-10-03T14:30:00Z",
                "fees": [
                    {"feeType": "CHARGING", "uom": "kwh", "usageBase": 41.2, "totalDue": 16.48, "currencyCode": "USD"},
                    {"feeType": "PARKING", "uom": "min", "usageBase": 0, "totalDue": 0, "currencyCode": "USD"},
                ],
            }]})
        return httpx.Response(404)


def client(settings, fake):
    return core.FleetClient(settings, httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None, clock=lambda: NOW)


def test_no_tokens_means_login_required(settings):
    with pytest.raises(core.LoginRequired):
        client(settings, FakeTesla()).list_vehicles()


def test_expired_token_refreshes_and_saves_rotated_refresh_token(settings):
    save_tokens(settings, expires_at=NOW - 10)
    fake = FakeTesla()
    client(settings, fake).list_vehicles()
    saved = json.loads(settings.token_path.read_text())
    assert saved["refresh_token"] == "rt-new-1"
    assert saved["access_token"] == "at-new-1"
    assert fake.refresh_count == 1


def test_token_file_is_owner_only(settings):
    save_tokens(settings, expires_at=NOW - 10)
    client(settings, FakeTesla()).list_vehicles()
    assert stat.S_IMODE(settings.token_path.stat().st_mode) == 0o600


def test_fresh_token_is_not_refreshed(settings):
    save_tokens(settings)
    fake = FakeTesla()
    client(settings, fake).list_vehicles()
    assert fake.refresh_count == 0


def test_rejected_refresh_asks_for_login(settings):
    save_tokens(settings, expires_at=NOW - 10)

    def handler(request):
        return httpx.Response(401, json={"error": "login_required"})

    with pytest.raises(core.LoginRequired):
        client(settings, handler).list_vehicles()


def test_asleep_car_is_not_woken_by_default(settings):
    save_tokens(settings)
    fake = FakeTesla(vehicle_state="asleep")
    result = client(settings, fake).vehicle_status(VIN)
    assert result["state"] == "asleep"
    assert not any("wake_up" in c or "vehicle_data" in c for c in fake.calls)


def test_wake_true_wakes_and_retries_until_data(settings):
    save_tokens(settings)
    fake = FakeTesla(vehicle_state="asleep")
    fake.data_unavailable_times = 2
    result = client(settings, fake).vehicle_status(VIN, wake=True)
    assert result["battery_percent"] == 82
    assert sum("wake_up" in c for c in fake.calls) == 1
    summary = core.Ledger(settings, clock=lambda: NOW).summary()
    assert summary["wake_requests"] == 1


def test_status_is_compact_and_location_is_opt_in(settings):
    save_tokens(settings)
    c = client(settings, FakeTesla())
    status = c.vehicle_status(VIN)
    assert status["battery_percent"] == 82 and status["locked"] is True and status["odometer_miles"] == 12346
    assert "latitude" not in status
    assert len(status) <= 14
    assert c.vehicle_status(VIN, include_location=True)["latitude"] == 42.27


def test_unknown_vin_is_explained(settings):
    save_tokens(settings)
    with pytest.raises(core.FleetError, match="No vehicle"):
        client(settings, FakeTesla()).vehicle_status("NOPE")


def test_charging_history_shapes_sessions_and_totals(settings):
    save_tokens(settings)
    result = client(settings, FakeTesla()).charging_history("2026-10-01", "2026-10-31")
    session = result["sessions"][0]
    assert session["energy_kwh"] == 41.2 and session["cost"] == 16.48
    assert result["total_cost"] == {"USD": 16.48}


def test_charging_history_rejects_bad_dates(settings):
    save_tokens(settings)
    with pytest.raises(ValueError):
        client(settings, FakeTesla()).charging_history("Oct 1", "2026-10-31")


def test_ledger_counts_every_request(settings):
    save_tokens(settings)
    c = client(settings, FakeTesla())
    c.list_vehicles()
    c.vehicle_status(VIN)  # list + vehicle_data
    summary = c.spend()
    assert summary["data_requests"] == 3 and summary["wake_requests"] == 0
    assert summary["estimated_spend_usd"] == pytest.approx(0.006)
