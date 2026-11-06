# Optional: Gitea Actions auto-deploy

This repository ships a Gitea Actions workflow
([`.gitea/workflows/deploy.yaml`](../.gitea/workflows/deploy.yaml)) that rebuilds
and rolls the `hwmon-web` container on the Docker host on every push to `main`.

> **GitHub note:** GitHub does not read `.gitea/workflows/`, so this workflow
> never runs there. The GitHub side uses
> [`.github/workflows/ci.yml`](../.github/workflows/ci.yml), which only builds and
> smoke-tests the image — it does not deploy. Keeping both remotes is described
> in [README → Two remotes](../README.md#two-remotes-gitea--github).

## What it needs

| Requirement | Why |
|---|---|
| A self-hosted runner **on the Docker host**, labelled `hwmon-host` | The job runs `docker compose` locally and must reach the intranet |
| Secret `GIT_REPO_URL` | `http://<GITEA_HOST>:3000/<owner>/<repo>.git`, **without** credentials |
| Secret `DEPLOY_TOKEN` | A Gitea token that can read the repository; injected into the clone URL |
| Secret `AGENT_URL` | Address the web container uses to reach the agent |
| Secret `AGENT_TOKEN` | Shared token between web and agent |

Deploy directory is `/opt/hwmon`; the container is `hwmon-web` and listens on
`127.0.0.1:8080` (published as `8080:8080`).

## Manual fallback

```bash
sudo git clone http://<GITEA_HOST>:3000/<owner>/<repo>.git /opt/hwmon
cd /opt/hwmon
cp .env.example .env      # set AGENT_URL and AGENT_TOKEN
docker compose up -d --build
```

The build version is `major.minor` from [`VERSION`](../VERSION) plus the total
commit count, e.g. `1.0.123`. CI writes it into `.env` as `HWMON_VERSION`; for
manual deploys:

```bash
HWMON_VERSION="$(cut -d. -f1,2 VERSION).$(git rev-list --count HEAD)"
```

The page shows the web build version at the bottom right and highlights it in
amber when the agent reports a different version (agent not yet updated).

## Workflow source

The job is a single `runs-on: hwmon-host` step that:

1. clones `main` (full clone, not `--depth 1`, because the build number is the
   total commit count),
2. rsyncs the tree into `/opt/hwmon` while preserving the host's `.env`
   (falls back to `find` + `cp` when rsync is absent),
3. writes `AGENT_URL` / `AGENT_TOKEN` / `HWMON_VERSION` from secrets,
4. runs `docker compose up -d --build`, then prunes dangling images and build
   cache older than 7 days,
5. polls `docker inspect` health (or `/healthz`) for up to 60 s and dumps the
   last 60 log lines on failure.

See the commented YAML itself for the exact commands; it is the single source
of truth and is not duplicated here.
