# HS-Coach Project Guardrails & Architecture

## Core Product Rules
1. **100% Post-Game Only**:
   - Strictly analyze **completed** Hearthstone matches (`PLAYSTATE` ended in `WON`, `LOST`, or `TIED`).
   - Never add live mid-match advice, in-game overlays, memory reading, or client hooks.
2. **Privacy & IP Hygiene**:
   - Strip player names and BattleTags (`Name#1234`) locally before anything leaves the machine.
   - Only emit `"you"` and `"opponent"` in `GameReplay`, `CoachReport`, HTML reports, logs, and CLI output.
   - Use official/community HearthstoneJSON (`cards.json`) with the built-in ETag sync helper; never scrape third-party stat CDNs (e.g., HSReplay, Firestone).
   - Never commit `.log` files, API keys, `.env` files, or generated outputs.

## Package Architecture (`src/hs_coach/`)
- `schemas.py`: Pydantic v2 contracts for `GameReplay` and `CoachReport` (plus LLM output schema `CoachReportContent`).
- `cards.py`: `CardDB` loader, text cleaner, Standard secret pool builder, and HearthstoneJSON ETag sync helper.
- `parser.py`: Streaming `Power.log` parser (`iter_game_chunks` -> prescan -> chronological state machine) producing anonymized `GameReplay` objects with deterministic pre-checks.
- `coach.py`: Grounded prompt builder, model-agnostic LLM client (Anthropic, OpenAI, Gemini, OpenAI-compatible), JSON repair retry, strict schema validation, and token/cost tracking.
- `render.py`: Self-contained Jinja2 HTML report renderer (`autoescape=True`).
- `auth.py`: Password hashing (`PBKDF2-HMAC-SHA256`), HMAC-SHA256 session cookies, upload tokens, and Free vs Pro plan quotas (`PLAN_CONFIGS`).
- `billing.py`: Stripe Pro Checkout Sessions, Customer Portal, and HMAC-SHA256 webhook verification (`checkout.session.completed`, `customer.subscription.*`).
- `storage.py`: SQLite `GameStore` for multi-user accounts, anonymized `GameReplay` / `CoachReport` history, and longitudinal habit aggregation.
- `web.py`: FastAPI drag-and-drop web app (`/api/auth/*`, `/api/billing/*`, `POST /api/upload`, `POST /api/replays`, `POST /api/games/{id}/coach`, `GET /api/habits`, `GET /games/{id}/report`).
- `uploader.py`: Thin post-game local `Power.log` watcher (`PowerLogWatcher`) stripping BattleTags locally and posting only anonymized `GameReplay` JSON to `/api/replays`.
- `desktop.py`: Thin post-game Windows tray / `tkinter` uploader wrapper (`BackgroundWatcherService`, `run_desktop_app`).
- `cli.py`: CLI (`sync-cards`, `parse`, `coach`, `render`, `schema`, `serve`, `watch`, `tray`).

## Development Commands
- Python runtime on this machine: `C:\Users\felza\AppData\Local\Python\pythoncore-3.14-64\python.exe`
- Run tests:
  ```powershell
  & 'C:\Users\felza\AppData\Local\Python\pythoncore-3.14-64\python.exe' -m unittest discover -s tests -v
  ```
- Run CLI module without installation by setting `PYTHONPATH=src`:
  ```powershell
  $env:PYTHONPATH = "src"
  & 'C:\Users\felza\AppData\Local\Python\pythoncore-3.14-64\python.exe' -m hs_coach --help
  ```
