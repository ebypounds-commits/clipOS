# Deploying ClipOS with Coolify

This guide deploys the **validated production topology**
(`deploy/compose.production.yml`, invariants H1..H8 enforced in CI) onto a
host that runs **Coolify 4.x**. Coolify supplies the edge (Traefik + TLS),
image builds, environment storage, restarts and the UI; the repository
supplies the stack shape, the health contract and the gates. Nothing in the
topology is replaced — only the edge service is swapped, via
`deploy/compose.coolify.yml`.

Everything below marked **owner act** is part of the human gates in
`13_HUMAN_GATES_AND_SECRETS.md` (production secrets, platform accounts,
first public post). Until those close, ClipOS runs in DRAFT with public
publishing disabled by default — that is the safe state this deployment
lands in.

## Verified host facts (this guide was written against them)

| Fact | Value |
| --- | --- |
| Coolify | `4.1.2`, healthy, UI on host port 8000 |
| Edge proxy | `coolify-proxy` (Traefik `v3.6`) publishing `80`, `443`, `8080` |
| Traefik docker provider | `exposedbydefault=false` — only labeled containers route |
| ACME | resolver `letsencrypt`, HTTP-01 on the `http` entrypoint |
| Coolify data | `/data/coolify/{applications,databases,images,proxy,backups}` |
| API image build context | `./services/api` (Dockerfile ships `alembic.ini` + `migrations/`) |
| Web image build context | `./apps/web` (Next.js, port 3000) |

If your host differs (older Coolify, no Traefik), re-verify the Traefik
labels and the ACME resolver name before continuing.

## What runs where

| Piece | Runs as | Notes |
| --- | --- | --- |
| `postgres`, `redis`, `minio`, `temporal`, `temporal-init` | compose services | `clipos_internal` network (`internal: true`), **no host ports** (H1) |
| `api`, `worker`, `web` | compose services | `clipos_edge` (+ `clipos_internal` for api/worker) |
| TLS + routing | **coolify-proxy (Traefik)** | replaces the stack's Caddy `edge` — see `deploy/compose.coolify.yml` |
| Images | registry (`ghcr.io`), pinned by digest | immutable, tagged by commit SHA (`11_CI_CD_DEPLOYMENT.md`) |

Do **not** deploy ClipOS as Coolify "Application" resources built ad hoc:
that shape is not the one CI validates. Use the Compose resource.

## Step 1 — Build and push immutable images (owner act for registry auth)

```bash
SHA=$(git rev-parse HEAD)

docker buildx build --platform linux/amd64 \
  -t ghcr.io/OWNER/clipos-api:${SHA} -f services/api/Dockerfile services/api --push
docker buildx build --platform linux/amd64 \
  -t ghcr.io/OWNER/clipos-web:${SHA} -f apps/web/Dockerfile apps/web --push

API_DIGEST=$(docker buildx imagetools inspect ghcr.io/OWNER/clipos-api:${SHA} --format '{{json .Manifest.Digest}}')
WEB_DIGEST=$(docker buildx imagetools inspect ghcr.io/OWNER/clipos-web:${SHA} --format '{{json .Manifest.Digest}}')
```

CI currently builds images for its own smoke/qualification runs but does not
push them; until a release workflow does, this step is manual. Images are
referenced by **digest** (rollback = re-pin, see below).

## Step 2 — Environment file (owner act)

```bash
cp deploy/env.production.example deploy/.env.production
# fill every empty value from the server secret manager (owner act)
```

Rules the repository already enforces: `deploy/.env.*` is git-ignored and
blocked by `scripts/secret_scan.sh`; the committed example contains names
only. In Coolify, enter the same variables on the Compose resource
(**Environment Variables**) — they interpolate into `${VAR}` references in
the compose file. Minimum set: `CLIPOS_API_IMAGE`, `CLIPOS_WEB_IMAGE`
(digests from Step 1), `CLIPOS_DOMAIN`, `POSTGRES_*`, `REDIS_PASSWORD`,
`DATABASE_URL`, `CACHE_REDIS_URL`, `MINIO_*`, `STORAGE_*`,
`TEMPORAL_*`, `PUBLIC_API_URL`, optional `JEV_*`.

## Step 3 — Coolify Compose resource

1. **New project** (e.g. `clipos`) → **+ New resource** → *Based on a
   Compose file* → point at this repository.
2. Compose file path:
   ```
   deploy/compose.production.yml
   deploy/compose.coolify.yml
   ```
   (one path per line — the override swaps the edge for Traefik)
3. Paste the environment variables from Step 2.
4. **Do not start yet.**

Staging is the same two steps with `deploy/compose.staging.yml` (its edge
binds `8080`, which also collides with `coolify-proxy` — the same override
applies cleanly).

## Step 4 — Attach the proxy to the stack network

Traefik routes to containers it can reach; it is not on the stack's edge
network by default. After the first `up`:

```bash
docker network connect clipos-production_clipos_edge coolify-proxy
```

Re-run this after any `docker compose down` (the network is recreated).
The routing contract then comes from the labels in
`deploy/compose.coolify.yml`, which reproduce `deploy/Caddyfile`
one-to-one: `/api/*` stripped to `api:8000`, `/health` to `api:8000`
unstripped, everything else to `web:3000`, all on `Host(${CLIPOS_DOMAIN})`
with `certresolver=letsencrypt`.

If you prefer Coolify-managed domains per service instead of the override
labels: you get **two hostnames** (e.g. `clipos…` → web, `api.clipos…` →
api) and must change `PUBLIC_API_URL` accordingly. The single-hostname
path-based contract in this repository is what the override preserves.

## Step 5 — Migrate (post-deploy)

The API image ships its migration chain (`alembic.ini`, `migrations/`;
verified: `python3 -m alembic heads` → current head). Run the schema step
once the api container is healthy:

```bash
docker exec "$(docker compose -f deploy/compose.production.yml ps -q api)" \
  python3 -m alembic upgrade head
```

Migrations are expand/contract (`11_CI_CD_DEPLOYMENT.md`): application
rollback never needs a schema rollback. If your Coolify version offers a
per-service *post-deploy command*, use the same command there.

## Step 6 — Verify (gates, not vibes)

```bash
# 1. topology still matches what CI validates
python3 scripts/deploy_topology_check.py --strict

# 2. health
curl -fsS https://${CLIPOS_DOMAIN}/health

# 3. the production qualification harnesses (safe: every adapter is a
#    deterministic stand-in until the human gates open, and DRAFT mode
#    blocks public publishing by default - the harnesses only exercise
#    DENIED paths and local state)
python3 scripts/security_qualification.py --base-url https://${CLIPOS_DOMAIN}
python3 scripts/private_mode_qualification.py --base-url https://${CLIPOS_DOMAIN}
```

All three must pass before the deployment is considered live. Evidence to
record (`11_CI_CD_DEPLOYMENT.md`, release evidence): commit SHA, image
digests, migration head reached, CI run URL, harness verdicts.

## Rollback

Images are immutable and digest-pinned. To roll back: set
`CLIPOS_API_IMAGE` / `CLIPOS_WEB_IMAGE` in Coolify to the previous
digests and redeploy — that is the one-command application rollback the
spec requires. Restoring a database backup is disaster recovery, not
rollback.

## Secrets, gates and capacity

- **production_secrets** (owner act): fill Step 2 from the secret manager;
  treat Coolify's environment storage as the injection point and restrict
  who can read the project.
- **youtube/tiktok/meta real accounts + first_public_post** (owner acts):
  unchanged by deployment; until they close, adapters report
  `adapter_kind: "mock"` on every response (W2) so nothing can be mistaken
  for a real publication.
- **Backups**: Coolify's scheduled-backup UI covers Coolify-managed
  databases, not services inside a compose file. Use the runbook backup
  script (`12_OPERATIONS_RUNBOOK.md`) against the `postgres_data` volume
  and schedule it (the P1-2 item in `docs/BACKLOG.md`).
- **Capacity**: the compose file enforces cpu/memory limits (H6). Check
  them against your host before `up`, especially if the development stack
  (`docker-compose.dev.yml`) or other Coolify projects share the box —
  both can coexist (production publishes no host ports), but the limits
  are real allocations.

## What protects this deployment in CI

| Gate | Where | What it proves |
| --- | --- | --- |
| `deploy_topology_check.py` | validate job | H1..H8 on `compose.production.yml` + `compose.staging.yml`, no drift between them |
| `security_qualification.py` / `private_mode_qualification.py` | test job (live API) | rights gate, DRAFT policy, secret boundary, audit trail |
| `schema_drift.py` | test job | models ↔ migrated schema, autoincrement defaults |
| `dependency_audit.sh` + npm audit | quality job | no known CVEs in Python or web dependencies |
| `autonomy_evidence.py` | validate job | build state, ledger coherence, CI record health |
