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

Where these stand (2026-11-11): **A1 built** (`match_record.py`: on by default, the last 30 matches or 200 MB,
game data scrubbed of identity, every line Doty showed; no screenshots). **A2 built** (`doty/bench`: replays a
record, a capture, a recording or the scripted match through the real rules; the AI coach's part is replayed
with scripted answers). **A3 partly built:** the bench computes the scorecard per match
(`python -m doty.bench --latest`), and `bench.records.last_game_summary` writes it in plain sentences, but the
app doesn't show it in the recap yet; "calls you followed" isn't measured. **A4 not built:** "Wrong tip?" is
still a click in the feed; reports do become bench fixtures (`--from-report`).

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

Where these stand (2026-11-11): **B1 partly built:** hero cards (`knowledge/cards.py`) hold builds by position
from pro matches of this patch, and pace by rank; the build follows the card for your role once Doty knows it.
Builds by rank bracket aren't there (the pro sample is one bracket). **B2 built** (`knowledge/judgement.py`, see
`ARCHITECTURE.md`, "Judgement from the AI, facts from code"). **B3, B4, B5 not built.**

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

## Decided while talking it through (2026-11-10, after the first pass)

These came out of a design conversation with the player. Status as of 2026-11-11 after each one; how they fit
together is in `ARCHITECTURE.md` ("Judgement from the AI, facts from code").

1. **Disagree once, with a reason; and make it a setting.** When Doty disagrees with the player's build
   it says so at most once per item per match, at a buying moment, and only with a reason from this game.
   With no such reason it says nothing. After that it follows the player. This is the default of a
   setting ("When I go my own way": follow me silently / say it once with a reason / keep reminding me),
   and it replaces today's three pushes and two "you can afford" calls.
   **Built** (`rules/own_way.py`, `coach.own_way`, Settings > Alerts > What Doty suggests). The rules and the
   coach share one record of what was argued; the reason is the item judgement of point 3.
2. **Your instructions to the coach.** A free-text box in Settings, like custom instructions in a chat
   app ("I play position 4, I always build Boots of Bearing on Spirit Breaker, no BKB talk unless they
   have three stuns"). Free text only reaches the AI, and the instant rules do most of the talking, so on
   saving, the AI turns the text into settings the code enforces (items never to suggest, a build per
   hero, topics off, how much it talks) and shows the player what it understood. Whatever can't be made
   a setting goes into the prompt as style. "Remember that" by voice adds to the same list.
   **Built** (`coach/instructions.py`, Settings > AI coach > Your instructions), with a reader in code when no AI
   is set up. By voice, only "never suggest X again" is understood; a general "remember that" isn't.
3. **Is this counter item worth it, calculated.** `abilities.json` already marks every spell's damage
   type and whether it goes through BKB (checked: Omnislash and Quill Spray pierce it; Impale and Laguna
   Blade don't). Score BKB by the enemy disables and magic damage it would block, and do the same for
   break, healing reduction, Linken's and the other counters. This supplies the "reason" in point 1.
   **Built** (`knowledge/judgement.py`, `Knowledge.item_case`): twenty-odd counter items, a score and a short
   reason each, no verdict without the enemies' ability data.
4. **Follow what the player is already buying.** Parts held that point at an item make it the target
   even when it isn't in Doty's plan; read the quick-buy panel by matching item icons, the way the top
   bar's portraits are matched.
   **Partly built:** held parts decide the item being built (`Knowledge.held_target`). The quick-buy panel isn't
   read.
5. **The AI as strategist.** It writes a short plan for the match at the start and at each death (how
   you win, your job, what to watch, item conditions); the instant rules talk live, set up by that plan;
   the player can see and veto it.
   **Not built.** The opening look and the plan hotkey still give a plan and a build, but nothing keeps a plan
   for the match or sets the rules up from it.
6. **Drawings: lighter and exact.** Today a mark is a 5 px line inside a dark outline and a glow, about
   14 px across, and the AI picks its position in pixels on a shrunken screenshot that is 2 to 4 seconds
   old.
   - Look: a thin line (about 2 px), a hairline dark edge, no glow, 60 to 70% opaque, hollow rings,
     smaller labels, quicker fade. Offer a thickness and opacity setting.
   - Position: the AI names the thing ("circle Lion", "arrow to my bottom tier-1 tower") and code places
     it: heroes from the minimap reader, the player's hero from the game data's position, towers, runes
     and Roshan from fixed map coordinates. A point the AI does give snaps to the nearest thing Doty can
     see, or isn't drawn.
   - Scope: map things go on the minimap only. On the main view, draw only on fixed parts of the HUD
     (shop, item slots, abilities), since the camera moves under anything else.

   **Partly built.** The look and the two settings are built (Settings > Overlay). Named places are built
   (`knowledge/mapgeo.py`: towers, runes, Roshan's pits and more from the 7.41 map data; `me` follows your hero;
   HUD parts from `hud.anchor`), and unknown names are dropped. Missing: other heroes by name (the minimap
   reader isn't calibrated, so there are no hero markers to name or snap to), and the pixel fallback on the main
   view still exists. The map and HUD positions haven't been checked against a real game frame yet
   (`hud.HUD_CALIBRATED = False`).

## More ideas from the same conversation

In plain words, what would take the coach from "reminders" to "gets a beginner good". Status as of 2026-11-11.

7. **A card per hero.** What this hero wants at each stage of the game, its power spikes, its combos and
   the mistakes people make on it. Built from real data (ability numbers, how high-rank players build it
   in each role, how fast they farm it), written once per patch and checked, so the coach reads from the
   card instead of making it up mid-game.
   **Partly built** (`knowledge/cards.py`, `knowledge/study.py`): builds by position, item timings with win
   rates, pace by rank, power spikes, skill order, matchups and game length, from public statistics, rebuilt
   for each patch and read in every AI look. Missing: combos and common mistakes (not in the data), and no
   person checks a card.
8. **A card per enemy.** At the draft, for each enemy hero: the one or two things that kill you and what
   answers them, worked out from their abilities. This is the "threat list" for the match.
   **Not built.** (The item judgement already works out what each counter item answers; the card itself is
   being worked on.)
9. **Really read the map.** Calibrate the minimap reader on the player's own screen so Doty knows who is
   showing and where. Then the calls that matter get simple and exact: "three missing and you're past the
   river: back", "nobody is top: take the tower".
   **Not built.** Needs a captured match.
10. **Read the scoreboard.** Enemy items and levels by matching icons, the same way portraits are matched.
    That tells the coach who is stronger right now, which is the whole fight-or-don't question.
    **Not built.**
11. **Teach one thing per game.** Pick a single focus from the player's last few games (last hits by ten
    minutes, dying with no TP, buyback), coach mostly that, grade it afterwards and track it across games.
    Harder topics unlock as the easy ones stick. This is the part that turns a beginner into a good player.
    **Not built** (being worked on).
12. **Benchmarks by rank.** "Players one rank above you have this item by 14:00; you got it at 17:00."
    Honest, specific and motivating.
    **Partly built:** at 10:00 and 20:00 the farm check compares your last hits with players of your hero,
    role and rank and the rank above (from the hero card; normal games only, quiet in Turbo). Item timings by
    rank aren't said.
13. **Only the best line.** Score every possible thing to say by how much it changes what the player
    should do in the next thirty seconds, and say only the top one, at a quiet moment unless it's danger.
    A "why?" hotkey expands the last tip for people who want the reasoning.
    **Not built** (a speech budget and the "why?" key are being worked on). The bench already counts talking
    too often.
14. **Learn from the player.** A thumbs-up and thumbs-down hotkey, plus noticing which advice is followed,
    so the coach leans towards what this player finds useful.
    **Not built.**
15. **Keep every fix fixed.** Every recorded game and every "Wrong tip?" report becomes a test that each
    new version is replayed against, with one accuracy score per release.
    **Partly built:** match records and the bench score real games; "Wrong tip?" reports become fixtures
    (`tests/data/bench/`) that the test suite replays. There is no score per release yet: nothing runs the
    bench on a fixed set of records for each version.

## The first numbers (the bench, 2026-11-11)

Scored with `python -m doty.bench` on the session logs of real past games. A score is 100 minus a weighted
share of lines that failed a check; the target for a first bar is under 2% of lines failing and under 10%
repeats. Aggregate numbers only:

| What was scored | Score | Lines failing a check | Lines said a minute |
|---|---|---|---|
| The two Turbo games of 2026-11-10, game 1 | 79.4 | 30.3% of lines shown | 3.9 |
| The same evening, game 2 | 85.4 | 25.8% | 3.7 |
| An earlier 29.5-minute session | 67.5 | 69% | 7.0 |

Across these logs the biggest failure counts were talking too often (the rate and the coach's own budget) and
naming one item too many times. These games were played before most of the eleventh and twelfth passes, so they
are the "before" mark, not a judgement of today's version. The next real game, with the record on, is the
"after".

**The scripted match,** today's version: `python -m doty.bench --scripted` scores **100.0** (44 lines said of
63 shown in 26.5 minutes, 1.66 a minute, none failing). That is the demo's scripted match with the rules only:
the AI coach isn't replayed and the lineup is unknown, so it shows the rules don't contradict the game data on
that match, not that the coach is accurate.

## What I need from you

1. **Play one real game with this version.** The match record is on by default, so nothing to set up: the
   game is kept in `%LOCALAPPDATA%\Doty\records` and `python -m doty.bench --latest` scores it against the
   numbers above.
2. **For calibration, one game with capture mode on** (Settings > Advanced > Capture matches). It saves frames
   of the game, which the record doesn't. Those frames are what the map and HUD positions of the drawings, the
   minimap reader and the dead-hero reading get checked and corrected against.
3. **Press "Wrong tip?" every time a line is wrong.** Each one becomes a test.
4. **Which role you play each game** would settle builds by role fastest if Doty asked once at the horn. Yes or
   no?
