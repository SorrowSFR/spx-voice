# SPX Voice

SPX Voice is an open-source voice AI platform for building and deploying
conversational agents with Pipecat, LiveKit, telephony, WebRTC, and a
workflow-builder dashboard.

Base application version: `1.31.0`

This edition keeps the FastAPI backend, Next.js dashboard, workflow builder,
Postgres/Redis/MinIO storage stack, and self-hosted deployment model, then adds
the LiveKit voice runtime and Vobiz SIP provisioning flow used by this branch.

SPX Voice intentionally keeps some upstream upstream-compatible internals so
future security fixes and feature updates can be imported without a painful
rewrite. See `docs/developer/upstream-compatibility.md` before doing
large renames or upstream merges.

## What Is Included

- Next.js dashboard for creating and running voice-agent workflows.
- FastAPI backend with async SQLAlchemy, ARQ workers, Redis, Postgres, and MinIO.
- Local email/password auth with first-user bootstrap superadmin.
- Single-organization mode enabled by default for one-business deployments.
- Pipecat runtime as the default voice runtime.
- Optional LiveKit runtime for browser voice tests and SIP calls.
- Vobiz telephony configuration and LiveKit SIP setup helpers.
- Hosted cloud services and telemetry disabled by default.

## Quick Install

Use this path when you want to run this exact checkout with Docker. It mounts
the local `api/` and `ui/` source into containers, so local changes are the code
that runs.

### Requirements

- Docker Desktop or Docker Engine with Docker Compose v2.
- Git.
- Free local ports: `3010`, `8000`, `5432`, `6379`, `9000`, `9001`, and `2000`.

### Start The Stack

```bash
git clone <this-repository-url>
cd <repo-directory>
bash start.sh
```

PowerShell:

```powershell
git clone <this-repository-url>
cd <repo-directory>
.\start.ps1
```

The launcher checks for Docker Compose v2, initializes submodules, creates
`.env`, `api/.env`, and `ui/.env` from their examples when missing, then starts
the Docker Compose stack.

If you prefer the raw Compose command:

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d --pull missing
```

Open the app:

- Dashboard: http://localhost:3010
- API health: http://localhost:8000/api/v1/health
- MinIO console: http://localhost:9001

The first signup creates the bootstrap superadmin account. After that, public
signup is disabled unless `ALLOW_PUBLIC_SIGNUP=true` is set.

## Coolify Deployment

For production, the recommended path is Coolify with the dedicated compose file:

```text
docker-compose.coolify.yaml
```

In Coolify:

1. Create a new Docker Compose resource from this repository.
2. Set the Compose file path to `docker-compose.coolify.yaml`.
3. Attach your domain to the `ui` service on port `3010`. In Coolify's domain
   field, use `https://voice.example.com:3010`; in the browser, open
   `https://voice.example.com`.
4. Set these required environment variables:

```env
POSTGRES_PASSWORD=generate-a-long-random-value
REDIS_PASSWORD=generate-a-long-random-value
MINIO_ROOT_PASSWORD=generate-a-long-random-value
OSS_JWT_SECRET=generate-a-long-random-value
```

Coolify handles HTTPS and routing. The API, Postgres, Redis, and MinIO stay
inside the Docker network. The app auto-detects the public URL from the `ui`
service domain; set `APP_URL=https://voice.example.com` only when you want to
override that generated domain.

Detailed guide: `docs/deployment/coolify.mdx`.

## Basic Configuration

Docker Compose reads variables from your shell or from a root `.env` file.
Defaults are for local development only.

Common values:

```env
ALLOW_PUBLIC_SIGNUP=false
SINGLE_ORGANIZATION_MODE=true
MANAGED_ORGANIZATION_PROVIDER_ID=managed_single_org
ENABLE_TELEMETRY=false
DISABLE_HOSTED_CLOUD=true
OSS_JWT_SECRET=change-this-before-production
```

The bundled local services use these default development credentials:

- Postgres: `postgres` / `postgres`
- Redis password: `redissecret`
- MinIO: `minioadmin` / `minioadmin`

Change them before any shared or production deployment.

## LiveKit And Vobiz Setup

The stack starts with `VOICE_RUNTIME=livekit`. Add LiveKit settings through the
UI at `Telephony Configurations`, or provide them as environment variables
before starting the stack.

Minimum LiveKit variables:

```env
VOICE_RUNTIME=livekit
LIVEKIT_URL=wss://your-livekit-host
LIVEKIT_CLIENT_URL=wss://your-livekit-host
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
LIVEKIT_SIP_INBOUND_HOST=your-livekit-sip-host
LIVEKIT_WORKER_MANAGED_BY_API=true
```

For Vobiz, use the `Vobiz + LiveKit setup` flow in the Telephony
Configurations page. It saves the LiveKit runtime settings, stores the Vobiz
account credentials, imports CLIs/phone numbers when available, and provisions
the related SIP assets.

You still need to configure your own LLM, STT, TTS, telephony, and observability
credentials for real calls. Do not put production secrets in committed files.

## Local Source Development

Use this path when you want to run backend and frontend processes directly on
your machine.

Requirements:

- Python 3.13.
- Node.js 24.
- Docker for Postgres, Redis, and MinIO.

Setup:

```bash
git submodule update --init --recursive
cp api/.env.example api/.env
docker compose -f docker-compose-local.yaml up -d
bash scripts/setup_requirements.sh --dev
bash scripts/start_services_dev.sh
```

In a second terminal:

```bash
cd ui
npm install
npm run dev
```

PowerShell equivalents:

```powershell
git submodule update --init --recursive
Copy-Item api/.env.example api/.env
docker compose -f docker-compose-local.yaml up -d
.\scripts\setup_requirements.ps1 -Dev
.\scripts\start_services_dev.ps1
```

```powershell
cd ui
npm install
npm run dev
```

Direct local UI defaults to http://localhost:3000. The Docker quick install UI
uses http://localhost:3010.

## Verification

Backend health:

```bash
curl http://localhost:8000/api/v1/health
```

Targeted checks for the LiveKit/Vobiz release area:

```bash
source venv/bin/activate
set -a && source api/.env.test && set +a
ruff check api/routes/livekit.py api/services/livekit api/tests/test_livekit_vobiz_setup.py api/tests/test_livekit_worker.py api/tests/telephony/vobiz/test_provider.py
python -m pytest api/tests/test_livekit_vobiz_setup.py api/tests/test_livekit_worker.py api/tests/telephony/vobiz/test_provider.py
```

PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
Get-Content api\.env.test | ForEach-Object {
  if ($_ -match '^\s*([^#][^=]+)=(.*)$') { Set-Item -Path "env:$($matches[1].Trim())" -Value $matches[2].Trim('"') }
}
ruff check api/routes/livekit.py api/services/livekit api/tests/test_livekit_vobiz_setup.py api/tests/test_livekit_worker.py api/tests/telephony/vobiz/test_provider.py
python -m pytest api/tests/test_livekit_vobiz_setup.py api/tests/test_livekit_worker.py api/tests/telephony/vobiz/test_provider.py
```

## Release Hygiene

Before publishing this fork or creating an open-source artifact, follow
`docs/developer/upstream-compatibility.md`.

At minimum:

- Keep `ui/openapi-ts-error-1779461066256.log` deleted.
- Do not stage `api/.env`, `ui/.env`, runtime directories, browser caches, or
  any `*.log` files.
- Verify `api/.env.example`, `docker-compose.yaml`, and
  `docker-compose.dev.yaml` contain placeholders only.
- Replace business-specific prefixes or identifiers in LiveKit/Vobiz code.
- Review Vobiz tests for fake credentials, fake phone numbers, and fake account
  data only.
- Decide whether broad UI polish files belong in this release or should be
  split into a separate change.

Final scans before pushing:

```bash
git status --short
rg -n -i "private-client-name|private-project-name|real-customer-phone|real-customer-email" .
```

The private-data scan should return no source-code hits except ignored local
files.

## Useful Commands

Stop containers:

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml down
```

Reset local Docker data. This deletes database, Redis, MinIO, and runtime
volumes:

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml down -v
```

Regenerate the frontend API client after backend API changes:

```bash
cd ui
npm run generate-client
```

## Project Layout

```text
api/       FastAPI backend, workers, integrations, LiveKit runtime
ui/        Next.js 15 frontend
scripts/   Local setup and service scripts
docs/      Documentation source
```

## License

This repository is licensed under the BSD 2-Clause License. See `LICENSE`.
