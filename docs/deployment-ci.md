# Gitea Actions 自动部署（可选）

本仓库自带 Gitea Actions 部署工作流（[`.gitea/workflows/deploy.yaml`](../.gitea/workflows/deploy.yaml)）：推送 `main` 分支后自动重建并更新 Docker 宿主机上的 `hwmon-web` 容器，无需额外配置。工作流内容如下。

```yaml
name: deploy-hwmon

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: deploy-hwmon
  cancel-in-progress: true

jobs:
  deploy:
    # 需要一台位于 Docker 宿主机上的 runner，
    # 且该 runner 带有 hwmon-host 标签。
    runs-on: hwmon-host
    steps:
      - name: Fetch code and rebuild container
        env:
          GIT_REPO_URL: ${{ secrets.GIT_REPO_URL }}
          DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}
          AGENT_URL: ${{ secrets.AGENT_URL }}
          AGENT_TOKEN: ${{ secrets.AGENT_TOKEN }}
          GIT_TERMINAL_PROMPT: "0"
        run: |
          set -euo pipefail
          WORKDIR="$(mktemp -d)"
          trap 'rm -rf "$WORKDIR"' EXIT

          # 1) 拉取 main 分支最新代码（GIT_REPO_URL 形如 http://host:port/owner/repo.git，不含凭据）
          if [ -z "${GIT_REPO_URL:-}" ]; then
            echo "缺少 Gitea Secret: GIT_REPO_URL（形如 http://<GITEA_HOST>:3000/<owner>/<repo>.git）" >&2
            exit 1
          fi
          # 诊断（不泄露 token）：确认 Secret 已配置
          echo "GIT_REPO_URL 已配置: [${GIT_REPO_URL}]"
          if [ -z "${DEPLOY_TOKEN:-}" ]; then
            echo "缺少 Gitea Secret: DEPLOY_TOKEN（可读该仓库的 Gitea token）" >&2
            exit 1
          fi
          # 把凭据注入 URL：http://host/... -> http://deploy:<token>@host/...
          # 不用字符串替换（各版本 bash 对 // 的处理不一致），用 %/: 拆分 + 拼接
          case "$GIT_REPO_URL" in
            http://*|https://*) ;;
            *) echo "GIT_REPO_URL 需为 http(s):// 开头" >&2; exit 1 ;;
          esac
          SCHEME="${GIT_REPO_URL%%://*}"
          HOST_PATH="${GIT_REPO_URL#*://}"
          GIT_REPO_URL="$SCHEME://deploy:$DEPLOY_TOKEN@$HOST_PATH"
          # 诊断（不泄露 token）：确认凭据已注入、scheme 正确
          echo "clone URL（token 已隐藏）: ${GIT_REPO_URL/deploy:*deploy:<token>@}"
          git clone --depth 1 --branch main "$GIT_REPO_URL" "$WORKDIR/src"

          # 2) 同步到部署目录；保留宿主机上的 .env，并兼容未安装 rsync 的机器
          DEPLOY_DIR=/opt/hwmon
          mkdir -p "$DEPLOY_DIR"
          if command -v rsync >/dev/null 2>&1; then
            rsync -a --delete \
              --exclude '.git' --exclude '.gitea' --exclude '.env' \
              --exclude '.venv' --exclude '__pycache__' \
              "$WORKDIR/src/" "$DEPLOY_DIR/"
          else
            echo "未找到 rsync，改用 find + cp 同步"
            find "$DEPLOY_DIR" -mindepth 1 -maxdepth 1 ! -name '.env' -exec rm -rf {} +
            cp -a "$WORKDIR/src/." "$DEPLOY_DIR/"
            rm -rf "$DEPLOY_DIR/.git" "$DEPLOY_DIR/.gitea"
          fi

          # 3) 用 Gitea Secrets 写入运行时配置；未配置 secrets 时沿用宿主机已有 .env
          if [ -n "${AGENT_URL:-}" ] && [ -n "${AGENT_TOKEN:-}" ]; then
            umask 077
            printf 'AGENT_URL=%s\nAGENT_TOKEN=%s\n' "$AGENT_URL" "$AGENT_TOKEN" > "$DEPLOY_DIR/.env"
          fi
          if [ ! -f "$DEPLOY_DIR/.env" ]; then
            echo "缺少 $DEPLOY_DIR/.env，请在仓库 Secrets 配置 AGENT_URL / AGENT_TOKEN" >&2
            exit 1
          fi

          # 4) 重建镜像并滚动更新容器
          cd "$DEPLOY_DIR"
          docker compose up -d --build
          docker image prune -f >/dev/null 2>&1 || true           # 旧的悬空镜像
          docker builder prune -f --filter until=168h >/dev/null 2>&1 || true   # 7 天前的构建缓存
          docker system df
          docker compose ps

      - name: Health check
        run: |
          set -e
          for i in $(seq 1 30); do
            status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' hwmon-web 2>/dev/null || echo missing)"
            if [ "$status" = "healthy" ] || curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
              echo "✅ hwmon-web 健康检查通过 (health=$status)"
              docker ps --filter name=hwmon-web --format '{{.Names}}  {{.Status}}'
              exit 0
            fi
            sleep 2
          done
          echo "❌ 健康检查失败，最近日志：" >&2
          docker logs --tail 60 hwmon-web 2>&1 || true
          exit 1
