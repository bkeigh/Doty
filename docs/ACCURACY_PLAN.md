# Doty: the accuracy plan

Written 2026-11-10 from the session log of two real Turbo games on Spirit Breaker played that evening
(the session log in `%LOCALAPPDATA%\Doty\logs` and the post-game review beside it). It sits beside
`NEXT_PLAN.md`: that plan says what to build; this one says what a real match showed was wrong, what is now
fixed, and the order that makes the coach measurably more accurate.

## What the two games showed

Both games together: 286 lines in about 52 minutes of game time, 5.5 a minute.

| What went wrong | How often | Cause |
|---|---|---|
| "Buy Phase Boots" to a player wearing Boots of Bearing | 22 lines from 20:13 to the end of game 1 | Boots of Bearing are made from Tranquil Boots, so their parts don't list Boots of Speed, and Doty didn't count them as boots. Guardian Greaves had the same fault |
| Black King Bar brought up after the player went another way | 57 lines over the two games. In game 1 the player built Shadow Blade, Aghanim's Scepter, Boots of Bearing, Silver Edge, Yasha and Kaya and Eul's | Nothing told Doty or the AI that an item had been declined. The AI's opening plan (Phase Boots, Black King Bar) stayed the plan all game |
| "Buy a TP scroll" | 32 lines. Some were right (Doty's own rule saw the slot empty twice), but the player held one at all 8 deaths in game 1, which is when the AI said it most | The note for the lowest ranks told the AI to "prefer advice about ... a TP scroll", and nothing checked the claim against the TP slot |
| "Buy X now" without the gold | "Buy Black King Bar now with your 985 gold", "Buy Ogre Axe now" at 453 | The AI's word against the game data, unchecked |
| A hidden instruction read out loud | "...don't walk the river alone. TRIGGER: hp < 30 and alive => Back under your tower right now" | The AI wrote the tag on the answer's line; Doty only stripped tags on their own line |
| A change of mind spoken as one tip | "Respawn in 3 seconds, buy Tranquil... no: once back, buy a Town Portal Scroll" | Only "Wait, ..." after a full sentence was caught |
| A call about a moment long past | "Grab the water rune at 2:00", said at 2:47 on respawning | A prepared call fired when its condition came true, however late |
| The same call three times in nine seconds | "Back off toward your tower", "Walk back to your tower now", "Walk back to your tower now" | Calls were compared word for word |
| Nothing to buy once the AI's three items were done | The Buy Next panel went empty | The AI's plan replaced the hero's build instead of leading it |

What Doty got right, and should keep: the instant calls landed in 1.4 to 2.7 ms; the AI's live looks answered
in about 1.3 to 1.5 s; the death answers used the game data well ("stunned 3.6 s, 98% HP gone in 4 seconds");
the Turbo clock was right.

## Fixed in this pass

Each of these has a test in `tests/test_own_build.py`, written the way it showed up in the match.

1. **Every pair of boots is boots.** Bearing, Greaves and Boots of Travel 2 included; a pair made from the pair
   you wear is an upgrade, not a second pair.
2. **Doty takes no for an answer.**
   - An item you buy around (twice its price spent elsewhere, no part of it held) leaves this game's build.
     Replaying game 1's purchases: Black King Bar leaves at 17:11 instead of being called until 32:19, and what
     Doty points at next (Eul's, then Yasha and Kaya) is what the player went on to buy.
   - "You can afford X" is said twice. After that the item leaves the build.
   - Say "skip Black King Bar" or "I don't need BKB" on push-to-talk, press Ctrl+Alt+X, or click the ✕ on Buy
     Next. "Bring back Black King Bar" undoes it. No AI call, so it's instant and works with the AI off.
   - The AI is told what you passed on and how many times it has already named each item.
3. **A switch for each subject** (Settings > Alerts > What Doty suggests): items to buy, skill points, farm
   and lane, runes and Roshan, enemy timings, gold and TP scroll, death reviews. Off means the instant calls
   and the AI both stop bringing it up; asking still gets an answer.
4. **The AI's lines are checked against the game data before they're said:** "buy now" against your gold, "buy
   a TP scroll" against your TP slot, an item named more than three times with no part bought, answers that
   stream to the voice sentence by sentence, tags on the answer's line, a change of mind mid-sentence.
5. **Prepared calls** drop when their time has passed or they repeat what was just heard.
6. **The build never runs out,** and an item hardly anyone buys on the hero (Bracer on Spirit Breaker: 2 games
   of 75) isn't in it.

## What is still wrong, in order of how much it costs accuracy

Honest status: Doty is not yet a coach a strong player would trust, and it cannot be called the best until
there is a number that says so. These are the gaps between here and there.

### 1. Nothing measures accuracy (the blocker for everything else)

Today's fixes came from reading one log by hand. That doesn't scale, and there is no way to tell whether a
prompt change makes the coach better or worse.

| # | What | Why | Effort |
|---|---|---|---|
| A1 | **Record every match by default** (the raw game data, compressed, the last ~10 kept; today `gsi.record_dir` is off, so these two games can't be replayed exactly) | Every game you play becomes a test | S |
| A2 | **The replay bench** (`NEXT_PLAN.md` 3.1): feed a recorded match through the rules and the coach offline, at any prompt or model | A change is tried on real moments in minutes | M |
| A3 | **A scorecard per match, computed by Doty itself:** lines that failed a check, lines repeated, lines about an owned or passed-on item, late lines, lines per minute, and calls you followed. Shown in the recap | The number that "more accurate" means. Target for a first bar: under 2% of spoken lines failing a check, under 10% repeats |
| A4 | **"Wrong tip?" in one key press mid-game** (it exists in the app; add a hotkey and a voice phrase: "that's wrong") | Your judgement is the best label there is, and it has to cost nothing to give |

### 2. The item brain is still popularity, not judgement

- **The sample is thin and blind to role.** OpenDota's item popularity for Spirit Breaker was 75 games, all
  roles mixed, normal games only. A roaming support and an offlane core get the same build.
- **Counter items come from a hand-written table** (`knowledge/counters.py`) that names heroes, not what
  their spells do, so neither Doty nor the AI can weigh an item's worth in this game. In game 1 the plan was
  Black King Bar; the player read the lineup as mostly right-click (Juggernaut, Bristleback) and built
  Silver Edge, which is the table's own answer to Bristleback and was never suggested. Whether Black King
  Bar was right there is arguable (Necrophos, Crystal Maiden and Ogre Magi were in the game too); that Doty
  couldn't show its reasons, and couldn't be told no, is not.

| # | What | Effort |
|---|---|---|
| B1 | **Builds by role and bracket.** Per hero, per position, from a larger source (OpenDota's explorer by `lane_role`, or STRATZ, which needs a free key), cached weekly as one data pack | M |
| B2 | **Score counter items from the data, not a table.** For each enemy: damage split (physical, magical, pure), how many of their disables Black King Bar blocks and which go through it, dispellable or not (all in dotaconstants already). "BKB stops 5 of their 9 spells; Reaper's Scythe goes through" is a fact Doty can show and the AI can reason from | M |
| B3 | **The build follows the game:** what killed you (B4), who is fed, whether you're ahead. Today only your own purchases move it | M, after B4 |
| B4 | **Who killed you, with what.** The game data doesn't say. Read the death screen with the screen reader, or after the game from the parsed match | M |
| B5 | **Turbo's own numbers** from your match history (item times, farm), since public statistics are normal games | M |

### 3. The AI still talks too much and holds no plan

5.5 lines a minute is more than anyone can use. `NEXT_PLAN.md` 1.1 (a match plan the coach keeps), 1.5 (one
director for what gets said) and 1.6 (no generic tips) are the fix, and they matter more after this log than
before it: "farm near your tower until Black King Bar" was the AI having nothing new to say. Add to them:

- **A budget:** at most one unprompted coach line every 30 s outside danger, enforced in code.
- **Structured answers** (1.2): fields instead of text, which ends the tag-parsing faults for good.
- **The stronger model writes the plan, the fast one only checks you against it.**

### 4. It can't see the map

Most "back off" calls came after the damage started. Knowing that three enemies are missing before you walk
past the river is what a good coach is for. This needs the minimap reader calibrated on one captured match
(`ROADMAP.md`, Next, item 1). It is the single biggest accuracy gain left that the rules can't give.

### 5. Depth of knowledge

To coach a strong player Doty has to know things the item statistics don't hold:

- **Per-hero playbooks**, built from data and checked each patch: combos, power spikes by level and item,
  what each matchup turns on, how the hero is played at each position.
- **Techniques, called at the moment they apply:** stacking and pulling times, wave states (when to shove,
  freeze or pull), smoke timings, Roshan and Tormentor set-ups, high-ground sieges, buyback discipline,
  save-item reactions (already instant).
- **Replay analysis:** parse the match after the game (OpenDota's parser, or the replay file) for what the
  live data can't show: fights, ward spots, who was where.

### 6. Speed

The instant path is already 1 to 3 ms, and the AI's 1.3 to 1.5 s is near what a network model allows. What's
left: speak from pre-voiced lines for common calls (`ROADMAP.md`, Next, item 6), send no screenshot when the
picture adds nothing (`NEXT_PLAN.md` 1.3), and move anything the AI says often and correctly into a rule
(`NEXT_PLAN.md` 3.4), which makes it free and instant.

## The order

1. **A1 to A4.** Measure first. One evening of play then says what to fix next better than any plan.
2. **B2 and the per-minute budget.** Both are contained, and both hit the two things this log complained
   about most: wrong items and too much talk.
3. **The match plan** (`NEXT_PLAN.md` 1.1, 1.2, 1.5, 1.6).
4. **B1, B4, B5** and the minimap reader, once a captured match exists.
5. **Playbooks, techniques and replay analysis.** This is the long road to a coach a pro would use, and it
   only pays off once 1 to 4 make the basics trustworthy.

## What I need from you

1. **Play one game with capture and recording on** (Settings > Advanced > Capture matches). Still the most
   useful thing you can do: it unlocks the replay bench, the minimap reader and B4.
2. **Press "Wrong tip?" every time a line is wrong.** Each one becomes a test.
3. **Which role you play each game** would settle B1 fastest if Doty asked once at the horn. Yes or no?
