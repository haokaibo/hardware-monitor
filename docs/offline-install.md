# Offline / air-gapped installation

The RPM needs a CPython 3.12 and four wheels that are not in any distribution
repository. On a connected host `hwmon-setup` fetches them; on an air-gapped
host it installs from a **bundle** prepared elsewhere.

Verified end to end in a container with `--network none`: bundle built on a
connected host, copied in, `hwmon-setup --offline` installs and the agent runs.

## Why this exists

Rocky 8 ships Python 3.6 and Rocky 9 ships 3.9, both below the project floor, so
the interpreter is always provisioned rather than reused. That makes network
access a hard requirement unless the payload travels with the package — which is
what the bundle is for. The RPM `%post` no longer pretends otherwise: a failed
provisioning prints the exact retry command instead of leaving a service that
cannot start.

## Building the bundle (connected host)

Same architecture as the target. A bundle built on x86_64 cannot serve aarch64:
the interpreter and every wheel are architecture specific.

```bash
packaging/offline-bundle.sh --python 3.12 --out /media/usb/hwmon-offline
```

Produces:

```
hwmon-offline/
  cpython-3.12-linux-x86_64-gnu/   standalone interpreter (real files, not a symlink)
  wheels/                          every wheel the runtime imports
  bundle.txt                       interpreter, platform, uv.lock digest, build time
  SHA256SUMS                       ~3.5k files, verified before install
```

`wheels/` is derived from `uv export --frozen --no-dev`, so the bundle matches
`uv.lock` exactly. A hand-written list would drift and an offline install would
quietly differ from a connected one.

Check `bundle.txt` records the same `uv_lock_sha256` as the release you are
deploying; that is how you confirm the bundle belongs to this version.

## Installing on the target

```bash
# 1. the RPM itself, plus its dependencies, from your local mirror
dnf install ./hwmon-1.0.0-1.el9.aarch64.rpm

# 2. copy the bundle into place
sudo cp -a /media/usb/hwmon-offline /opt/hwmon-offline

# 3. install from it
sudo hwmon-setup --offline

# 4. configure and start
sudo vi /etc/hwmon/agent.env        # AGENT_TOKEN, AGENT_LABEL
sudo systemctl enable --now hwmon-agent
```

To make offline the permanent mode:

```bash
echo 'HWMON_OFFLINE=1' | sudo tee /etc/hwmon/hwmon-setup.conf
```

Without that file, `%post` during an RPM install runs in online mode and will
fail on an air-gapped host (by design — it reports the failure and the retry
command rather than hanging on a network timeout).

## Prerequisite: `uv`

The RPM declares `Requires: uv`, so a missing binary is a dependency error
rather than a surprise inside `%post`. If your mirror does not carry a `uv` RPM,
place the static binary at `/usr/local/bin/uv` (single-file, no dependencies)
and install the hwmon RPM with `--nodeps` for that one dependency, or vendor it
into your own wrapper package.

## What can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| `no bundle at /opt/hwmon-offline` | bundle not copied, or copied elsewhere | copy it, or point `HWMON_OFFLINE_DIR` at it |
| `bundle checksum mismatch` | truncated copy, USB unplugged mid-copy | re-copy; the failing files are printed |
| `bundle has no interpreter for 3.12` | architecture or version mismatch | rebuild on a host with the same CPU arch |
| `wheel install failed` | bundle incomplete (built with `--no-dev` but a runtime dep missing) | rebuild the bundle; compare `bundle.txt` `uv_lock_sha256` |
| `uv not found` | `uv` RPM not installed | see the prerequisite above |
| Service starts then exits | `/etc/hwmon/agent.env` has an empty `AGENT_TOKEN` and the agent is reachable from the LAN | set a token (see SECURITY.md) |

The checksum step reads every file in the bundle, so it catches a partial copy
before any wheel is installed — a failure here never leaves a half-installed
venv.

## Verifying the artefacts themselves

The bundle is only as trustworthy as the release it came from. Verify before
copying anything onto the target:

```bash
# on the machine that has the release
uv run hwmon verify dist/SHA256SUMS -k release-public.pem
sha256sum -c dist/SHA256SUMS
```

See [troubleshooting-packaging.md](troubleshooting-packaging.md#4-licence-verification-failures)
for the failure messages, and SECURITY.md for what the SBOM and signing cover.
