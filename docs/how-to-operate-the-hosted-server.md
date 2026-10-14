# How to operate the hosted server

**Prerequisites**

- Admin access to the GitHub repository `toneeraj/tesla-fleet-mcp`
- The 1Password vault `tesla-fleet` with these Password items, value in the password field: `fly-api-token`, `github-oauth-client-id`, `github-oauth-client-secret`, `tesla-client-id`
- One GitHub repository secret: `OP_SERVICE_ACCOUNT_TOKEN`, a 1Password service account with read-only access to `tesla-fleet` only
- The Fly.io app `tesla-fleet-mcp-toneeraj` (created by the first deploy)

All routine tasks run from a browser. Nothing below needs a local machine except replacing an expired Tesla login.

## Deploy a change

1. Merge the change to `main`.
2. Open **Actions → Deploy → Run workflow**, keep `main`, and run it. Use **Run workflow**, never **Re-run jobs** on an old run: a re-run replays the old commit.

**Verify:** both jobs are green, and the run's **Health check** step printed `{"ok":true}`.

## Check sign-ins and the audit log

1. Open **Actions → Diagnose → Run workflow**.
2. Read the run's annotations: registered clients and how each is named, how far recent sign-ins got (authorize → code issued → token issued), and the last audit lines.

**Verify:** each client appears with the expected name (`claude-code`, `claude.ai`, `agent.meta.ai`). Addresses, codes and tokens appear as `<ip>` or `<redacted>`.

## Connect a new client

1. In the client, add a custom connector with URL `https://tesla-fleet-mcp-toneeraj.fly.dev/mcp` and no client ID or secret.
2. Approve the server's consent page, then sign in to GitHub as `toneeraj`.
3. Ask the client to run `list_vehicles`.

**Verify:** run Diagnose; the client's call appears in the audit log under its own name. A client the server cannot name appears as `unknown` and gets the autonomous wake budget.

## Rotate a deploy secret

1. Generate the new value at its source: Fly dashboard → Tokens, or GitHub → Settings → Developer settings → OAuth Apps.
2. Replace the value in the matching `tesla-fleet` item in 1Password.
3. Run **Deploy**.
4. Revoke the old value at its source.

**Verify:** Deploy is green and an existing client still answers `list_vehicles`.

## Tighten spend limits

Fly has no spending cap. Two controls bound the bill:

1. Pay Fly with a card number that has its own monthly limit. A card's spending limit for the account holder, or a per-purchase limit, does not cap a monthly bill.
2. Scope the deploy token to this app:
    1. Fly dashboard → app `tesla-fleet-mcp-toneeraj` → **Tokens** → create a deploy token with a one-year expiry.
    2. Replace the value of `fly-api-token` in the `tesla-fleet` vault with it.
    3. Run **Deploy**, then run **Diagnose**.
    4. Revoke the organisation token in the Fly dashboard.

    An app-scoped token cannot create the app, so a full rebuild from nothing needs an organisation token for that one run.

To change a usage cap, set its environment variable in the `[env]` block of `fly.toml` and deploy: `CAP_WAKES_PER_DAY`, `CAP_REQUESTS_PER_DAY`, `CAP_MONTHLY_CEILING_USD`, `CAP_LOCATION_PER_DAY`, `CAP_CALLS_PER_MINUTE`, `CAP_UNTRUSTED_WAKES_PER_DAY`.

**Verify:** Deploy and Diagnose are green on the scoped token; `get_api_spend` reports the caps under `today`.

## Know what auto-sleep changes

The machine stops after a few idle minutes and starts on the next request, so compute is billed only while it answers; the 1 GB volume is billed regardless. The first call after a quiet spell takes a few seconds longer. Daily caps, the ledger, the audit log and the Tesla login survive every sleep because they live on the volume; the per-minute rate limit resets because it lives in memory. To keep the machine always on, set `auto_stop_machines = "off"` and `min_machines_running = 1` in `fly.toml` and run **Deploy**.

## Revoke all access in an emergency

1. Tesla account → Third-party apps → remove **My Fleet Assistant**. This kills the server's Tesla login.
2. GitHub → Settings → Applications → Authorized OAuth Apps → revoke **tesla-fleet-mcp**. This signs out every client.
3. Fly dashboard → the app → **Suspend** or destroy it.

**Verify:** any client call fails, and Diagnose shows rejected sign-ins.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Deploy step fails with exit code 127 | A command name not installed on the runner | Check the step log for the missing command |
| A tool returns `Tesla login required` | The Tesla refresh token expired, usually after about three months unused | On a laptop with the repository: run `python -m tesla_fleet.login`, then `flyctl ssh console -a tesla-fleet-mcp-toneeraj -C "rm /data/tesla/tokens.json"`, save the new `tokens.json` as a one-line `tesla-tokens-seed` item in the vault, run Deploy, then delete the item and the local file |
| A client shows "registered but holds no credential" | The client never opened the server's authorize page | Run Diagnose; if no newer authorize request appears after the client's registration, the fault is in the client's sign-in flow |
| The first call after idle times out | The machine was waking | Ask again; the second call reaches a running machine |
| A tool returns a cap message | A daily or monthly cap was reached | Wait for the reset in the message, or raise the cap per *Tighten spend limits* |
| Diagnose shows an address or token unredacted | The redaction missed a format | Delete the run (Actions → Diagnose → the run's **⋯** → Delete workflow run) and fix the patterns in `diagnose.yml` |

## Related

- [Safety design for a hosted MCP server that several AI agents share](safety-design-for-a-hosted-mcp-server.md)
- [Architecture diagram](architecture.svg)
