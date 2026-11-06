# Web client image (Debian-based default).
#
# uv installs the pinned interpreter and the locked dependency set, so the
# container runs exactly what CI verified rather than whatever the base image
# happens to ship. Use packaging/docker/Dockerfile.rocky{8,9} for RHEL targets.
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
 && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /src

# Dependency layer first: only the lock inputs, so source edits reuse the cache.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY VERSION README.md LICENSE ./

# Third and final sync installs the project itself on top of the cached deps.
RUN uv sync --frozen --no-dev \
 && useradd --system --no-create-home --uid 997 hwmon

USER hwmon
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD /opt/hwmon/venv/bin/python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["hwmon", "web"]
