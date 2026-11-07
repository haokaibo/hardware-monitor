# Web client image (Debian-based default).
#
# uv installs the pinned interpreter and the locked dependency set, so the
# container runs exactly what CI verified rather than whatever the base image
# happens to ship. Use packaging/docker/Dockerfile.web-rocky{8,9} for RHEL targets.
#
#   docker build -t hwmon-web .
FROM python:3.12-slim

ENV PYTHON_VERSION=3.12 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/hwmon/venv \
    PATH=/opt/hwmon/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 # not `mv /root/...`: HOME may not be /root (GitHub's container: override
 # uses /github/home), so resolve the binary instead of assuming a path
 && install -m 0755 "$(command -v uv || echo "$HOME/.local/bin/uv")" /usr/local/bin/uv

WORKDIR /src

# Dependency layer first: only the lock inputs, so source edits reuse the cache.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY VERSION README.md LICENSE ./

# Third and final sync installs the project itself on top of the cached deps.
#
# --no-editable matters: uv's default for the project under build is an editable
# install, which drops a .pth pointing at /src/src. That works only while the
# sources stay in the image, so it silently breaks the moment they are stripped
# (and stripping them is required to keep the runtime image minimal). A real
# install copies the package into site-packages.
#
# The chmod is not cosmetic: files created with a restrictive umask (0600) reach
# the image as owner-only, and the container runs as an unprivileged user, so
# index.html would be unreadable and "/" would 404 while /healthz stayed green.
# That failure only shows up at request time, never at build time.
RUN uv sync --frozen --no-dev --no-editable \
 && chmod -R a+rX /src/src \
 && useradd --system --no-create-home --uid 997 hwmon

# Strip build residue from the runtime image. Without this the image carries the
# whole uv download cache (~22 MB of wheels, vendored setuptools copies and
# build-isolation environments) plus the project sources. Both are pure attack
# surface for a container that only ever runs `hwmon web`, and they make the
# SBOM report dozens of packages the runtime never imports - the inventory
# should describe what is actually used, not what was used to build it.
RUN rm -rf /root/.cache /root/.local /src/src /src/packaging \
 && rm -f /src/pyproject.toml /src/uv.lock /src/README.md /src/LICENSE

# Keep only what the entrypoint needs: the venv and the version string.
WORKDIR /opt/hwmon

USER hwmon
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD /opt/hwmon/venv/bin/python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["hwmon", "web"]
