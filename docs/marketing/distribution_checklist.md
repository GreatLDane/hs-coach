# HS-Coach Distribution Checklist

The day-by-day plan and FAQ answers are in [`launch_schedule.md`](launch_schedule.md). Post copy is in [`launch_posts.md`](launch_posts.md).

## Phase A: Release (done)
- [x] `v0.1.0` published with `HS-Coach-Uploader.exe` and `.sha256` at https://github.com/GreatLDane/hs-coach/releases (public, downloads without login; checked 2026-11-24).
- [x] Dashboard links to `releases/latest`, with the SmartScreen note and checksum mention.
- [ ] For each new uploader version: tag `vX.Y.Z`, wait for **Release Windows Uploader** to go green, then download it once yourself on a clean PC or VM.

## Phase B: Before any public post
- [ ] Stripe is in Live mode and the live $9 checkout, webhook and Portal cancel all passed (see `plan.md` step 4).
- [ ] OpenRouter hard monthly spend cap is set. Pick a number you're fine losing in one bad week.
- [ ] Render disk snapshots are on for `/app/data/hs_coach.db`.
- [ ] 2-3 `/shared/...` reports are chosen. Each one:
  - opens in an incognito window with no login
  - shows no names or BattleTags anywhere (also check the page source)
  - has at least one concrete finding: a turning point with a better line, a missed lethal, or a mulligan grade with a reason
  - has a review you agree with. If the coach is wrong in it, don't feature it.
  - renders a clean preview card when pasted in a Discord DM
- [ ] 2 screenshots: Turning Points section and Mulligan section. Crop out browser bars and your email.
- [ ] A fresh account completed the whole path on a PC that never ran HS-Coach: download, SmartScreen, token paste, Enable Power.log, one finished game, Coach, report.

## Phase C: Waves
Post in small waves, fixing friction between them. Exact days are in `launch_schedule.md`.

| Wave | Where | Goal | Go to next wave when |
|------|-------|------|----------------------|
| 1 | 3-5 friends / one small Discord, by DM | Find setup friction | At least 3 testers got a report without your help, or you fixed what blocked them |
| 2 | r/CompetitiveHS, then r/hearthstone 2+ days later | First real users and honest criticism | No open parser or onboarding bugs from wave 1 |
| 3 | X thread, 5-10 creator DMs, 1-2 Discord servers (with mod OK) | Reach and credibility | Reddit questions are answered and turned into fixes or FAQ entries |

Community rules:
- **r/CompetitiveHS:** the post must teach something on its own. Check the sidebar rules on self-promotion and tools, and message the mods first if unclear. Disclose that you made it.
- **r/hearthstone:** image posts do best. Use the required flair. One post only; don't repost if it flops, wait for a real update (e.g. v0.2).
- **Discord:** only in self-promo or tool channels, or with a mod's OK. Never DM members you don't know.
- **X:** reply to Hearthstone conversations with a relevant report only when it actually helps that thread. Don't paste links into strangers' threads.
- **Creators:** personalize every message. Max 10, no follow-up after one polite nudge.

## Watch during launch week
- Render logs and `/api/health` twice a day. Watch for 5xx spikes and parser errors on new cards.
- OpenRouter spend against the cap.
- Stripe Live webhook deliveries: all 2xx.
- Every bug report gets a reply within 24h, even if only "reproduced, fixing".
