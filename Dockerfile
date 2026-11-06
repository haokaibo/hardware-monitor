# Container runtime: 3.12-slim. Pinned on purpose — the web client only needs the
# stdlib, so any 3.10+ would work; 3.12 matches the newest Ubuntu LTS Python and
# is verified by CI. The agent on the host runs on whatever `python3` the distro
# ships (3.10+), which is why the code must stay free of newer syntax.
FROM python:3.12-slim
WORKDIR /app
# --chmod=0644 rewrites file metadata so the layer never hits the cache:
# every deploy build copies in the current files
COPY --chmod=0644 app.py index.html favicon.svg ./
USER nobody
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python", "app.py", "web"]