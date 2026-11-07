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

### What the licensing check can and cannot do

The licence check runs on the customer's own machine, so it is a **detection
mechanism, not an enforcement one**. Anything a process can do locally, a
sufficiently determined local operator can also do: patch the check out,
short-circuit the import, or attach a debugger. No amount of client-side
obfuscation changes that, and Cython is not claimed to.

This is stated deliberately rather than left implied, because the alternative
reads as an overpromise. What the check actually buys:

* **Honest-use compliance.** The overwhelming majority of customers act in good
  faith; a check that is easy to run and hard to "accidentally" skip is enough.
* **Contractual evidence.** A signed licence bound to a machine id is
  attributable proof of entitlement, which is what an audit or a procurement
  review asks for.
* **Tamper visibility.** Licence JSON is signed; edits are detected, not ignored.

What it does not do is stop a hostile operator. Preventing that requires the
algorithm or the entitlement decision to live somewhere the operator does not
control — a server-side component or hardware root of trust — which is a
different architecture with its own availability and privacy trade-offs. This
project does not have one, and does not pretend to.

The same caution applies to compiling `pro/`: it raises the cost of
reverse-engineering a proprietary algorithm. It is not protection against a
determined attacker, and describing it as such would be exactly the kind of
claim that fails a security review.

### SBOM and artefact integrity

Supply-chain expectations for a packaged product are: an inventory of what is
inside, a checksum for each artefact, and a signature over the checksum. This
repository produces the first two and verifies the third:

| Artefact | Produced by | Where |
|---|---|---|
| SBOM (SPDX JSON + CycloneDX) | `syft`, in `packaging/sbom.sh` | attached to the GitHub release |
| `SHA256SUMS` over every artefact | `sha256sum`, in the same script | attached to the release |
| `SBOM.sha256` | same | proves the SBOM was not swapped |
| Detached Ed25519 signature per artefact | `hwmon sign` | `*.sig` next to each file |

Signing covers the checksum file as well as the artefacts, so a consumer can
verify one signature and then trust the manifest. `hwmon verify` is the check
side; it fails closed (see
[troubleshooting-packaging.md](docs/troubleshooting-packaging.md#4-licence-verification-failures)
for the error-to-cause table).

The SBOM is generated from the **built image and the wheel**, not from
`pyproject.toml`: the point is to describe what actually ships, including the
CPython runtime that uv installs inside the image.

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
