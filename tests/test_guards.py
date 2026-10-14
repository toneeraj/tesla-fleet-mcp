"""Caps, allowlist, client naming, rate limit and audit log. No network."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from tesla_fleet import core, remote
from tesla_fleet.config import Limits, load_settings

NOW = 1_790_000_000.0  # a fixed instant, mid-afternoon Eastern


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ("TESLA_CLIENT_ID", "TESLA_CLIENT_SECRET", "TESLA_DOMAIN", "TESLA_REGION"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("TESLA_CLIENT_ID=cid\n")
    return load_settings(tmp_path)


def ledger(settings, limits=Limits(), at=NOW):
    return core.Ledger(settings, clock=lambda: at, limits=limits)


def as_client(name):
    return core.current_client.set(name)


# ---- caps in the core -------------------------------------------------------

def test_daily_wake_cap_refuses_before_counting(settings):
    led = ledger(settings, Limits(wakes_per_day=2))
    led.record("wake")
    led.record("wake")
    with pytest.raises(core.CapReached, match="Daily wake cap"):
        led.record("wake")
    assert led.summary()["wake_requests"] == 2  # the refused wake was never counted


def test_untrusted_client_gets_fewer_wakes(settings):
    led = ledger(settings, Limits(wakes_per_day=5, untrusted_client_wakes_per_day=1))
    token = as_client("muse.example")
    try:
        led.record("wake")
        with pytest.raises(core.CapReached, match="muse.example"):
            led.record("wake")
    finally:
        core.current_client.reset(token)
    token = as_client("claude.ai")
    try:
        led.record("wake")  # a trusted client still has budget
    finally:
        core.current_client.reset(token)


def test_daily_request_cap(settings):
    led = ledger(settings, Limits(requests_per_day=3))
    for _ in range(3):
        led.record("data")
    with pytest.raises(core.CapReached, match="Daily request cap"):
        led.record("data")


def test_daily_caps_reset_next_day(settings):
    ledger(settings, Limits(requests_per_day=1)).record("data")
    tomorrow = ledger(settings, Limits(requests_per_day=1), at=NOW + 86_400)
    tomorrow.record("data")  # no error


def test_monthly_ceiling_stops_spend(settings):
    led = ledger(settings, Limits(monthly_ceiling_usd=0.05, wakes_per_day=99))
    led.record("wake")
    led.record("wake")
    with pytest.raises(core.CapReached, match="Monthly spend ceiling"):
        led.record("wake")  # third wake would pass $0.05


def test_location_cap(settings):
    led = ledger(settings, Limits(location_reads_per_day=1))
    led.record("location")
    with pytest.raises(core.CapReached, match="location"):
        led.record("location")


def test_summary_shows_today_against_caps(settings):
    led = ledger(settings, Limits(requests_per_day=100))
    led.record("data")
    assert led.summary()["today"]["requests"] == "1 of 100"


# ---- naming clients and masking ----------------------------------------------

@pytest.mark.parametrize(
    "uris, name",
    [
        (["https://claude.ai/api/mcp/auth_callback"], "claude.ai"),
        (["http://localhost:54321/callback", "http://127.0.0.1/callback"], "claude-code"),
        (["https://connectors.muse.example/oauth/cb"], "connectors.muse.example"),
        ([], "unknown"),
    ],
)
def test_classify_redirects(uris, name):
    assert remote.classify_redirects(uris) == name


def test_untrusted_by_default():
    assert "connectors.muse.example" not in core.TRUSTED_CLIENTS
    assert "unknown" not in core.TRUSTED_CLIENTS


def test_mask_vin():
    assert remote.mask_vin("5YJ3TEST0000582800") == "…2800"


# ---- audit log ---------------------------------------------------------------

def test_audit_prune_keeps_90_days(tmp_path):
    audit = remote.AuditLog(tmp_path / "audit.log")
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    old = (now - timedelta(days=91)).isoformat()
    new = (now - timedelta(days=1)).isoformat()
    (tmp_path / "audit.log").write_text(
        json.dumps({"time": old, "event": "tool_call"}) + "\n" + json.dumps({"time": new, "event": "tool_call"}) + "\n"
    )
    audit.prune(now=now)
    lines = (tmp_path / "audit.log").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["time"] == new


# ---- one-time token seeding --------------------------------------------------

def test_seed_writes_once_and_never_overwrites(tmp_path, monkeypatch):
    audit = remote.AuditLog(tmp_path / "audit.log")
    path = tmp_path / "tokens.json"
    monkeypatch.setenv("TESLA_BOOTSTRAP_TOKENS", json.dumps({"access_token": "a", "refresh_token": "first"}))
    assert remote.seed_tokens_once(path, audit) is True
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("TESLA_BOOTSTRAP_TOKENS", json.dumps({"access_token": "b", "refresh_token": "stale"}))
    assert remote.seed_tokens_once(path, audit) is False
    assert json.loads(path.read_text())["refresh_token"] == "first"


def test_seed_rejects_tokens_without_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TESLA_BOOTSTRAP_TOKENS", json.dumps({"access_token": "a"}))
    with pytest.raises(SystemExit):
        remote.seed_tokens_once(tmp_path / "tokens.json", remote.AuditLog(tmp_path / "audit.log"))


# ---- middleware end to end, with a fake signed-in user -----------------------

def fake_token(login, client_id="client-1"):
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"client_id": client_id}).encode()).decode().rstrip("=")
    return SimpleNamespace(token=f"h.{payload}.s", claims={"login": login})


class FakeAuth:
    def __init__(self, uris):
        self.uris = uris

    async def get_client(self, client_id):
        return SimpleNamespace(redirect_uris=self.uris)


def guarded_server(tmp_path, monkeypatch, login, uris=("https://claude.ai/api/mcp/auth_callback",), limits=Limits()):
    monkeypatch.setattr(remote, "get_access_token", lambda: SimpleNamespace(token="gho_upstream_github_token", claims={"login": login}))
    monkeypatch.setattr(remote, "_presented_token", lambda: fake_token(login).token)
    audit = remote.AuditLog(tmp_path / "audit.log")
    server = FastMCP("test")
    seen = {}

    @server.tool
    def get_vehicle_status(vin: str, wake: bool = False) -> dict:
        seen["client"] = core.current_client.get()
        tracked = core.call_requests.get()
        tracked.extend(["data", "data"])
        return {"battery_percent": 82}

    @server.tool
    def capped() -> dict:
        raise core.CapReached("Daily wake cap reached: 5 of 5.")

    server.add_middleware(remote.AllowlistGuard({"toneeraj"}, audit))
    server.add_middleware(remote.CallGuard(audit, limits, auth=FakeAuth(list(uris))))
    return server, audit, seen


def audit_lines(audit):
    return [json.loads(line) for line in audit.path.read_text().splitlines()]


def call(server, tool, args=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args or {}, raise_on_error=False)

    return asyncio.run(go())


def list_tools(server):
    async def go():
        async with Client(server) as client:
            return await client.list_tools()

    return asyncio.run(go())


def test_owner_call_is_audited_without_car_data(tmp_path, monkeypatch):
    server, audit, seen = guarded_server(tmp_path, monkeypatch, "toneeraj")
    result = call(server, "get_vehicle_status", {"vin": "5YJ3TEST0000582800"})
    assert not result.is_error
    assert seen["client"] == "claude.ai"
    line = audit_lines(audit)[-1]
    assert line["outcome"] == "ok" and line["client"] == "claude.ai" and line["vin"] == "…2800"
    assert line["tesla_requests"] == 2 and line["est_cost_usd"] == pytest.approx(0.004)
    assert "82" not in json.dumps(line)  # the battery value never reaches the log


def test_stranger_is_refused_even_listing_tools(tmp_path, monkeypatch):
    server, audit, _ = guarded_server(tmp_path, monkeypatch, "someone-else")
    # Refused at the handshake: a stranger cannot even see which tools exist.
    with pytest.raises(Exception):
        list_tools(server)
    with pytest.raises(Exception):
        call(server, "get_vehicle_status", {"vin": "X"})
    assert audit_lines(audit), "every refusal is logged"
    assert all(line["event"] == "denied_account" for line in audit_lines(audit))
    assert audit_lines(audit)[0]["github_login"] == "someone-else"


def test_cap_refusal_is_audited(tmp_path, monkeypatch):
    server, audit, _ = guarded_server(tmp_path, monkeypatch, "toneeraj")
    result = call(server, "capped")
    assert result.is_error
    line = audit_lines(audit)[-1]
    assert line["outcome"] == "cap_refused" and "wake cap" in line["detail"]


def test_rate_limit_per_client(tmp_path, monkeypatch):
    server, audit, _ = guarded_server(tmp_path, monkeypatch, "toneeraj", limits=Limits(calls_per_minute=2))
    outcomes = [call(server, "get_vehicle_status", {"vin": "VIN0001"}).is_error for _ in range(3)]
    assert outcomes == [False, False, True]
    assert audit_lines(audit)[-1]["cap"] == "calls_per_minute"


def test_unknown_client_is_named_by_host(tmp_path, monkeypatch):
    server, _, seen = guarded_server(tmp_path, monkeypatch, "toneeraj", uris=("https://connectors.muse.example/cb",))
    call(server, "get_vehicle_status", {"vin": "VIN0001"})
    assert seen["client"] == "connectors.muse.example"


def test_client_named_from_presented_token_not_upstream_github_token(monkeypatch):
    """Regression: the upstream GitHub token has no client_id; naming must use the client's own token."""
    monkeypatch.setattr(remote, "get_http_request", lambda: SimpleNamespace(headers={"authorization": "Bearer " + fake_token("toneeraj").token}))
    assert remote._jwt_client_id(remote._presented_token()) == "client-1"
