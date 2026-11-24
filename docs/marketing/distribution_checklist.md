# HS-Coach Distribution & Pre-Launch Checklist (Step by Step)

Follow these steps after your Render app is deployed and tested (see main README/guide).

---

## Phase A — Publish the Windows Uploader on GitHub Releases (10 mins)
Your repo already has `.github/workflows/release.yml` configured. Anytime you create a `v*` version tag, GitHub automatically builds `HS-Coach-Uploader.exe` on a clean Windows runner and publishes a download page.

1. Make sure your latest changes are committed and pushed to `main`:
   ```powershell
   git add .
   git commit -m "Add shareable reports and release workflow"
   git push origin main
   ```
2. Create and push your first release tag:
   ```powershell
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. Open your GitHub repository -> click **Actions** -> **Release Windows Uploader** and wait ~3 minutes for it to turn green.
4. Click **Releases** on the right sidebar of your GitHub repo. Copy the download link for `HS-Coach-Uploader.exe` (`https://github.com/YOUR_USERNAME/hs-coach/releases/latest`).
   *(Note: If your code repo is private, create a tiny public "releases-only" repo or attach the `.exe` on your landing site — public users cannot download assets from a private GitHub repo.)*

---

## Phase B — Create Proof Assets Before Posting (15 mins)
Players click real proof faster than marketing claims. Prepare these 2 things first:

1. **Generate 2 real shareable report URLs:**
   - Play 2 interesting Standard matches (ideally 1 close win + 1 close loss with real turning points).
   - Coach both on your live Render dashboard.
   - Click **Share** on each row -> test opening the `/shared/...` links in an Incognito browser window (confirming the **"Try HS-Coach Free ->"** banner and **0 BattleTags** appear cleanly).
2. **Capture a 20-second demo GIF / screenshot:**
   - Paste one `/shared/...` link into Discord to confirm the rich OpenGraph preview card renders cleanly (`Class vs Class · Grade ...`).
   - Take 2 clean screenshots: (a) the Mulligan + Turning Points report, and (b) the Observed Decklists + Habits section.

---

## Phase C — Staged Community Launch (Avoid Launching Everywhere at Once)
Launch in small waves so you catch parser edge cases or UX hiccups with your first 10 users before wider traffic hits:

1. **Wave 1 — 3–5 Hearthstone Discords / Friends (Day 1):**
   - Ask beta testers to run the uploader, click **Enable Power.log**, play 2 games, and report any friction (e.g., Windows SmartScreen warning, token paste confusion, card text edge cases).
2. **Wave 2 — `r/CompetitiveHS` + `r/hearthstone` (Day 3–4):**
   - Post using the Reddit template in `launch_posts.md` with your real `/shared/...` link and screenshots (check subreddit self-promotion rules first).
   - Monitor Render logs + OpenRouter spend + `/api/health` throughout the day.
3. **Wave 3 — X/Twitter & Streamer DMs (Week 2):**
   - Share interesting anonymous `/shared/...` breakdowns of meta matchups, and reach out to 5–10 instructional Hearthstone streamers/coaches offering free Pro accounts.

---

## Watch Items During First Week
- **Windows SmartScreen:** Until you buy an EV/OV Authenticode certificate, tell Windows users in your download note that *"Windows protected your PC -> More info -> Run anyway"* appears because the indie `.exe` is unsigned — also share the `.sha256` checksum from the release!
- **OpenRouter spend cap:** Set a hard monthly spend limit on your OpenRouter API key dashboard so a viral spike cannot unexpectedly drain your balance.
- **SQLite backups:** Enable Render disk snapshots for `/app/data/hs_coach.db`.
