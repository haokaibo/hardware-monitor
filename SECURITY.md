# Security policy

## Scope: this project is designed for a trusted intranet

`hwmon` is a self-hosted monitoring tool with **no authentication**, **no TLS**
and **no user accounts**. It assumes the agent and the dashboard live on a
network you already control. Read this before you deploy it.

### What is deliberately absent

| Not included | Consequence |
|---|---|
| TLS / HTTPS | Traffic is plaintext; anyone who can sniff the LAN sees metrics |
| Login / session auth | Anyone who can reach port 8080 sees the dashboard |
| User accounts or roles | There is no notion of "who did what" |
| Rate limiting / WAF | Nothing throttles a hostile client |

The only credential is the shared `AGENT_TOKEN` between agent and web. It
authenticates the web container to the agent — it is **not** a user login and
does not protect the dashboard itself.

### Required hardening when deploying

1. Never expose ports `8080` or `9101` to the public internet.
2. Firewall the agent port so only the web host can reach it, e.g.
   `ufw allow from <WEB_HOST_IP> to any port 9101`.
3. Set a strong `AGENT_TOKEN` (`openssl rand -hex 16`). An empty token on the
   agent disables the check entirely — only acceptable on a single-host setup.
4. For remote access, put the **web** container behind a VPN or a TLS reverse
   proxy (nginx/caddy). Do not expose the agent through a proxy.
5. Keep `.env` out of version control. It is gitignored; verify with
   `git check-ignore -v .env`.

### Signing keys and licences

Release artefacts are signed with **Ed25519** and verified with
`hwmon verify` before installation; verification fails closed. The private
signing key must never be committed, stored in CI secrets that a fork could
reach, or kept on a machine that runs the agent. `*.pem`, `*.key`, `license.json`
and `*.sig` are gitignored — verify with `git check-ignore -v <file>`.

Licence files (`licensing/issue.py` → `licensing/verify.py`) are signed with the
same primitives and bound to a machine identifier. A leaked licence lets someone
run the pro tier; it does not grant access to the dashboard.

`cryptography` is imported only by `hwmon/release.py` and `licensing/`. The
collectors and the HTTP server do not depend on it.

### What the agent can read

The agent runs as root and reads `/proc`, `/sys` and `hwmon` nodes. It exposes
**aggregate** hardware metrics only — usage, temperatures, clocks, capacity and
model strings. It does not read user files, process command lines, or network
payloads. Mount names are reported (e.g. `/mnt/data`), so treat the metrics
endpoint as revealing your storage layout.

`/api/metrics` on the agent requires the token header `X-Token` when
`AGENT_TOKEN` is set. `/healthz` and `/api/info` never require a token and leak
no host data beyond the build version.

## Reporting a vulnerability

This is a personal, best-effort project with no security SLA. If you find a
problem, open a **private** security advisory on GitHub (Security → Advisories →
Report a vulnerability) rather than a public issue, and include:

- affected file/endpoint and the exact request or payload,
- what an attacker gains,
- whether it requires network access beyond the intranet assumption.

Please allow reasonable time for a fix before disclosing publicly.

## Out of scope

- "The dashboard is reachable over plain HTTP" — by design, intranet only.
- "There is no login" — by design. Use a VPN or reverse proxy.
- Denial of service from an actor already on your LAN.
- Metrics accuracy questions; those belong in a regular issue.
