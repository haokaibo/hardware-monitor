%global __pythondist_requires %{nil}
# RPM packaging for the hwmon agent.
#
# The target hosts (Rocky 8/9) ship Python 3.6/3.9, below the project floor, so
# this spec bundles a pinned interpreter installed by uv under /opt/hwmon and
# points the systemd unit at that venv. Nothing is installed into the system
# Python, so the package cannot conflict with distro tooling.
#
# Build (after `make dist`, which produces the sdist):
#   rpmbuild -ta hwmon-1.0.0.tar.gz
#
# Install:
#   dnf install ./hwmon-1.0.0-1.el9.x86_64.rpm
#   echo "AGENT_TOKEN=$(openssl rand -hex 16)" > /etc/hwmon/agent.env
#   systemctl enable --now hwmon-agent

Name:           hwmon
Version:        1.0.0
Release:        1%{?dist}
Summary:        Lightweight hardware monitor agent
License:        MIT
URL:            https://github.com/%{name}/%{name}
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       systemd
Requires:       kmod
# hwmon-setup provisions the interpreter and venv with uv. Declared so a missing
# uv is a dependency error, not a runtime surprise in %post.
Requires:       uv

# The pro extensions are compiled against one CPython minor version. Keep this
# in lockstep with .python-version; the %build step aborts if they disagree.
# (3.12 matches the interpreter the Docker images and CI use.)
%{!?pyver: %global pyver 3.12}
BuildRequires:  python%{pyver}

%description
hwmon samples CPU, memory, AMD GPU and disk metrics from /proc and /sys once
per second and serves them as JSON on port 9101 for the web dashboard.

The agent runs as root because reading hwmon nodes and SMART data requires it.
It exposes aggregate metrics only, never file contents.

%prep
%setup -q

%build
# Compile the pro tier to native extensions here, in %%build, so the artifact is
# produced once and %install only copies it. Doing the Cython work at install
# time would need a compiler on the target host, which a "no build tools on
# production" policy forbids.

# The ABI tag of the output is that of the interpreter used HERE. RPM must
# therefore be built with the same minor version the target runs: see the
# %{pyver} check below. A mismatch does not produce a link error, it produces a
# file the target interpreter cannot even see (ModuleNotFoundError).
#
# The buildroot interpreter is provisioned by uv, the same way the runtime venv
# is, so the extension is compiled against the exact CPython the package
# installs. Without uv on the build host this step fails loudly rather than
# silently emitting untagged source-only output.
BUILD_PY=%{_builddir}/%{name}-%{version}/.buildvenv/bin/python
uv venv --python %{pyver} --seed .buildvenv
uv pip install --python "$BUILD_PY" cython setuptools
"$BUILD_PY" packaging/build_ext.py --out cython-out

# Record what was built so %install can assert it matches the package's
# declared interpreter instead of trusting the build host's default python3.
"$BUILD_PY" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' > cython-out/interpreter.txt
echo "compiled extensions for: $(cat cython-out/interpreter.txt)"
find cython-out -name '*.so' -printf '  %f\n'

%install
install -d %{buildroot}%{_datadir}/%{name} %{buildroot}%{_sysconfdir}/%{name}
install -d %{buildroot}%{_unitdir} %{buildroot}%{_sbindir}

# The open tiers ship as source; only the pro tier ships compiled. That is the
# product boundary, and it is enforced by what %install copies, not by a promise
# in the docs.
cp -a src %{buildroot}%{_datadir}/%{name}/
rm -rf %{buildroot}%{_datadir}/%{name}/src/hwmon/pro
install -d %{buildroot}%{_datadir}/%{name}/src/hwmon/pro
install -m 0644 cython-out/hwmon/pro/*.so %{buildroot}%{_datadir}/%{name}/src/hwmon/pro/

# The compiled pro modules import `from .. import contract`, so the shim and the
# contract module must sit beside them; a .so alone is not importable.
install -m 0644 src/hwmon/pro/__init__.py %{buildroot}%{_datadir}/%{name}/src/hwmon/pro/
install -m 0644 src/hwmon/contract.py %{buildroot}%{_datadir}/%{name}/src/hwmon/

# A pro package without its extensions is the failure this guards against: it
# installs cleanly and breaks only when someone enables the pro features.
test -n "$(ls %{buildroot}%{_datadir}/%{name}/src/hwmon/pro/*.so 2>/dev/null)" || {
  echo "ERROR: no compiled pro extensions were installed" >&2; exit 1; }
build_ver="$(cat cython-out/interpreter.txt)"
[ "$build_ver" = "%{pyver}" ] || {
  echo "ERROR: extensions built for python $build_ver but the package declares %{pyver}" >&2
  exit 1; }

cp -a pyproject.toml uv.lock .python-version %{buildroot}%{_datadir}/%{name}/
echo %{version} > %{buildroot}%{_datadir}/%{name}/VERSION


cat > %{buildroot}%{_unitdir}/hwmon-agent.service <<'UNIT'
[Unit]
Description=hwmon hardware monitor agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/hwmon/agent.env
ExecStart=/opt/hwmon/venv/bin/hwmon agent
Restart=always
RestartSec=5
# The agent needs /proc and /sys; no other privileges are required.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadOnlyPaths=/
ReadWritePaths=/var/lib/hwmon

[Install]
WantedBy=multi-user.target
UNIT

install -m 0755 /dev/stdin %{buildroot}%{_sbindir}/hwmon-setup <<'SETUP'
#!/bin/sh
# Provision the pinned interpreter and the runtime venv.
#
# Two modes, because enterprise hosts are frequently air-gapped:
#
#   online  (default)  uv python install + uv sync, fetching from the internet
#   offline            install from a bundle prepared on a connected machine
#
# The offline bundle layout (built by packaging/offline-bundle.sh):
#
#   /opt/hwmon-offline/
#     cpython-<version>-<platform>/   a uv-managed interpreter, pre-downloaded
#     wheels/                         every wheel the runtime needs
#     bundle.txt                      interpreter version + SHA256SUMS
#
# Selecting a mode: set HWMON_OFFLINE=1 in /etc/hwmon/hwmon-setup.conf, or pass
# --offline. The installer never silently falls back to the network: if the
# bundle is requested and unusable it fails with an actionable message, because
# a systemd service that cannot start is easier to diagnose than one that
# half-installed.
set -eu

DATA=/usr/share/hwmon
CONF=/etc/hwmon/hwmon-setup.conf
BUNDLE=/opt/hwmon-offline
PYVER=$(cat "$DATA/.python-version")

MODE=online
[ -f "$CONF" ] && . "$CONF"
[ "${1:-}" = "--offline" ] && MODE=offline
[ "${HWMON_OFFLINE:-0}" = "1" ] && MODE=offline

UV=$(command -v uv 2>/dev/null || true)
[ -n "$UV" ] || [ -x /usr/local/bin/uv ] && UV=/usr/local/bin/uv

export UV_PYTHON_INSTALL_DIR=/opt/uv/python
export UV_PROJECT_ENVIRONMENT=/opt/hwmon/venv
export UV_CACHE_DIR=/opt/hwmon/.uv-cache

fail() { echo "hwmon-setup: $*" >&2; exit 1; }

if [ "$MODE" = offline ]; then
  echo "hwmon-setup: offline mode, using $BUNDLE"
  [ -d "$BUNDLE" ] || fail "no bundle at $BUNDLE. Build one on a connected host:
    packaging/offline-bundle.sh --python $PYVER --out /media/usb/hwmon-offline
  then copy it to $BUNDLE on this host."

  # Integrity first: a truncated copy is the most common failure and it looks
  # like a corrupt wheel further down.
  if [ -f "$BUNDLE/SHA256SUMS" ]; then
    # Plain `-c`: --quiet is a GNU extension and BusyBox's sha256sum rejects it,
    # so the check would fail on a minimal host for the wrong reason. Output is
    # thousands of "OK" lines; keep the tail only when it fails.
    if ! ( cd "$BUNDLE" && sha256sum -c SHA256SUMS >/tmp/hwmon-verify.log 2>&1 ); then
      grep -v ': OK$' /tmp/hwmon-verify.log | head -5 >&2 || true
      fail "bundle checksum mismatch in $BUNDLE (see /tmp/hwmon-verify.log)"
    fi
    rm -f /tmp/hwmon-verify.log
  else
    fail "$BUNDLE/SHA256SUMS missing; refusing to install unverified wheels"
  fi

  # Interpreter: copy the pre-downloaded managed CPython into uv's directory.
  PYDIR=$(ls -d "$BUNDLE"/cpython-"$PYVER"* 2>/dev/null | head -1)
  [ -n "$PYDIR" ] || fail "bundle has no interpreter for $PYVER (looked for $BUNDLE/cpython-$PYVER*)"
  install -d "$UV_PYTHON_INSTALL_DIR"
  cp -a "$PYDIR" "$UV_PYTHON_INSTALL_DIR"/

  # Wheels: --offline forbids any network access; if a wheel is missing uv says
  # which one, which is exactly the message the operator needs.
  [ -n "$UV" ] || fail "uv not found; the RPM Requires it. Install the uv RPM or place it in /usr/local/bin."
  "$UV" venv --python "$PYVER" "$UV_PROJECT_ENVIRONMENT" --allow-existing
  "$UV" pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" \
        --offline --no-index --find-links "$BUNDLE/wheels" \
        "hwmon==${HWMON_VERSION:-$(cat "$DATA/VERSION")}" \
    || fail "wheel install failed. Verify the bundle contains every file from uv.lock.
    Rebuild it with: packaging/offline-bundle.sh"
  echo "hwmon-setup: offline install complete"
else
  [ -n "$UV" ] || fail "uv not found; install it or run: hwmon-setup --offline"
  "$UV" python install "$PYVER"
  ( cd "$DATA" && "$UV" sync --frozen --no-dev )
  echo "hwmon-setup: online install complete"
fi

chmod -R a+rX /opt/hwmon/venv
install -d -m 0700 /var/lib/hwmon
echo "hwmon installed. Configure /etc/hwmon/agent.env then: systemctl enable --now hwmon-agent"
SETUP

%post
install -d -m 0700 /var/lib/hwmon
if [ ! -f %{_sysconfdir}/%{name}/agent.env ]; then
  umask 077
  printf 'AGENT_TOKEN=\nAGENT_LABEL=%s\n' "$(uname -n)" > %{_sysconfdir}/%{name}/agent.env
fi
# The service cannot start without a provisioned venv, so a failure here is
# reported loudly instead of letting systemd fail later with a confusing
# "no such file" on the ExecStart path. %post must not abort the transaction
# (the files are installed correctly); it prints the exact command to retry.
if ! %{_sbindir}/hwmon-setup; then
  cat >&2 <<'MSG'

  hwmon: runtime provisioning FAILED - the service will not start yet.

  Online host:   hwmon-setup
  Air-gapped:    packaging/offline-bundle.sh on a connected machine, copy the
                 bundle to /opt/hwmon-offline, then: hwmon-setup --offline

  Then:          systemctl enable --now hwmon-agent

MSG
fi
%systemd_post hwmon-agent.service

%preun
%systemd_preun hwmon-agent.service

%postun
%systemd_postun_with_restart hwmon-agent.service
rm -rf /opt/uv/python /opt/hwmon

%files
%doc README.md LICENSE
%{_datadir}/%{name}
%{_sbindir}/hwmon-setup
%dir %{_sysconfdir}/%{name}
%config(noreplace) %{_sysconfdir}/%{name}/agent.env
%{_unitdir}/hwmon-agent.service
%dir /var/lib/hwmon

%changelog
* Thu Nov 06 2025 Maintainer <maintainer@example.invalid> - 1.0.0-1
- Initial packaging: agent-only, pinned Python via uv, systemd unit.
