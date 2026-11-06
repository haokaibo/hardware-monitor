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