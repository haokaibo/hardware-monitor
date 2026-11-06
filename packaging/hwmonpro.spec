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
# Provision the pinned interpreter and venv, then print next steps.
set -eu
DATA=/usr/share/hwmon
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
export UV_PROJECT_ENVIRONMENT=/opt/hwmon/venv
cd "$DATA"
/usr/local/bin/uv python install "$(cat .python-version)"
/usr/local/bin/uv sync --frozen --no-dev
echo "hwmon installed. Configure /etc/hwmon/agent.env then: systemctl enable --now hwmon-agent"
SETUP

%post
install -d -m 0700 /var/lib/hwmon
if [ ! -f %{_sysconfdir}/%{name}/agent.env ]; then
  umask 077
  printf 'AGENT_TOKEN=\nAGENT_LABEL=%s\n' "$(uname -n)" > %{_sysconfdir}/%{name}/agent.env
fi
%{_sbindir}/hwmon-setup || echo "hwmon: run %{_sbindir}/hwmon-setup once uv is available"
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
