"""Hosted mode: the same four tools over HTTPS, behind GitHub sign-in.

Layers, outermost first:
  1. GitHub OAuth (FastMCP's GitHubProvider) proves who is calling.
  2. AllowlistGuard refuses every MCP request from any GitHub account not on
     the allowlist. GitHub sign-in alone would admit any GitHub user.
  3. CallGuard names the client (Claude Code, claude.ai, or something else such
     as Muse), applies a per-minute rate limit, and writes the audit log.
  4. The core enforces the daily and monthly caps before anything reaches Tesla.

Run with:  python -m tesla_fleet.remote
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import core
from .config import Limits, private_home

AUDIT_DAYS_KEPT = 90


def mask_vin(vin: Any) -> Any:
    return f"…{vin[-4:]}" if isinstance(vin, str) and len(vin) > 4 else vin


class AuditLog:
    """Append-only JSON lines. Records that something happened, never the car's data."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(line) + "\n")

    def prune(self, now: datetime | None = None) -> None:
        if not self.path.exists():
            return
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=AUDIT_DAYS_KEPT)
        kept = []
        for raw in self.path.read_text().splitlines():
            try:
                if datetime.fromisoformat(json.loads(raw)["time"]) >= cutoff:
                    kept.append(raw)
            except (ValueError, KeyError):
                continue
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("".join(line + "\n" for line in kept))
        os.replace(tmp, self.path)


def classify_redirects(redirect_uris: list[str]) -> str:
    """Name a client by where its sign-in returns to. Unknown clients keep their host as a name."""
    hosts = {urlparse(str(uri)).hostname or "" for uri in redirect_uris}
    if "claude.ai" in hosts:
        return "claude.ai"
    if hosts and hosts <= {"localhost", "127.0.0.1"}:
        return "claude-code"
    named = sorted(h for h in hosts if h)
    return named[0] if named else "unknown"


def _jwt_client_id(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("client_id")
    except (IndexError, ValueError):
        return None


class AllowlistGuard(Middleware):
    """Refuses every MCP request, including tool listing, from accounts not on the allowlist."""

    def __init__(self, allowed_logins: set[str], audit: AuditLog) -> None:
        self.allowed = {login.lower() for login in allowed_logins}
        self.audit = audit

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        token = get_access_token()
        login = (token.claims.get("login") if token else None) or ""
        if login.lower() not in self.allowed:
            self.audit.write("denied_account", github_login=login or None, method=getattr(context, "method", None))
            raise AuthorizationError("This server only serves its owner.")
        return await call_next(context)


class CallGuard(Middleware):
    def __init__(self, audit: AuditLog, limits: Limits, auth: Any = None, clock=time.monotonic) -> None:
        self.audit = audit
        self.limits = limits
        self.auth = auth
        self.clock = clock
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._client_names: dict[str, str] = {}

    async def client_name(self) -> str:
        token = get_access_token()
        client_id = _jwt_client_id(token.token) if token else None
        if not client_id:
            return "unknown"
        if client_id not in self._client_names:
            info = await self.auth.get_client(client_id) if self.auth else None
            uris = [str(u) for u in (info.redirect_uris or [])] if info else []
            self._client_names[client_id] = classify_redirects(uris)
        return self._client_names[client_id]

    def _rate_ok(self, client: str) -> bool:
        now = self.clock()
        window = self._recent[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.limits.calls_per_minute:
            return False
        window.append(now)
        return True

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        tool = context.message.name
        args = dict(context.message.arguments or {})
        token = get_access_token()
        client = await self.client_name()
        base = {
            "github_login": token.claims.get("login") if token else None,
            "client": client,
            "tool": tool,
            "vin": mask_vin(args.get("vin")),
            "wake": args.get("wake"),
            "include_location": args.get("include_location"),
        }
        base = {k: v for k, v in base.items() if v is not None}

        if not self._rate_ok(client):
            self.audit.write("tool_call", **base, outcome="cap_refused", cap="calls_per_minute", tesla_requests=0)
            raise ToolError(f"Rate limit: more than {self.limits.calls_per_minute} calls a minute from {client}.")

        sent: list[str] = []
        client_token = core.current_client.set(client)
        sent_token = core.call_requests.set(sent)
        started = time.monotonic()
        outcome, detail = "ok", None
        try:
            return await call_next(context)
        except Exception as exc:  # noqa: BLE001 - classify, log, re-raise
            cause = exc.__cause__ or exc
            if isinstance(cause, core.CapReached) or "cap reached" in str(exc).lower():
                outcome, detail = "cap_refused", str(cause)
            else:
                outcome, detail = "error", type(cause).__name__
            raise
        finally:
            core.current_client.reset(client_token)
            core.call_requests.reset(sent_token)
            cost = sent.count("data") * core.COST_PER_DATA_REQUEST + sent.count("wake") * core.COST_PER_WAKE
            self.audit.write(
                "tool_call",
                **base,
                outcome=outcome,
                **({"detail": detail} if detail else {}),
                tesla_requests=len(sent),
                wakes=sent.count("wake"),
                est_cost_usd=round(cost, 4),
                ms=round((time.monotonic() - started) * 1000),
            )


class RejectedAuthLogger:
    """ASGI wrapper that records refused sign-ins and bad tokens (401/403) on the MCP endpoint."""

    def __init__(self, app: Any, audit: AuditLog) -> None:
        self.app = app
        self.audit = audit

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        async def watch(message: dict) -> None:
            if message.get("type") == "http.response.start" and message.get("status") in (401, 403):
                headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
                had_token = headers.get("authorization", "").lower().startswith("bearer ")
                self.audit.write(
                    "rejected_auth",
                    status=message["status"],
                    reason="bad_or_expired_token" if had_token else "no_token",
                    ip=headers.get("fly-client-ip") or (scope.get("client") or ["?"])[0],
                )
            await send(message)

        await self.app(scope, receive, watch)


def build_server():
    """Assemble the hosted server from environment variables. Fails fast if any are missing."""
    from fastmcp.server.auth.providers.github import GitHubProvider

    from .server import mcp

    required = ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "PUBLIC_BASE_URL", "JWT_SIGNING_KEY", "ALLOWED_GITHUB_LOGINS"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    allowed = {login.strip() for login in os.environ["ALLOWED_GITHUB_LOGINS"].split(",") if login.strip()}
    audit = AuditLog(private_home() / "audit.log")
    audit.prune()
    limits = Limits.from_env()

    auth = GitHubProvider(
        client_id=os.environ["GITHUB_CLIENT_ID"],
        client_secret=os.environ["GITHUB_CLIENT_SECRET"],
        base_url=os.environ["PUBLIC_BASE_URL"],
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
    )
    mcp.auth = auth
    mcp.add_middleware(AllowlistGuard(allowed, audit))
    mcp.add_middleware(CallGuard(audit, limits, auth=auth))

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return mcp, audit


def main() -> None:
    import uvicorn

    mcp, audit = build_server()
    app = mcp.http_app(path="/mcp")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(RejectedAuthLogger(app, audit), host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
