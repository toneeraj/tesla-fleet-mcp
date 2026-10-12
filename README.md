# tesla-fleet-mcp

Ask Claude about your Tesla. A read-only MCP server over Tesla's official Fleet API, built so an AI client can answer "what's my battery?" without ever being able to send a command to the car.

**Status:** Phase 1 (read-only) in progress.
**Full design write-up:** https://claude.ai/code/artifact/91137cdf-1812-4e6a-8c5c-69acbea7a126

## What it does

Four tools, all read-only:

| Tool | Returns | Guard |
| --- | --- | --- |
| `list_vehicles` | Vehicles with online or asleep state | Never wakes the car |
| `get_vehicle_status` | Battery %, range, charging state, lock, climate, odometer | Returns `asleep` unless `wake=True`; location only on request |
| `get_charging_history` | Supercharger sessions and cost in a date range | Home charging not covered |
| `get_api_spend` | Calls made and estimated spend against the monthly credit | Reads the local ledger; no Tesla call |

## Design rules

- **No commands.** No tool can lock, unlock, or change anything on the car.
- **No passthrough.** There is no raw "call any endpoint" tool.
- **No polling.** Tesla is called only when a question is asked.
- **One token owner.** Tesla refresh tokens are single-use. Only the core reads and writes the token file, and it saves each new refresh token immediately.
- **Thin wrapper.** All logic lives in `tesla_fleet.core`. The MCP server only publishes it as tools.

## How a question flows

Claude Code → MCP server (`tesla_fleet.server`) → core (`tesla_fleet.core`) → Tesla Fleet API.
The core refreshes tokens, records spend, applies the wake guard, and returns about ten fields instead of the full vehicle payload.

## Setup

### Prerequisites

- Python 3.11+
- A Tesla account with a vehicle
- A domain that can host a public key (this project uses GitHub Pages)

### One-time Tesla setup

1. Generate an EC `prime256v1` key pair in `~/.tesla-fleet/` (never in this repo).
2. Host the public key at `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`. On GitHub Pages, add an empty `.nojekyll` file at the site root.
3. Create a developer app at https://developer.tesla.com with:
   - allowed origin: your domain
   - redirect URI: `http://localhost:8080/callback`
   - read scopes for vehicle information and charging
4. Put the credentials in `~/.tesla-fleet/.env`:

   ```
   TESLA_CLIENT_ID=...
   TESLA_CLIENT_SECRET=...
   TESLA_DOMAIN=your-domain
   TESLA_REGION=na
   ```

### Install, register, log in

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m tesla_fleet.register_partner   # one time: ties the app to your domain
python -m tesla_fleet.login              # opens a browser; saves ~/.tesla-fleet/tokens.json
```

### Connect to Claude Code

```bash
claude mcp add --scope user tesla-fleet -- "$(pwd)/.venv/bin/python" -m tesla_fleet.server
```

Restart Claude Code and ask: *"What's my battery?"*

## Tests

```bash
pip install -e '.[dev]'
pytest
```

Tests use mocked HTTP and cover token rotation, the wake guard, the spend ledger, response shaping, and that the MCP surface is exactly four read-only tools.

## Secrets

Nothing secret lives in this repository. `~/.tesla-fleet/` holds the private key, client credentials, and tokens. `.gitignore` blocks `.env`, `*.pem`, and `tokens.json` as a second line of defense.

## Project layout

```
tesla_fleet/
  config.py            # endpoints, scopes, price constants, reads ~/.tesla-fleet/.env
  core.py              # auth refresh, wake guard, spend ledger, Tesla calls
  server.py            # FastMCP wrapper: the four tools, no logic
  register_partner.py  # one-time domain registration
  login.py             # one-time OAuth login with local callback
tests/                 # pytest, mocked HTTP; no live Tesla calls
```

## Roadmap

1. **Phase 1:** read-only, one client (Claude Code). *Current.*
2. **Phase 2:** a few signed commands (lock, climate), each requiring confirmation, after virtual key pairing.
3. **Phase 3:** a hosted MCP server that owns the tokens and serves a second client.

## License

MIT

Built by [Neeraj Singh](https://github.com/toneeraj).
