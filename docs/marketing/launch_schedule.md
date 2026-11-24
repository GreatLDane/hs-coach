# HS-Coach: 2-Week Launch & Feedback Schedule (Nov 24 - Dec 7, 2026)

Every public post needs the owner's OK on the final text first. US Thanksgiving is Thu Nov 26: Reddit is quiet Thu-Sun and posts sink, so the first public post is **Tue Dec 1**. Before each Reddit post, check whether a Hearthstone patch or expansion lands that day. Patch days bury tool posts, and new cards can break parsing.

| Day | Date | Do | Measure / log in progress.md |
|-----|------|----|------------------------------|
| 1 | Tue Nov 24 | Deploy the onboarding fix. Stripe Live cutover and live $9 test. Pick 2-3 reports. **Wave 1:** DM 3-5 Hearthstone friends (template below). | Live checkout passes. Testers invited. |
| 2 | Wed Nov 25 | Watch testers: where did they get stuck (SmartScreen, token, log.config, restart)? Fix the quick ones the same day. | Setup time per tester, every blocker. |
| 3-6 | Thu Nov 26 - Sun Nov 29 | Holiday: play your own games and coach them. Collect real numbers for the r/CompetitiveHS post (habit stats over 20-30 games). Fix wave-1 bugs. | 20+ coached games of your own. Findings drafted. |
| 7 | Mon Nov 30 | Message the r/CompetitiveHS mods with the draft and ask if it's OK. Final read of both Reddit drafts. Screenshots ready. | Mod reply. |
| 8 | Tue Dec 1 | **r/CompetitiveHS post** (morning US Eastern, ~9-11am). Stay on it 3 hours and answer everything. | Upvotes, comments, sign-ups, uploads, first coached games. |
| 9 | Wed Dec 2 | Turn every question from yesterday into a FAQ entry or an issue. Ship small fixes. Reply to late comments. | Bug list, conversion (sign-up to first coached game). |
| 10 | Thu Dec 3 | **r/hearthstone image post.** Same 3-hour reply window. | Same as Day 8. |
| 11 | Fri Dec 4 | **X thread.** Ask 1-2 Discord strategy servers' mods for permission to post. | Mod replies. |
| 12-13 | Sat Dec 5 - Sun Dec 6 | Weekend ladder: watch Render logs, spend and errors. Post the Discord blurb where approved. | Peak-load errors, spend against the cap. |
| 14 | Mon Dec 7 | **Creator DMs** (5-10, personalized). Retro: what worked, what to fix, whether to ship v0.1.1. | Retro notes in progress.md. |

Daily, all 14 days: `/api/health`, Render 5xx count, OpenRouter spend, Stripe webhook deliveries, replies within 24h.

## Feedback loop
- Ask every tester and commenter with a bad review to click **Share** and send the link. That's the cheapest repro you'll get, and it has no personal data in it.
- Track each report as: card/interaction, what the coach said, what was right, and whether it's fixed. Parser bugs on new cards come first.
- When you fix something a user reported, reply to them. That reply is often the most convincing marketing.

## Wave 1 DM template (friends)
> Hey! I built a post-game Hearthstone coach and want to see if it works on someone else's PC before I post it anywhere.
> 1. Register at https://hs-coach.onrender.com and click Copy Upload Token
> 2. Download the uploader from the "Get the Windows Uploader" box (Windows will warn that it's unsigned: More info → Run anyway)
> 3. Paste the token, click Enable Power.log, Save, restart Hearthstone
> 4. Play 1-2 Standard games, then hit Coach on the site
> Tell me anywhere you got stuck, and send a Share link if the review says something dumb. I'll give you Pro for helping.

## FAQ: honest answers to common questions
Keep answers short, factual and calm. Never claim Blizzard approval.

**"Is this against Blizzard's ToS? Will I get banned?"**
> I can't speak for Blizzard, and HS-Coach isn't affiliated with or endorsed by them. What it does: after a game ends, it reads `Power.log`, a text file Hearthstone writes when logging is enabled in its own `log.config` (deck trackers have used that file for years). It doesn't read game memory, doesn't inject anything into the client, and shows nothing while you're playing. Your call whether that's OK for you.

**"What do you upload? Can you see my account?"**
> The uploader parses the log on your PC and strips player names and BattleTags there. Only the anonymized replay ("you" vs "opponent", cards and plays) is sent; the raw Power.log never leaves your machine. Your HS-Coach login is just an email and password, with no Battle.net link. Privacy policy: https://hs-coach.onrender.com/privacy

**"How is this different from HSReplay / Firestone / HDT?"**
> Those are trackers and stat sites: they help during a game or show meta winrates across many players. HS-Coach does neither. It looks at one of your finished games and asks "what decision cost this game?": mulligan grades, the 3 deciding turns with a better line, missed lethals. It's fine to use alongside them.

**"Why would I trust an AI with Hearthstone? They hallucinate."**
> Fair. That's why the exact parts (lethal math with Taunt/Divine Shield/Immune/Secrets, floated mana, unused attacks) are computed by plain code first, and the AI has to explain from those numbers. It can still get strategy calls wrong. If it does, send me the share link and I'll fix it.

**"Why is the .exe unsigned / flagged by SmartScreen?"**
> Code-signing certificates cost money and I'm a solo dev at v0.1. The release page has a sha256 checksum you can verify. The uploader's source is public: https://github.com/GreatLDane/hs-coach If you'd rather not run an unsigned exe, wait until it's signed. That's a reasonable choice.

**"Does it work for Wild / Battlegrounds / Arena / Mac?"**
> Not yet: Standard Constructed on Windows only. Tell me which one you want most.

**"Why pay $9?"**
> You don't have to: 5 coached games a month are free. Pro is for people grinding many games who want 200 reviews a month and stronger models. Cancel any time from Manage Billing.

**"Is it live / does it tell me what to play?"**
> No. It never runs during a game. It only reviews games after they've ended.
