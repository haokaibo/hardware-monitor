FROM python:3.12-slim
WORKDIR /app
# --chmod=0644 会改写文件元数据使层无法命中缓存, 部署时每次 build 都能拷入最新文件
COPY --chmod=0644 app.py index.html favicon.svg ./
USER nobody
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["python", "app.py", "web"]