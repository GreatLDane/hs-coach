# HS-Coach Launch & Marketing Copy

> Replace `[YOUR_APP_URL]`, `[EXAMPLE_REPORT_URL]`, and `[RELEASE_URL]` with your live Render URL, a real `/shared/...` report you generated from one of your own games, and your GitHub Releases link before posting.

---

## 1. Reddit Launch Post (`r/CompetitiveHS` / `r/hearthstone`)

**Suggested Title (`r/CompetitiveHS`):**
> [Tool] I built HS-Coach — a 100% post-game Hearthstone Standard AI coach that checks missed lethals, floated mana, and turning points from Power.log (BattleTags stripped locally)

**Body:**
Hey everyone,

I got tired of scrolling through raw replays wondering which turn I actually threw a Standard game, so I built **HS-Coach** — a post-game match reviewer specifically for Standard Constructed.

### What makes it different (and Blizzard-ToS clean)
- **100% Post-Game Only:** It never shows anything mid-match — no live overlays, no memory reading, no client hooks. It only processes completed games (`PLAYSTATE` ended in `WON`, `LOST`, or `TIED`).
- **Privacy-First Local Parsing:** The Windows desktop uploader (and CLI) parses your `Power.log` **on your machine**, strips BattleTags/account names locally, and only sends anonymized `"you"` vs `"opponent"` match snapshots.
- **Deterministic Math Before AI:** Before an LLM touches the game, Python pre-checks calculate exact turn-by-turn board/hand lethal math (accounting for Taunts, Divine Shield, Immune, Secrets), mana floated, burned/discarded cards, and missed attacks/locations — so the AI doesn't hallucinate non-existent lethals.
- **Per-Game Actionable Review:**
  - Mulligan keep/toss verdicts with explanations
  - Top 3 Turning Points (Missed Lethal / Blunder / Inaccuracy) with concrete better lines
  - Reconstructed observed decklists (excluding Coin & generated cards)
  - Recurring mistake tags & per-deck stats across your games

Here is an example coached report from one of my own matches (public unlisted share link):
[EXAMPLE_REPORT_URL]

**Try it free:** [YOUR_APP_URL] (includes 5 free coached games/month + optional auto-uploader: [RELEASE_URL]).

Would love honest feedback from Legend/Diamond grinders — especially any card interactions or edge cases where the turning point analysis could be sharper!

---

## 2. X / Twitter Launch Thread

**Post 1/4:**
I built HS-Coach — a 100% post-game AI coach for Standard Hearthstone.

Drop in your replay after a match and get:
- Mulligan verdicts
- Top 3 turning points (Missed Lethal / Blunder / Inaccuracy)
- Exact Python damage math so the AI can't hallucinate lethal

Try it free: [YOUR_APP_URL]

**Post 2/4:**
Privacy & fair-play first:
- Strictly post-game only (zero live overlays or mid-match hints)
- Windows auto-uploader strips BattleTags on your PC before upload
- Reconstructs both observed decklists automatically

Example public report: [EXAMPLE_REPORT_URL]

**Post 3/4:**
Over time, HS-Coach tracks your recurring habit tags (e.g. `mana-efficiency`, `ordering`, `missed-lethal`) and winrate/mana-float breakdown per archetype — so you see *which habit* is costing you stars cleanly.

**Post 4/4:**
Free tier gives you 5 coached games/month + the Windows uploader (with one-click `log.config` setup).

Feedback from Standard ladder players is super welcome — drop your weirdest Hearthstone card interaction and I'll test the parser on it!

---

## 3. Discord Community Blurb (Hearthstone Strategy / Coaching Channels)

> Always check server rules on self-promotion first (`#self-promo` or ask mods).

Hey folks — sharing a post-game coaching tool I built for Standard: **HS-Coach** ([YOUR_APP_URL]).

- **100% post-game only** (only reads completed `Power.log` games, zero live overlays/hints)
- **Windows uploader** strips BattleTags locally on your PC + enables `log.config` in one click
- Runs deterministic lethal/mana checks first, then breaks down Mulligan + top 3 turning points

Sample report from one of my games: [EXAMPLE_REPORT_URL]
Happy to trade Pro test credits for feedback on confusing card interactions!

---

## 4. Streamer / YouTuber Outreach DM Template

Hey [Name], love your Standard ladder/VOD reviews!

I built **HS-Coach** ([YOUR_APP_URL]) — a 100% post-game Hearthstone Standard reviewer that catches missed lethals/turning points deterministically from `Power.log` (strips BattleTags locally, zero overlays) and turns each match into a clean shareable report ([EXAMPLE_REPORT_URL]).

Happy to upgrade your account to Pro (200 coached games/mo across Gemini Pro / Claude Sonnet / GPT-4.1) for free if you'd like to try it for post-match reviews or Discord coaching. Zero strings attached — let me know if you want to give it a spin!
