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

### Why the licence verifier is not compiled

The pro tier ships as compiled extensions; `licensing/verify.py` deliberately
does not. This is a design decision, not a packaging oversight, and it follows
from Kerckhoffs's principle: a verification system's security must rest on the
secrecy of the **key**, never on the secrecy of the algorithm.

Concretely, for this project:

* **The secret is the private key, and it never reaches a customer.** The
  verifier only embeds the *public* key. Publishing the verification logic
  therefore removes no security — an attacker who can read it still cannot
  forge a signature without the private key.
* **Obfuscation here would only hide behaviour, not protect a secret.** That is
  the worst trade in this space: a customer cannot audit what the check
  actually inspects, whether it reports anything back, or whether it behaves as
  documented. For buyers with supply-chain review obligations — semiconductor
  and EDA teams in particular — an unauditable licence check is a reason to
  reject the product, not a reason to trust it.
* **What compilation is for is different.** `pro/history.py` and
  `pro/alerts.py` are compiled to protect commercial value: the algorithms are
  the product. Protecting business logic and protecting a security mechanism
  are separate goals achieved by separate means, and conflating them produces a
  system that is neither auditable nor meaningfully harder to reverse.

The verifier is also not imported by the runtime: nothing under `src/hwmon/`
references `licensing/`. It is an independent library used by
`licensing/issue.py` and the test suite, so compiling it would not even be
"free" — it would ship a native artefact that no runtime path exercises.

If a future tier genuinely needs the check itself obscured, that is a different
product with a different threat model, and it should be argued explicitly rather
than switched on by default. `packaging/build_ext.py` can compile it
(`--modules licensing/verify.py`); the default list is what expresses the
decision.

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
