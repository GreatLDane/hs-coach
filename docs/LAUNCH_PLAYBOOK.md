# HS-Coach Launch, Distribution & Marketing Playbook

A novice-friendly, step-by-step guide taking HS-Coach from your GitHub repo (`https://github.com/GreatLDane/hs-coach`) to a live public SaaS launch and Windows desktop release.

---

## Part 1 — Technical Setup & Cloud Deployment

Complete Steps 1–6 in order before inviting external users.

### Step 1: Push the Latest Fix & Confirm Green CI on GitHub
1. Open Windows PowerShell in `C:\claude projects\HS-Coach` and run:
   ```powershell
   git add requirements.lock pyproject.toml docs/LAUNCH_PLAYBOOK.md
   git commit -m "Bump certifi and add launch playbook"
   git push origin main
   ```
2. Open your repository Actions page:
   https://github.com/GreatLDane/hs-coach/actions
3. Wait ~1–2 minutes for the latest **CI** workflow run to finish and turn green:
   - **`test-and-audit`**: confirms all 44 unit tests pass, `pip check` passes, and `pip-audit -r requirements.lock` reports zero known vulnerabilities.
   - **`docker-build`**: confirms the production container image builds cleanly.

---

### Step 2: Deploy the Backend on Render (Single-Click Blueprint)
1. Go to https://render.com, sign up (or log in), and connect your **GitHub** account.
2. In the Render dashboard, click **New +** -> **Blueprint**.
3. Pick your repository (`GreatLDane/hs-coach`). Render will automatically read `render.yaml`, which sets:
   - `HS_COACH_MODE=hosted`
   - Persistent disk mounted at `/app/data` (for SQLite `hs_coach.db` and `cards.json`)
   - Auto-generated `HS_COACH_SESSION_SECRET`
   - Health check path `/api/health`
4. Wait for the service to be created. Keep it at **1 instance** (do not scale horizontally across multiple nodes while using single-file SQLite + in-memory rate limits/job queue).
5. Copy your new live HTTPS URL at the top of the Render service page (it looks like `https://hs-coach-xxxx.onrender.com`). **No trailing slash.**

---

### Step 3: Set Up Stripe Subscriptions (Start in Test Mode)
1. Sign up / log in at https://dashboard.stripe.com and make sure the **Test mode** toggle (top right) is turned **ON**.
2. Create your Pro subscription product:
   - Go to **Product catalog** -> **+ Add product**.
   - **Name**: `HS-Coach Pro`
   - **Pricing**: Recurring, Monthly (e.g., `$9.00` USD).
   - Click **Save product**, open the product, and copy the **Price ID** (looks like `price_...`).
3. Copy your Stripe Secret Key:
   - Go to **Developers** -> **API keys** -> copy the **Secret key** (`sk_test_...`).
4. Create the Stripe Webhook pointing to your Render URL:
   - Go to **Developers** -> **Webhooks** -> **+ Add endpoint**.
   - **Endpoint URL**: `https://YOUR_RENDER_URL/api/billing/webhook` (replace `YOUR_RENDER_URL` cleanly).
   - Under **Select events**, search and add these exact 3 events:
     1. `checkout.session.completed`
     2. `customer.subscription.updated`
     3. `customer.subscription.deleted`
   - Click **Add endpoint**, click **Reveal** under Signing secret, and copy `whsec_...`.

---

### Step 4: Set Up Password Reset Email (Optional, Recommended)
Users can already register and sign in without an email sender, but password reset links need an email provider:
1. Create a free account at https://resend.com.
2. Verify your sending domain (or use Resend's onboarding domain for initial personal testing) and create an **API Key** (`re_...`).
3. *(Alternatively, any SMTP server works with `HS_COACH_SMTP_HOST`, `HS_COACH_SMTP_PORT`, `HS_COACH_SMTP_USER`, `HS_COACH_SMTP_PASS`.)*

---

### Step 5: Configure Environment Variables in Render
In your Render dashboard -> select `hs-coach` -> **Environment**, add/fill these secret variables:

- `OPENROUTER_API_KEY` = your OpenRouter API key
- `HS_COACH_PUBLIC_URL` = `https://YOUR_RENDER_URL` *(exact HTTPS URL, NO trailing `/`)*
- `STRIPE_SECRET_KEY` = `sk_test_...` *(switch to `sk_live_...` after Step 6 passes!)*
- `STRIPE_WEBHOOK_SECRET` = `whsec_...` *(must match Test or Live webhook respectively)*
- `STRIPE_PRO_PRICE_ID` = `price_...` *(must match Test or Live Price ID respectively)*
- *(Optional for password reset emails)*:
  - `RESEND_API_KEY` = `re_...`
  - `HS_COACH_EMAIL_FROM` = `HS-Coach <noreply@yourdomain.com>`

Click **Save Changes**. Render will redeploy and automatically download `cards.json` on first boot.

---

### Step 6: Run Your Live End-to-End Smoke Test
Test every critical flow yourself before sharing publicly:

1. **Health check:** Visit `https://YOUR_RENDER_URL/api/health` in your browser -> confirm `{"status":"ok","db_ok":true,"cards_loaded":...}`.
2. **Pages & Auth:** Visit `https://YOUR_RENDER_URL`:
   - Click `/privacy` and `/terms` in the footer to verify both pages load.
   - Click **Register (Free)** to create your test account.
   - Copy your **Upload Token** (`hsc_...`) shown under the Account Bar.
   - Try dropping a raw `Power.log` on the hosted page if testing upload limits -> confirm hosted mode rejects raw logs cleanly and only accepts locally anonymized `.replay.json` files or uploads from `HS-Coach-Uploader.exe`.
3. **Desktop Uploader E2E:**
   - Double-click `dist\HS-Coach-Uploader.exe` on Windows.
   - Set **Server URL** = `https://YOUR_RENDER_URL` and **Upload Token** = your `hsc_...` token.
   - Click **Enable Power.log** (restart Hearthstone if it is already open), then click **Save Settings**.
   - Play one Standard Constructed match to completion (`WON`/`LOST`/`TIED`), then watch the uploader automatically detect and upload it.
   - Refresh the dashboard -> confirm the match appears with BattleTags stripped (`you` vs `opponent`), click **Coach**, watch it transition `Queued...` -> `Coaching...`, and open **View Report**.
4. **Stripe Checkout E2E (Test mode):**
   - Click **Upgrade to Pro** -> complete checkout using Stripe's test card (`4242 4242 4242 4242`, any future expiry, any 3-digit CVC).
   - Return to HS-Coach -> verify your account badge now says **PRO · 0/200 coached this month · 0/30 deep** and **Manage Billing** opens Stripe's Customer Portal.
5. **Switch Stripe to Live Mode:**
   - Once Test mode works end-to-end, toggle **Test mode OFF** in Stripe, recreate the `HS-Coach Pro` product + webhook endpoint (`https://YOUR_RENDER_URL/api/billing/webhook`), and update `STRIPE_SECRET_KEY` (`sk_live_...`), `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRO_PRICE_ID` in Render.

---

## Part 2 — Distribution & Marketing Steps

Once your live site passes Step 6, follow Steps 7–10 to package the desktop app and roll out publicly.

### Step 7: Publish the Windows Uploader via GitHub Releases
Your repo includes `.github/workflows/release.yml`, which automatically compiles `HS-Coach-Uploader.exe` cleanly in the cloud and creates a download page whenever you push a `v*` version tag.

1. Make sure all edits are committed and pushed to `main`:
   ```powershell
   git status
   ```
2. Create and push your `v0.1.0` release tag:
   ```powershell
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. Open https://github.com/GreatLDane/hs-coach/actions -> watch **Release Windows Uploader** complete (~3 minutes).
4. Open **Releases** on the right sidebar of your GitHub repository to find `HS-Coach-Uploader.exe` and `HS-Coach-Uploader.exe.sha256`.
   - **Important visibility check:** If `GreatLDane/hs-coach` is **Private**, outside users cannot open your GitHub Releases link! Either make the repo public (if you want open-source distribution), or create a small **Public** releases-only repo (e.g. `GreatLDane/hs-coach-releases`) and upload the `.exe` + `.sha256` there.
   - **Windows SmartScreen note:** Because the `.exe` is not yet code-signed with an Authenticode certificate, let downloaders know on your download instructions: *"Windows may show 'Windows protected your PC' on first launch — click More info -> Run anyway, or run `hs-coach` from source / verify with `.sha256`."*

---

### Step 8: Build Visual Proof Before Posting
Gamers trust playable examples and real match screenshots over marketing text. Create these 2 assets first:

1. **Create 2 real unlisted public reports:**
   - Coach 2 interesting Standard matches (e.g., 1 close win with an interesting lethal or ordering sequence + 1 loss showing real turning points).
   - Click **Share** on each match row in your dashboard.
   - Test both `/shared/...` links in an **Incognito browser window** to confirm they load without login, show zero BattleTags, and display the **Try HS-Coach Free ->** banner at the top.
2. **Verify Discord preview + take screenshots:**
   - Paste one `/shared/...` link into a private Discord server or DM to verify the OpenGraph embed card renders cleanly (`CLASS vs CLASS (WIN · Grade ...) — HS-Coach Report`).
   - Capture 2 crisp screenshots:
     1. A **Turning Points + Mulligan Review** section from a coached report.
     2. The **Observed Decklists + Habit Tags** dashboard view.

---

### Step 9: Launch in Staged Community Waves
Don't post everywhere at once — roll out in 3 small waves so you catch early questions or Hearthstone card-parsing edge cases before large traffic arrives. Copy-paste templates are ready in [`docs/marketing/launch_posts.md`](marketing/launch_posts.md).

1. **Wave 1 — Private Beta / 3–5 Discord Friends (Day 1):**
   - Share your Render URL + Windows uploader link + setup note (`Enable Power.log`, restart Hearthstone).
   - Fix any UX confusion or log-parsing edge cases they hit.
2. **Wave 2 — Reddit (`r/CompetitiveHS` & `r/hearthstone`, Days 3–4):**
   - Double-check each subreddit's self-promotion guidelines first.
   - Post the **Reddit Launch Post** from [`docs/marketing/launch_posts.md`](marketing/launch_posts.md) with your real `/shared/...` example link and screenshots.
   - Lead clearly with: **100% Post-Game Only** (no live overlays or mid-match hints), **local BattleTag stripping**, and **deterministic lethal math**.
3. **Wave 3 — X/Twitter & Streamer / YouTuber Outreach (Week 2):**
   - Post the 4-part X/Twitter thread with your screenshot/GIF and live `/shared/...` matchup breakdown.
   - Send the short DM template in [`docs/marketing/launch_posts.md`](marketing/launch_posts.md) to 5–10 educational Standard Hearthstone streamers/YouTubers offering complimentary **Pro** upgrades for post-game VOD review segments.

---

### Step 10: First-Week Operations & Guardrails
Keep these checks active during launch week:
- **OpenRouter spend cap:** Set a hard monthly dollar limit on your OpenRouter API key dashboard so unexpected traffic spikes cannot drain your balance.
- **SQLite disk snapshots:** In Render -> your persistent disk -> enable scheduled snapshots/backups for `/app/data/hs_coach.db`.
- **Reverse-proxy & health monitoring:** Confirm Render's `/api/health` check stays green and rate limiting / request logs show normal traffic without runaway errors.
- **Keep `.env` out of Git:** Never commit `.env`, `.log` files, or `data/` backups (`git status` should stay clean).
