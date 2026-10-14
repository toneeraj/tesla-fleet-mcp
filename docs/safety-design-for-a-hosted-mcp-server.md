# Safety design for a hosted MCP server that several AI agents share

**Purpose:** explains how this server lets three different AI clients read a real car's data while staying safe against agents that can be steered, agents that loop, strangers who find the URL, and a compromised host, and what the build showed about each defence.

![Architecture: three clients, one gate, one owner of the Tesla login](architecture.svg)

## What the server is

The server exposes four read-only tools over Tesla's official Fleet API: list vehicles, vehicle status, Supercharger history and API spend. It runs as one machine with one disk on Fly.io and is reachable over HTTPS by three clients: Claude Code, claude.ai (web and phone), and Meta's Muse agent. Every client signs in through the same gate, and only one process ever talks to Tesla.

The design problem is not the Tesla API. It is that one server is shared by callers with very different risk profiles. A person typing in Claude Code is supervised. Muse acts on its own around the clock and reads untrusted content such as email, so a malicious message could try to steer it into calling the server's tools. The design treats every caller as a possible source of misuse and makes the worst outcome of misuse small, bounded and visible.

## The threats, and the layer that answers each

| Threat | Layer | Effect |
| --- | --- | --- |
| A stranger finds the URL | Auth gate | GitHub sign-in, then an allowlist with one account on it. Any other account is refused at the connection handshake, before it can even list the tools. |
| An agent is steered by malicious content | Read-only tool surface | No tool can change the car. A test fails the build if a command tool or a raw passthrough is ever added. |
| An agent loops | Caps and rate limit | Daily caps on wakes, requests and location reads, a monthly spend ceiling, and 10 calls a minute per client. |
| An autonomous agent overspends | Per-caller budget | Clients driven by a person get 5 wakes a day; Muse and any unrecognised client get 2. |
| Misuse goes unnoticed | Audit log | One line per call, refusal and rejected sign-in, naming the caller. |
| Two copies of the Tesla login | Single owner | One machine, one disk, one file lock. No client holds the login. |
| The host is compromised | Least secrets | The server never holds the Tesla client secret or private key. Access can be revoked from the Tesla account. |

## One owner for a credential that rotates

Tesla refresh tokens are single-use. Every refresh returns a new refresh token and retires the old one, with a 24-hour grace window. Two processes holding the same login will each refresh, and within a day one of them is logged out. The server is therefore the only holder: the core is the only code that reads or writes the token file, a file lock serialises refreshes, and each new refresh token is saved before anything else can fail. Moving from a laptop to a host was a handover, not a copy: the laptop deleted its file the moment the server took ownership.

This is also why the host is one machine rather than a serverless platform. A platform that scales to zero has no persistent disk, so the token store would have to move to a database and the locking would have to be rebuilt. One machine with one volume keeps the single-owner guarantee simple enough to reason about.

## Identity is the perimeter, not the network

A firewall that admits known addresses cannot work here. claude.ai connects from Anthropic's cloud and Muse from Meta's, from addresses the owner does not control. The gate therefore checks who the caller is on every request, not where it comes from. The host exposes only HTTPS; every other port is closed.

GitHub sign-in alone is not enough. It proves that a caller is *some* GitHub user, not *the* owner. The allowlist is the second half of the gate, and it runs before tool listing, so a signed-in stranger learns nothing about what the server can do.

## The budget belongs to the caller, not the tool

A tool has one quota, but its callers carry different risks. If the budget is set on the tool, whichever caller loops first spends what the others needed later. The server keys its budget on caller identity. Each client is named by where its sign-in returns to, or by the metadata URL it identifies with. Supervised clients get the full wake budget; autonomous and unrecognised clients get a fraction. An unrecognised caller defaults to the strict budget, never the generous one.

Every cap is checked **before** a request leaves the server. A refused call costs nothing, because the refusal happens in front of the effect. A cap reported after the fact would only measure the damage. A refusal names the cap, the count and the reset time, so an agent can report it to its user instead of retrying.

## A record that names the caller and holds no payload

The audit log records the time, the GitHub account, the client, the tool, the arguments that matter (wake, location, a VIN masked to four characters), the outcome, and the Tesla requests and estimated cost. It also records every rejected sign-in and bad token. It deliberately records that location *was read*, never *where* the car was, and none of the car's data. A log that stores the payload becomes the most sensitive file on the server.

## What the build showed

The record caught two defects in the code that names callers. In both, the README and the code said callers were named, and only the audit log showed every call arriving as `unknown`:

| Defect | Cause | Fix |
| --- | --- | --- |
| Every caller logged as `unknown` | Behind the OAuth proxy, the server read the upstream GitHub token, which carries no client identifier | Name the client from the bearer token it actually presented |
| claude.ai still logged as `unknown` | claude.ai does not self-register; it identifies with a client ID metadata document URL, which the naming code did not recognise | A URL client identifier is named by its host. A client presenting the upstream app's own ID is named `unknown` rather than mistaken for a trusted local client |

Both defects failed safe, because an unnamed caller got the strict budget. That is also why they were invisible without the record: nothing broke, every client simply ran on the smallest allowance.

A third finding came from diagnosing a stuck connection with Muse. The server-side record showed Muse's registration succeeding, the authorise step never being opened for the registered client, and no authorisation code ever issued. The fault was in the client's flow, so no server change was made and the gate was not loosened to accommodate it.

## Terms and definitions

| Term | Definition | Source |
| --- | --- | --- |
| MCP | Model Context Protocol, a standard way for an AI client to discover and call tools a server exposes | modelcontextprotocol.io |
| Fleet API | Tesla's official, metered API for vehicle data and commands | developer.tesla.com |
| Refresh token | Credential exchanged for a new access token; single-use at Tesla | Tesla Fleet API authentication docs |
| Wake | Bringing an asleep or offline car online so it returns live data; costs about ten times a data read | Tesla Fleet API pricing |
| Dynamic client registration | A client registering itself with the server at connect time; used by Muse | OAuth 2.0 (RFC 7591) |
| Client ID metadata document | A client identifying itself with a URL it hosts instead of registering; used by claude.ai | MCP authorization specification |
| Allowlist | The set of GitHub accounts admitted after sign-in; here, one | This repository |

## What this means in practice

| Situation | Action |
| --- | --- |
| A credential rotates on use | Give it exactly one holder. Move it; never copy it. |
| Callers include autonomous agents | Key budgets on caller identity, and default unknown callers to the strictest one. |
| A cap protects money or privacy | Check it before the request leaves, and make the refusal say which cap and when it resets. |
| Agents read untrusted content | Keep the tool surface read-only unless a write is specifically needed, and test that it stays that way. |
| Callers come from clouds you do not control | Gate on identity, not network address. |
| You need to know what happened | Log the actor, the act and the outcome; never the payload. Then read the log: it is how stated intent gets checked against actual behaviour. |

## Assumptions

| Assumption | Reason it matters | Load-bearing |
| --- | --- | --- |
| A wake costs about \$0.02 and a data request about \$0.002 | Basis for the caps and the spend estimates | No — the caps are set well below Tesla's \$10 billing cap |
| Supercharger history covers Supercharger sessions only | Sets what the charging tool can answer | No |
| The host's single machine stays at one machine | The single-owner guarantee depends on it | Yes — `fly.toml` documents this and no workflow scales it |

**Audience:** engineers and technical readers evaluating how this server handles shared access by AI agents.

| Version | Date | Prepared by | Change |
| --- | --- | --- | --- |
| 1.0 | 2026-10-14 | Neeraj Singh | Initial, after Phase 3 went live with three clients |
