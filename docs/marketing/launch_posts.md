# HS-Coach Launch Copy

Before posting anything:
- Replace `[APP_URL]` (`https://hs-coach.onrender.com`), `[RELEASE_URL]` (`https://github.com/GreatLDane/hs-coach/releases/latest`), and `[REPORT_1]` / `[REPORT_2]` (real `/shared/...` links opened in an incognito window first).
- Read the community's current self-promotion rules. If unsure, message the mods first and quote this draft.
- Nothing here is posted without the owner's final OK.

## Voice rules (apply to every post)
- Say "I made this" and the price in the first few lines. No fake-user tone, no "game changer".
- Always say: **post-game only** (it reads a game only after it has ended) and **names and BattleTags are stripped on your PC; raw `Power.log` is never uploaded**.
- Never call it an overlay, live helper or deck tracker. Never say "ToS approved", "Blizzard-safe" or "zero hallucinations". See the FAQ in `launch_schedule.md` for the honest answer.
- Name the limits: Windows only, Standard Constructed only, unsigned `.exe`, and an AI review that can still be wrong.
- Lead with a real report, not a feature list.
- Answer every comment in the first 3 hours, especially critical ones.

---

## 1. r/CompetitiveHS

This sub is about strategy, so the post should teach something even to readers who never click the link. Frame it as what the reviews showed, with the tool second. Ask the mods first if tool posts need approval.

**Title:**
> I had an AI + exact-math checker review 30 of my ladder games after they ended. My most common mistake wasn't what I expected.

(Use your own real number and finding, e.g. "I floated 2+ mana on 1 turn in 4", or "3 missed lethals, all involving Hero Power".)

**Body:**
I play [deck] at [rank]. Over the last [N] weeks I built a post-game review tool and ran my own games through it. Some things it found:

- **[Finding 1, with numbers]**, e.g. "I floated mana on 23% of turns 4-7, mostly by holding a 2-drop for value."
- **[Finding 2]**, e.g. "2 missed lethals in 30 games. Both came from forgetting Hero Power damage when the board was wide."
- **[Finding 3, a mulligan pattern]**, e.g. "I kept [card] on the play much more often than it deserved."

Here's one full report so you can judge the quality yourself (no account needed): [REPORT_1]

**How it works, briefly:**
- It only looks at games that have **already finished**. Nothing runs or shows up while you play.
- A small Windows uploader reads your local `Power.log` after each game, **removes player names and BattleTags on your PC**, and sends only the anonymized replay. The raw log never leaves your machine.
- Before any AI sees the game, plain code works out the exact numbers: lethal checks (Taunt, Divine Shield, Immune, Secrets), floated mana and unused attacks. The AI then explains turning points and mulligans from those numbers. It can still be wrong, and I want to know when it is.

Disclosure: I made it. Free tier is 5 coached games a month. Pro is $9/month for 200, including 30 deep reviews with stronger models. [APP_URL]

Limits: Windows only, Standard only, and the uploader isn't code-signed yet (SmartScreen will warn you; there's a sha256 on the release page).

I'm most interested in games where the review got it **wrong**. Weird card interactions are exactly what I need to test.

---

## 2. r/hearthstone

This sub is bigger and more casual, and visuals carry the post. Post as an image (screenshot of a Turning Points section) with the text in a comment, or as a text post with the image linked. Use whatever flair the sub requires for tools or fan-made content.

**Title:**
> I made a post-game coach that tells you which turn you actually threw the game (free, screenshot inside)

**Body / first comment:**
After a game ends, it shows you:
- Your mulligan, card by card: keep or toss, and why
- The 3 turns that decided the game, with a better line for each
- Any lethal you missed. The damage is computed exactly first, so it doesn't invent lethals.

Example from one of my games: [REPORT_1]

It **only** looks at finished games, so there's no overlay and nothing happens mid-match. The Windows uploader strips your name and BattleTag on your PC before anything is sent.

I made it. It's free for 5 games/month, $9 for more. [APP_URL]
Happy to answer anything, including "why should I trust this".

---

## 3. Discord (strategy or coaching servers, with mod permission)

Keep it short. Post only in `#self-promo` / `#tools`, or wherever mods say.

> Hey all, I built a post-game review tool for Standard and would love blunt feedback from people who actually review their games.
> After a match **ends**, it grades your mulligan, picks the 3 turns that mattered, and checks for missed lethals with exact math. Nothing runs during the game, and your name/BattleTag are stripped on your PC before upload.
> Example report: [REPORT_1]
> Free (5 games/mo): [APP_URL]
> If it gets one of your games wrong, send me the share link. That's the most useful thing you can give me. I'll give Pro to anyone who sends useful bug reports.

---

## 4. X / Twitter (3 posts, each with an image)

**1/3** (image: Turning Points section)
> Built a post-game coach for Standard Hearthstone.
> After a game ends it shows the 3 turns that decided it, grades your mulligan, and checks for missed lethals with exact math before the AI says anything.
> Example: [REPORT_1]

**2/3** (image: mulligan section)
> Privacy:
> - Only finished games, nothing during play
> - Names + BattleTags stripped on your PC
> - Raw Power.log never uploaded
> Windows uploader: [RELEASE_URL]

**3/3**
> Free: 5 coached games/month. Pro: $9 for 200 (30 of them deep reviews).
> [APP_URL]
> Send me a game it got wrong and I'll dig into it.

---

## 5. Streamer / YouTuber DM (educational Standard creators, 5-10 people)

Personalize the first line with something specific from a recent video. If you can't, don't send it.

> Hi [Name], your [specific video/segment] on [topic] was great. The bit where you [specific detail] is exactly the kind of review I've been trying to automate.
>
> I built HS-Coach, a **post-game** reviewer for Standard. After a match ends it grades the mulligan, finds the 3 deciding turns, and checks for missed lethals with exact math. It never runs during a game, and names/BattleTags are stripped on the player's PC. Example: [REPORT_1]
>
> If it'd be useful for viewer-game reviews, I'll set you up with Pro free. No obligation to mention it. If it gets something wrong I'd genuinely like to hear it.
>
> [Your name]
