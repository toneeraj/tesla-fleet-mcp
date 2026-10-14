"""Read-only diagnostics for the hosted server's sign-in state.

Runs on the server (via `flyctl ssh console`) and prints one JSON document:
  - registered OAuth clients: name, the client name the server derives from its
    redirect URIs, auth method, and a short client_id prefix
  - how many sign-in records exist at each stage of the OAuth flow
  - a sanitized audit-log summary (no IP addresses, no tokens)

It changes nothing. Output is safe for a public CI log: no secrets, codes,
full client IDs or IP addresses.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path

STAGES = {
    "mcp-oauth-proxy-clients": "1 registered clients",
    "mcp-oauth-transactions": "2 authorize requests in flight (waiting for GitHub)",
    "mcp-authorization-codes": "3 codes issued, not yet exchanged at /token",
    "mcp-jti-mappings": "4 access tokens issued",
}


def _load_process_env() -> None:
    """ssh sessions may not inherit the server's secrets; read them from the server process."""
    if os.environ.get("JWT_SIGNING_KEY"):
        return
    for pid in ("1", *sorted(p for p in os.listdir("/proc") if p.isdigit())):
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        env = dict(item.split("=", 1) for item in raw.decode(errors="ignore").split("\0") if "=" in item)
        if env.get("JWT_SIGNING_KEY"):
            for key, value in env.items():
                os.environ.setdefault(key, value)
            return


def _storage_root(auth) -> Path:
    inner = auth._client_storage.key_value
    for attr in ("data_directory", "_data_directory", "directory"):
        value = getattr(inner, attr, None)
        if value:
            return Path(str(value))
    raise RuntimeError("cannot locate OAuth storage directory")


def _entries(root: Path, collection: str):
    """Yield (path, entry) for each record file in a collection, skipping metadata."""
    prefix = "S_" + collection.replace("-", "_") + "-"
    for folder in root.iterdir():
        if folder.is_dir() and folder.name.startswith(prefix):
            for item in folder.iterdir():
                if item.suffix == ".json":
                    try:
                        yield item, json.loads(item.read_text())
                    except ValueError:
                        continue


def _live(entry: dict) -> bool:
    expires = entry.get("expires_at")
    if not expires:
        return True
    from datetime import datetime, timezone

    return datetime.fromisoformat(expires) > datetime.now(timezone.utc)


def _clients(auth) -> list[dict]:
    from tesla_fleet.remote import classify_redirects

    root = _storage_root(auth)
    found = []
    for _, entry in _entries(root, "mcp-oauth-proxy-clients"):
        data = auth._client_storage._decrypt_value(entry.get("value")) or {}
        uris = [str(u) for u in (data.get("redirect_uris") or [])]
        found.append(
            {
                "client_id_prefix": str(data.get("client_id", ""))[:8],
                "client_name": data.get("client_name"),
                "audit_name": classify_redirects(uris),
                "redirect_uris": uris,
                "token_endpoint_auth_method": data.get("token_endpoint_auth_method"),
                "scope": data.get("scope"),
                "registered_at": entry.get("created_at"),
            }
        )
    return sorted(found, key=lambda c: c.get("registered_at") or "")


def _stage_counts(auth) -> dict:
    root = _storage_root(auth)
    counts = {}
    for collection, label in STAGES.items():
        entries = [entry for _, entry in _entries(root, collection)]
        live = [e for e in entries if _live(e)]
        counts[label] = {
            "live": len(live),
            "expired_on_disk": len(entries) - len(live),
            "newest": max((e.get("created_at") or "" for e in entries), default=None),
        }
    return counts


def _audit(path: Path, last: int = 25) -> dict:
    if not path.exists():
        return {"present": False}
    lines = []
    for raw in path.read_text().splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    keep = ("time", "event", "client", "client_ref", "tool", "outcome", "cap", "status", "reason", "github_login", "detail")
    recent = [{k: v for k, v in line.items() if k in keep} for line in lines[-last:]]
    return {
        "present": True,
        "events_total": len(lines),
        "by_event": dict(Counter(line.get("event") for line in lines)),
        "tool_calls_by_client": dict(Counter(line.get("client") for line in lines if line.get("event") == "tool_call")),
        "recent": recent,
    }


async def main() -> None:
    _load_process_env()
    from fastmcp.server.auth.providers.github import GitHubProvider

    from tesla_fleet.config import private_home

    auth = GitHubProvider(
        client_id=os.environ["GITHUB_CLIENT_ID"],
        client_secret=os.environ["GITHUB_CLIENT_SECRET"],
        base_url=os.environ["PUBLIC_BASE_URL"],
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
    )
    report = {
        "clients": _clients(auth),
        "oauth_stage_counts": _stage_counts(auth),
        "audit": _audit(private_home() / "audit.log"),
        "tesla_tokens_present": (private_home() / "tokens.json").exists(),
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
