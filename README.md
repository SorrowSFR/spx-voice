# SPX Voice

SPX Voice is an open-source voice AI platform for building and deploying
conversational voice agents with a visual workflow builder, telephony, and
WebRTC — self-hosted, with your own API keys.

It ships a FastAPI backend, a Next.js dashboard with a node-based workflow
editor, an ARQ worker, and a Postgres / Redis / MinIO storage stack. The default
voice runtime is **LiveKit with Google Gemini realtime (speech-to-speech)**;
traditional STT → LLM → TTS pipelines and Vobiz SIP telephony are also supported.

## What Is Included

- Next.js dashboard with a drag-and-drop workflow editor for voice agents.
- Pre-built starter templates so you can create a working agent in one click.
- FastAPI backend with async SQLAlchemy, an ARQ worker, Redis, Postgres, and MinIO.
- Local email/password auth — the first signup becomes the admin.
- Single-organization mode by default for one-business deployments.
- **LiveKit + Gemini realtime** as the default voice runtime; OpenAI realtime and
  an OpenAI STT/LLM/TTS pipeline are also supported.
- Vobiz telephony + LiveKit SIP provisioning with a guided setup wizard.
- Hosted cloud services and telemetry disabled by default.

---

## 1. Prerequisites

You need **Docker** (with Compose v2) and **Git**. Everything else runs in
containers.

Free these local ports before starting: `3010`, `8000`, `5432`, `6379`, `9000`,
`9001`, `2000`.

### macOS

1. Install [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)
   (choose the Apple Silicon or Intel build for your machine), or with Homebrew:
   ```bash
   brew install --cask docker
   ```
2. Launch Docker Desktop and wait until it reports **Running**.
3. Verify in Terminal:
   ```bash
   docker compose version
   ```

### Windows

1. Install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
   and enable the **WSL 2** backend when prompted (recommended).
2. Launch Docker Desktop and wait until it reports **Running**.
3. Use **PowerShell** (not Command Prompt). Verify:
   ```powershell
   docker compose version
   ```
4. If running `.\start.ps1` is blocked by the execution policy, allow it for the
   current session only:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```

### Linux

1. Install Docker Engine and the Compose plugin from
   [docs.docker.com](https://docs.docker.com/engine/install/).
2. Verify:
   ```bash
   docker compose version
   ```

---

## 2. Quick Start

This runs the exact checkout with Docker. It bind-mounts the local `api/` and
`ui/` source into containers, so local edits are the code that runs.

**macOS / Linux:**

```bash
git clone <this-repository-url>
cd spx-voice
bash start.sh
```

**Windows (PowerShell):**

```powershell
git clone <this-repository-url>
cd spx-voice
.\start.ps1
```

The launcher checks Docker, initializes submodules, creates `.env`, `api/.env`,
and `ui/.env` from their examples when missing, then starts the stack.

> **First run builds the API image locally** from `api/Dockerfile` (no published
> image is required). This can take a few minutes and downloads dependencies;
> later starts reuse the built image and come up in seconds.

If you prefer the raw Compose command:

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d
```

When it's up, open:

- **Dashboard:** http://localhost:3010
- **API health:** http://localhost:8000/api/v1/health
- **MinIO console:** http://localhost:9001 (`minioadmin` / `minioadmin`)

---

## 3. First Run — Create Your First Agent

1. **Create the admin account.** Open http://localhost:3010 and sign up. The
   first signup becomes the admin; public signup is then disabled unless you set
   `ALLOW_PUBLIC_SIGNUP=true`.

2. **Add a model API key.** SPX Voice is Gemini-realtime first. Get a free key
   from [Google AI Studio](https://aistudio.google.com/app/apikey), then either:
   - **Easiest:** add it to `.env` *before* the first start so the agent is
     auto-configured:
     ```env
     GEMINI_API_KEY=your-google-ai-studio-key
     ```
     (then run `start.sh` / `start.ps1`), **or**
   - add it in the dashboard under **Model Configurations** → keep *Realtime
     Mode* on → choose the Google Gemini realtime provider → paste the key →
     **Save**.

   > Under the LiveKit runtime the provider list is gated to what the worker can
   > actually run: **Gemini / OpenAI realtime**, or an **OpenAI** STT/LLM/TTS
   > pipeline. Picking an unsupported provider is rejected at save time.

3. **Create an agent from a template.** Go to **Agents** → **Create Agent** →
   **Start from a Template**, and pick one (Customer Support, Appointment
   Scheduling, Lead Qualification, or Virtual Receptionist). It opens in the
   editor as a ready-to-run `Start → Agent → End` flow you can customize.

4. **Test it in the browser.** Open the agent and use the web call tester to talk
   to it. Edit node prompts, connect nodes by dragging between the handles, then
   **Save** and **Publish**.

5. **(Optional) Connect a phone number.** See *Telephony* below.

---

## 4. Configuration Basics

Docker Compose reads variables from your shell or from a root `.env` file.
Defaults are for **local development only** — change them before any shared or
production deployment.

Common values (`.env`):

```env
ALLOW_PUBLIC_SIGNUP=false
SINGLE_ORGANIZATION_MODE=true
ENABLE_TELEMETRY=false
OSS_JWT_SECRET=change-this-before-production
GEMINI_API_KEY=
```

Bundled local service credentials (development defaults):

- Postgres: `postgres` / `postgres`
- Redis password: `redissecret`
- MinIO: `minioadmin` / `minioadmin`

Put real provider keys in `.env` (for container env) or in the dashboard. Do not
commit secrets.

---

## 5. Telephony (LiveKit + Vobiz)

The stack starts with `VOICE_RUNTIME=livekit`. Add LiveKit settings in the
dashboard under **Telephony Configurations**, or as environment variables before
starting:

```env
VOICE_RUNTIME=livekit
LIVEKIT_URL=wss://your-livekit-host
LIVEKIT_CLIENT_URL=wss://your-livekit-host
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
LIVEKIT_SIP_INBOUND_HOST=your-livekit-sip-host
LIVEKIT_WORKER_MANAGED_BY_API=true
```

For Vobiz, use the **Vobiz + LiveKit setup** wizard on the Telephony
Configurations page. It saves the LiveKit runtime settings, stores the Vobiz
account credentials, imports phone numbers, and provisions the SIP assets. Use
the **Test Vobiz connection** button to verify credentials before the full run.

You still need your own LLM, STT/TTS, telephony, and observability credentials
for real calls.

---

## 6. Local Source Development

Run the backend and frontend directly on your machine (Postgres/Redis/MinIO
still run in Docker).

Requirements: **Python 3.13**, **Node.js 24**, Docker.

**macOS / Linux:**

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

**Windows (PowerShell):**

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

The direct-local UI runs on http://localhost:3000 (the Docker quick start uses
http://localhost:3010).

---

## 7. Coolify Deployment (Production)

For production, use Coolify with the dedicated compose file
`docker-compose.coolify.yaml`:

1. Create a new Docker Compose resource from this repository.
2. Set the Compose file path to `docker-compose.coolify.yaml`.
3. Attach your domain to the `ui` service on port `3010`. In Coolify's domain
   field use `https://voice.example.com:3010`; open `https://voice.example.com`
   in the browser.
4. Set these required environment variables:

```env
POSTGRES_PASSWORD=generate-a-long-random-value
REDIS_PASSWORD=generate-a-long-random-value
MINIO_ROOT_PASSWORD=generate-a-long-random-value
OSS_JWT_SECRET=generate-a-long-random-value
```

Coolify handles HTTPS and routing; Postgres, Redis, and MinIO stay inside the
Docker network. The app auto-detects the public URL from the `ui` domain — set
`APP_URL=https://voice.example.com` only to override it.

Detailed guide: `docs/deployment/coolify.mdx`.

---

## 8. Useful Commands

Stop the stack (same command on macOS / Linux / Windows):

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml down
```

Reset all local data (deletes the database, Redis, MinIO, and runtime volumes):

```bash
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml down -v
```

Rebuild the API image after backend dependency changes:

```bash
bash scripts/docker_dev.sh rebuild      # macOS / Linux
.\scripts\docker_dev.ps1 rebuild        # Windows
```

Tail logs:

```bash
bash scripts/docker_dev.sh logs         # macOS / Linux
.\scripts\docker_dev.ps1 logs           # Windows
```

Regenerate the frontend API client after backend API changes:

```bash
cd ui
npm run generate-client
```

---

## 9. Troubleshooting

- **`docker compose version` fails** — Docker Desktop isn't running, or Compose
  v2 isn't installed. Start Docker Desktop and re-check.
- **First start is slow** — the API image is building locally; this is expected
  on the first run only. Watch progress with `docker_dev.sh logs`.
- **Port already in use** — free the ports listed in *Prerequisites*, or stop the
  process using them.
- **`.\start.ps1` is blocked (Windows)** — run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first.
- **`cloudflared` shows tunnel warnings** — the local stack runs a free Cloudflare
  quick-tunnel for convenience; warnings are harmless and the app works without
  it. It is not used in the Coolify deployment.
- **Can't sign up after the first user** — that's by design. Set
  `ALLOW_PUBLIC_SIGNUP=true` to allow more signups.
- **A model provider won't save** — under the LiveKit runtime only the
  worker-supported providers (Gemini/OpenAI realtime, OpenAI pipeline) are
  allowed; pick one of those.

---

## 10. Project Layout

```text
api/       FastAPI backend, ARQ worker, integrations, LiveKit runtime
ui/        Next.js 15 frontend and workflow editor
scripts/   Setup and service scripts (bash + PowerShell)
docs/      Documentation source (Mintlify)
```

Contributor and upstream-sync notes live in `docs/developer/`.

## License

BSD 2-Clause. See `LICENSE`.
