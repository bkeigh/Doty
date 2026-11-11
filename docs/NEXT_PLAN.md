# Doty: the next plan

Written 2026-11-09 after walking every screen of the app (pictures in `docs/plan-shots/`), re-reading the
coach's code and the log of the last demo run with the real AI, and looking at your last 20 games.
Nothing here is built yet.

## The three findings that change the plan

1. **You play Turbo, and Doty doesn't know Turbo exists.** All 20 of your recent games are Turbo
   (OpenDota `game_mode` 23; about 30 minutes each, ~900 gold a minute). Every number Doty coaches with
   is from normal games: when items "usually land", the last-hit targets, the enemy's power-spike times,
   the game-plan curves, "keep buyback from 30:00". In your games those are all wrong by roughly a
   factor of two. The Profile page's score suffers the same way: it calls you a 70 ("50 = average")
   while you win 40%, because it compares Turbo gold to normal-game benchmarks.
2. **The AI coach mostly says nothing useful between deaths.** In the last demo run it looked about twenty
   times and almost every look came back as a quiet variation of "keep farming the bottom lane safely".
   The useful lines were the death answer and the instant calls. The cause is structural: every look
   starts from scratch. There is no plan it is holding you to, no memory of what you did with the last
   tip, and nothing it knows about you as a player.
3. **Nothing measures whether the advice works.** No tip is ever checked against what happened next, so
   neither Doty nor you can tell which advice helps. That is also why the coach can't be "trained" yet:
   there is nothing to train against.

## What the OpenAI bot teaches us (and what it doesn't)

The 2017 bot that beat Dendi played one hero in one lane (1v1 Shadow Fiend mid). It read the exact game
state through Valve's bot interface rather than the screen, and learned by playing itself at enormous
scale (its 5v5 successor played about 180 years of games a day). What it was superhuman at was mechanics:
last hits and denies, creep blocking, spacing, exact damage arithmetic, instant reactions. It had no
understanding to teach, and players beat it within days using tricks it had never seen.

Three things follow for Doty:

- **We can't build that, and it wouldn't help.** In a real match Doty gets only your own hero's data and
  your own screen. Anything more is what gets accounts banned. There is no simulator and no compute to
  train by self-play, and a trained policy can't explain itself.
- **A human can't reach its mechanics, but can copy its method.** The bot got good through a tight loop:
  one narrow skill, a number that says how well it went, thousands of repetitions. Doty can be that
  number for you. It already sees your last hits, your gold, your item cooldowns and your deaths ten
  times a second.
- **"Training the coach" means evidence, not model training.** Recorded matches, a fixed set of test
  moments, a score for every answer, and a rule for anything the AI gets right often enough to do
  instantly.

## Honest limits

| Limit | What it means |
|---|---|
| Only your own hero's data | Enemy items, levels, cooldowns and positions are known only when they're on your screen |
| Screen reading | The top bar reads reliably; the minimap reader is still uncalibrated; the AI reading a screenshot takes 1-2 s and is vague about positions |
| AI speed | About 0.7-1.0 s to its first word, then speech. Nothing the AI says can be about this second of a fight; only rules and prepared calls can |
| AI knowledge | Its Dota memory predates 7.41 and it invents things ("the 0:32 bounty rune"). Every fact it needs must be handed to it |
| Your attention | One instruction every 20-30 seconds is about what a player can use mid-game. More is noise |
| Real-match testing | Two real matches so far. Most of the last three rounds has never run in one |
| Turbo data | Public statistics are mostly from normal games. Turbo numbers will come partly from your own history |

## The plan

Effort: S = under a day, M = a few days, L = a week or more.

### Phase 0: ground truth first

**Status (2026-11-09):** 0.2 to 0.5 are built and tested (see "Built in the tenth pass" in `ROADMAP.md`).
0.1 is still open: it needs one real Turbo game with capture on. Two things changed while building 0.2:
last-hit targets in lane don't move in Turbo (the creeps are the same), so the 5:00 lane check stays and
the 10:00/20:00 farm checks are off in Turbo until there's a Turbo benchmark; and the Tormentor isn't
announced in Turbo, because its time there isn't in a source Doty can cite.

| # | What | Why | Effort |
|---|---|---|---|
| 0.1 | **One recorded Turbo match.** You play one game with capture and recording on | Unlocks almost everything else: which game-data fields really arrive (net worth, towers, wards), the minimap calibration, the strip against the real HUD, and the first test moments for the coach. Pending since day one | You: one game |
| 0.2 | **Turbo mode.** Detect it (your match history, a setting, and the gold rate in the first minutes), then use Turbo numbers for item timings, last-hit targets, enemy spikes, the game plan and buyback | Finding 1. The Turbo rule differences need checking against Valve's own notes before any number is written | M |
| 0.3 | **Demo games stop counting as yours.** The Learn tab's "From your last game" is currently the scripted demo | It pollutes everything that learns about you | S |
| 0.4 | **One brain for "what to buy".** Today the app's Buy Next panel, the rules, the overlay strip, the start card, the Heroes page and the coach each decide separately, and disagree (the panel showed three items as "Buy now" with gold for one, and a Drum of Endurance on Juggernaut) | Conflicting advice is worse than none | M |
| 0.5 | Small faults found on the walk: the top bar says LIVE for ~35 s after a match or demo stops; feed tags (PROGRESSION) differ from overlay tags (LEVEL) | | S |

### Phase 1: the coach holds a plan instead of dropping tips

| # | What | Why | Effort |
|---|---|---|---|
| 1.1 | **A match plan the coach writes and keeps:** how this game is won, the goal for this phase, the next objective and when, the next item, the two biggest threats and the answer to each, your job in your role. The stronger model writes it at the horn, every few minutes and after big moments. The fast looks only check you against it and prepare instant calls | Finding 2. Gives every look a memory, and gives you one thing to hold on to | L |
| 1.2 | **Structured answers.** The AI fills in named fields instead of Doty parsing `SAY:` / `TRIGGER:` text | Removes a class of parsing faults (the AI once wrote the placeholder `item_name` literally) | M |
| 1.3 | **Looks without a screenshot when the picture adds nothing,** with the picture kept for the moments it matters | Smaller requests answer sooner and cost less. To be measured, not assumed | M |
| 1.4 | **More facts the coach already has access to:** the kill score (it's in the game data and unused), the game mode, the plan, and after 0.1 whatever else proves to arrive | The coach can't currently tell whether your team is winning | S |
| 1.5 | **One director for what gets said.** Rules and the AI share one attention budget; each line is scored by urgency, novelty and whether it's about your focus for this game; one instruction at a time | Your attention is the scarcest thing in the loop | M |
| 1.6 | **No more empty tips.** Every tip names an action, a place and an "until". A tip that could apply to any minute of any game goes to the feed, not your ears | Finding 2 | S |

### Phase 2: Doty learns you

| # | What | Why | Effort |
|---|---|---|---|
| 2.1 | **A player record across your last 20 games,** per hero and per mode: deaths by phase, deaths without a TP scroll, deaths alone past the river, deaths within seconds of a warning, gold in your pocket when you died, save items ready and never pressed, item timings, last hits at 5 and 10 minutes | Most of this is already computed per game and then forgotten | M |
| 2.2 | **One focus per game.** Doty picks your biggest leak, gives it a number ("never past the river without a TP: last game 4 times"), says it before the game, favours it during, and grades it after | The same method the bot used: one skill, one number | M |
| 2.3 | **Follow-through.** For each call, did you do it, and what happened next? Shown in the recap ("you followed 5 of 8 calls; the 3 you didn't cost 2 deaths") | Finding 3. Also the raw material for Phase 3 | M |
| 2.4 | **A richer review from the parsed match** (who killed you and with what, purchase times, fights) | "You died three times to Lion's Blink-Hex after he finished Blink at 9:40" beats "you died three times". Whether Turbo games can be parsed on request needs checking | M |
| 2.5 | **Draft suggestions from your own heroes first.** Today the top suggestion for a Spirit Breaker player is Shadow Fiend | A counter you can't play isn't a counter | S |

### Phase 3: train the coach with evidence

| # | What | Why | Effort |
|---|---|---|---|
| 3.1 | **A replay bench:** recorded matches fed back through the coach offline, at any prompt or model | Lets a change be tried on real moments in minutes instead of in your next game | M |
| 3.2 | **A score for every answer:** the checks Doty already runs (names a hero not in the game, sells you what you own, arrives late, repeats itself, is generic), plus a graded set of about 50 real moments. Every "Wrong tip?" report joins the set | Finding 3 | M |
| 3.3 | **Side-by-side trials** of prompts and models with quality, speed and cost in one table, and no prompt change shipped that scores worse | | S after 3.1-3.2 |
| 3.4 | **Promote what works into rules.** Anything the AI says often and correctly becomes an instant, free rule | That's how the save-item calls came about | ongoing |

### Phase 4: practice mode

| # | What | Why | Effort |
|---|---|---|---|
| 4.1 | **Last-hit trainer** in Demo Hero or a bot lane: live count and pace on the overlay, 5- and 10-minute runs, personal bests per hero | The bot's method applied to the skill that matters most at your rank | M |
| 4.2 | **Reaction numbers from real games:** how long after a burst you pressed the save item, how long gold sat unspent, how long a skill point waited | Doty can already see the moment the item goes on cooldown | M |
| 4.3 | **A weekly report:** the leaks, the drills, your rank over time | Progress you can see | S after 2.1 |

### Phase 5: show it better

What the walk found, screen by screen.

- **Coach (in a match).** The biggest thing on the page is "What Doty sees": a small, slow copy of the game
  you're already looking at (and a black hole in the demo). The plan, the enemy and the advice are squeezed
  around it. Replace it with a coach's board: the plan (now, next, why), your focus for the game, the
  enemy five with their threats and spike times, the next item's progress, and the last call with whether
  you followed it. Keep the screen view as a thumbnail. `plan-shots/coach-live.png`, `coach-portrait.png`
- **Coach (feed).** A plain log where the coach's one important line scrolls away under "Lotus pools in
  15s". Pin the current advice; fold the timers.
- **Draft.** The answer (suggested picks) is below the fold, under a wall of 127 tiny hero tiles, during a
  pick phase that lasts seconds. Put the suggestions first and the pool second. `plan-shots/draft.png`
- **Hero page.** Unlabelled numbers under the win rate; the "build" still lists parts (Boots of Speed,
  Ring of Health, Void Stone); matchups from 41 games shown as confidently as ones from 4,000; no
  three-line "how to play this hero". `plan-shots/hero-detail.png`
- **Learn.** The best-structured page, but its content is fixed text. Tie it to your measured leaks and
  to the drills in Phase 4. `plan-shots/learn.png`
- **Profile.** Flattering numbers that don't explain a 40% win rate. Show the leaks and goals from
  Phase 2, per mode, instead.
- **Settings.** Seven sections and a lot of AI plumbing (provider, model, look model, rhythm) for a player
  who wants to press Play. Presets first; the plumbing under Advanced.
- **Overlay.** Cards are sentences. Use item and hero pictures on them ("Buy" + the Ogre Axe icon), keep
  one current instruction large, and let the strip carry the standing information.
  `plan-shots/overlay.png`

## Order I'd build it in

1. Phase 0 (it's the foundation, and 0.1 costs you one game).
2. 1.1, 1.4, 1.6 and the coach board from Phase 5 together: the plan needs somewhere to live.
3. 2.1-2.3: the player record, the focus, follow-through.
4. 3.1-3.3 as soon as there are two or three recorded matches.
5. 1.2, 1.3, 1.5, then Phase 4, then the rest of Phase 5.

## Decisions for you

1. **Play one Turbo game with capture on** (Settings > Advanced > Capture matches) before anything else is built?
2. **Tune for Turbo first,** with normal games second? All 20 of your recent games say yes.
3. **Cost.** The plan in 1.1 uses the stronger model a handful of times a game. Rough guess: a few cents
   more per game than today's ~7-8 cents. Fine, or keep everything on the fast model?
4. **Which matters more to you next:** a smarter coach in real games (Phases 1-2), or practice mode (Phase 4)?
