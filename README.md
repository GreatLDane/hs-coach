# HS-Coach

**100% Post-Game AI Coach for Hearthstone (Standard Constructed)**

HS-Coach parses completed Hearthstone `Power.log` matches into anonymized, deterministic **Decision-Snapshot JSON** (`GameReplay`), computes exact Python pre-checks (missed lethals, mana floated, burned/discarded cards, unused attacks/locations), generates grounded post-game AI coaching (`CoachReport`), and renders self-contained HTML reports.

## Guardrails
- **Post-Game Only**: Only completed matches (`PLAYSTATE` in `WON`/`LOST`/`TIED`) are analyzed. No live overlays, no memory reading, no client hooks.
- **Privacy First**: Player names and BattleTags are stripped locally during parsing and never appear in `GameReplay`, `CoachReport`, HTML reports, or LLM prompts.
- **Clean Data Sourcing**: Uses HearthstoneJSON (`cards.json`) via `sync-cards`.

## Milestone 1 Quickstart

```powershell
$env:PYTHONPATH = "src"
$PY = "C:\Users\felza\AppData\Local\Python\pythoncore-3.14-64\python.exe"

# 1. Download or update cards.json (uses ETag caching)
& $PY -m hs_coach sync-cards --out data/cards.json

# 2. Parse completed Standard Constructed games from a Power.log
& $PY -m hs_coach parse "D:\Hearthstone\Logs\Hearthstone_...\Power.log" --cards data/cards.json --out-dir local_output --render-html

# 3. Preview prompt size and estimated cost without making an API call
& $PY -m hs_coach coach local_output/<game_id>.replay.json --dry-run

# 4. Generate AI coaching + full HTML report
& $PY -m hs_coach coach local_output/<game_id>.replay.json --out-dir local_output

# 5. Export JSON Schemas for GameReplay and CoachReport
& $PY -m hs_coach schema --out-dir local_output

# 6. Launch the local drag-and-drop Web App
& $PY -m hs_coach serve --cards data/cards.json --port 8000

# 7. Watch local Power.log files and upload only anonymized completed GameReplay JSON
& $PY -m hs_coach watch --logs-dir "D:\Hearthstone\Logs" --server "http://127.0.0.1:8000" --cards data/cards.json

# 8. Launch the desktop uploader tray/status UI
& $PY -m hs_coach tray --server "http://127.0.0.1:8000" --cards data/cards.json
```

## Packaging & Production Deployment
- **Docker / Compose (Web SaaS Backend)**:
  ```powershell
  docker compose up --build -d
  ```
  Or build/run directly:
  ```powershell
  docker build -t hs-coach .
  docker run -p 8000:8000 -v hs_coach_data:/app/data --env-file .env hs-coach
  ```
- **Cloud PaaS Configs**: Ready-to-deploy blueprints are included in `fly.toml` (Fly.io) and `render.yaml` (Render), both mounting `/app/data` persistently and probing `GET /api/health`.
- **Async Coaching & Security Hardening**:
  - `POST /api/games/{game_id}/coach?async_mode=true` queues coaching in a background worker (`202 Accepted`) with deduplication and status polling via `GET /api/jobs/{job_id}` (`queued` -> `running` -> `ready` | `failed`).
  - `GET /api/health` verifies SQLite WAL connectivity (`db_ok`) and loaded `cards.json` count.
  - Hardening env vars: `HS_COACH_MODE` (`local` vs `hosted`), `HS_COACH_TRUSTED_PROXIES` (comma-separated reverse proxy IPs/CIDRs), `HS_COACH_COOKIE_SECURE` (`1` to force `Secure` cookies + HSTS), `HS_COACH_ALLOW_HOSTED_GUESTS` (`1` for isolated anonymous guest sessions), `HS_COACH_MAX_QUEUED_JOBS` (`32`), and per-minute sliding-window rate limits `HS_COACH_RATE_LIMIT_AUTH` (`20`), `HS_COACH_RATE_LIMIT_UPLOAD` (`30`), and `HS_COACH_RATE_LIMIT_COACH` (`20`).
- **Standalone Windows Desktop Uploader (PyInstaller)**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/build_uploader.ps1
  ```
  Outputs `dist/HS-Coach-Uploader.exe` (double-click to open the status/settings uploader; stores config and card cache under `%LOCALAPPDATA%\HS-Coach`).

## LLM Environment Variables
You can either export environment variables or place them in a local gitignored `.env` file:
- `HS_COACH_PROVIDER`: `openrouter`, `anthropic`, `openai`, `gemini`, or `openai_compatible` (auto-detected if omitted based on available API key env var).
- `HS_COACH_MODEL`: Override default model (defaults: `google/gemini-3.1-pro-preview` on OpenRouter, `claude-sonnet-4-5`, `gpt-4.1`, `gemini-2.5-pro`).
- `HS_COACH_API_KEY`: API key (or set `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`).
- `HS_COACH_BASE_URL`: Optional custom base URL (for gateways or local OpenAI-compatible servers).
- `HS_COACH_UPLOAD_TOKEN`: Optional shared Bearer token protecting `POST /api/replays` for local/guest watcher uploads.
- `HS_COACH_SESSION_SECRET`: Signing secret for `hs_coach_session` HttpOnly cookies in multi-user SaaS deployments.
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRO_PRICE_ID`, `HS_COACH_PUBLIC_URL`: Stripe Pro subscription Checkout, Portal, and webhook verification (`/api/billing/checkout`, `/api/billing/portal`, `/api/billing/webhook`).

## SaaS Accounts & Freemium Quotas
- **Guest Trial (`local`)**: Drag-and-drop `Power.log` trial without registering.
- **Free Plan**: `5` coached games / month using `google/gemini-3.7-flash` plus a personal `hs-coach watch` upload token.
- **Pro Plan**: `200` coached games / month on the fast model (`google/gemini-3.7-flash`), including `30` deep reviews / month with stronger models (`google/gemini-3.1-pro-preview`, `anthropic/claude-sonnet-4.5`). Set by `PlanConfig.monthly_deep_limit` in `auth.py`.
