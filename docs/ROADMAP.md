# Where Doty goes next

## Built in November 2026 (after the first live match)

- **Honest coaching:** confirmed lineups (two agreeing reads, your hero anchors the side), tips naming heroes
  not in the game or items your hero doesn't buy are dropped, late tips and drawings are dropped, no AI
  looks mid-fight.
- **Hero knowledge from data:** pro skill order per patch, which abilities take points, ability notes,
  typical items and when they usually land (OpenDota, dotaconstants).
- **Live, not late:** instant calls the AI prepares and Doty fires itself (`triggers.py`); a fast model for
  live looks; looks at calm moments (respawn, after a fight, level 6, an item done).
- **Prediction rules:** gank risk past the river, item pace, a game-start card, a laning gauge.
- **Post-game review:** deaths with likely causes, item timings, farm pace, three things to work on.
- **App:** four tabs (Heroes holds Draft and All heroes), drag-and-drop drafting with a filterable hero
  pool, one status icon everywhere, one Coach menu, voice settings that show only what applies.
- **Overlay:** the app's look, color-coded cards, a status badge you drag to move the cards.
- **Voice:** Kokoro's Warm tone, the Nicole voice, and Kitten as a softer free engine.
- **Tooling:** capture mode, "Wrong tip?" reports, an updater (off until the repo exists), contributor docs.

## Built in the second pass

- **A cleaner free voice.** Kokoro's vocoder leaves steady whistles at 4.8 and 9.6 kHz (15-20 dB above the
  sound around them, in every voice) and a top end far brighter than real speech. `kokoro.clean` notches
  the whistles out, tilts the top end toward natural speech (ANSI S3.5) and tames harsh "s" sounds only
  while they spike. Tones: Warm, Clear (brighter), Bright (raw).
- **Item timing in the review.** Each core item next to when half the hero's players have it; one 3+
  minutes late becomes a "what to work on" point, and the Last game card lists them.
- **Laning helper.** A deny nudge (2:30 or 5:30, if you farm but never deny) and a last-hit check at 5:00
  against your hero's pace. Creep aggro waits for the screen readers (the game data has no creeps).
- **A fresh-eyes bug pass (36 fixes, each with a test).** Among them: last match's tip could replay in the
  next; "Monkey King Bar" counted as the hero Monkey King and Anti-Mage's Blink as Blink Dagger, so good
  tips were dropped; prepared calls fired on the death screen or were lost when two came close; one
  network hiccup could silence the voice for a session; capture mode could save other apps' windows; a
  typo in doty.toml reset every setting; a restart mid-match cut the review in half and cleared the draft;
  the hero pool could freeze the window while it fetched matchups; Settings' Save could undo an overlay
  drag. Also: the phrase cache and capture folders now have size limits.
- **Get started has a voice step,** so a first run doesn't stay on the robotic Windows voice. It is the
  first-run wizard: connect Dota, add an AI key, pick a voice, try the demo.

## Built in the third pass (live speed)

Measured first, on the logs of real matches: the AI answers a live look in about 2 s (Haiku: 1.1 s to the
first word), so most of the delay was elsewhere: a look only every 15 s, tips 30-45 words long (10 s to
say, the action last), the same tip again and again ("buy a TP scroll" twenty times in one game), tips
about a moment that was over before they were said ("Jakiro is stunned in front of you"), and black
screenshots in the draft.
- **Said as soon as it's written.** A live tip is spoken the moment its sentence is done, while the
  prepared calls and drawings are still streaming in. Live tips are one sentence (18 words at most).
- **Never late.** The voice drops a line it couldn't start in time (tips 6 s, prepared calls 3 s, alerts
  8 s) instead of talking about the past. Prepared calls cut in over whatever is being said, and are voiced
  ahead the moment they're armed, so they play instantly.
- **Nothing twice.** A live tip that says again what was said in the last two minutes (by the coach or by
  an alert) goes to the feed, not your ears. A reply that corrects itself ("Wait, ...") keeps one answer.
- **The right things for a slow tier.** The coach is told its words land 2-4 s late: fight moments go
  into prepared calls, the spoken tip is what's still true in 15 s. Casting your ultimate counts as a
  fight, so no look starts (and no tip lands) mid-fight at full HP.
- **Who's in the game, in seconds.** The top-bar reader is on: it read all ten heroes in about 19 ms a
  frame from 75 s before the horn in a real match, while the AI never got the lineup. Two steady reads
  confirm a hero; your own hero still anchors the side. With all ten known, it also reads who's dead.
- **Hero knowledge.** Each look carries a match brief (cached, so it costs a tenth after the first):
  every enemy's abilities with cooldowns and what goes through BKB, and when their players usually finish
  their big items. The opening look waits for the lineup (until 0:30 before the horn).
- **Predictions, instantly.** The enemy's usual power spikes (OpenDota item timings for their heroes) on a
  card, plus a heads-up shortly before the first three. "Two of them are dead: push a tower", and "your team
  is down: back off" when you're deep. Prepared calls can use enemies_dead, allies_dead, ult_ready, day and
  night.
- **Black screenshots.** A black capture is noticed; Doty tries the window's own picture (PrintWindow),
  which also sees Dota behind another window, and otherwise tells the coach it has no screenshot.

## Built in the fourth pass (live skills, hero knowledge, real-match edge cases)

Researched first: the current patch is still 7.41f (Valve's patch list, checked 2026-11-09), so the timing
table holds; competitors' live features (Dota Plus's death summary and bracket comparisons, Dota Coach's
voice and timers) and OpenDota's free endpoints shaped the list below.
- **Calls about this very moment.** The coach can now prepare calls on items you can press right now
  (`ready item_manta`), on what's on your hero (silenced, stunned, hexed, smoked...), on burst damage
  (HP lost in the last two seconds) and on buyback while dead: "silenced and Manta ready: Manta now",
  "a third of your HP gone in two seconds: Glimmer yourself". Doty fires them instantly.
- **Ready when you respawn.** A look starts three seconds before you respawn, so the tip for what to do
  next lands as you're back instead of two seconds later.
- **What really killed you.** The death look gets your last ten seconds from the game data: how fast the HP
  went, how long you were stunned or hexed, active items you had ready and never pressed, where you were.
  An Aegis or Reincarnation revive no longer counts as a death.
- **The game plan.** From each hero's public win rate in short and long games: "their lineup is stronger
  late: win the game early" (or the reverse), said once the lineup is known, and given to the coach.
- **Hero knowledge.** Each enemy spell in the match brief says whether a dispel removes it (any / strong
  only / none), so a "Manta it off" tip is only given when it works. Dead heroes come with how long they've
  been dead.
- **Real-match fixes** (from an edge-case review): the Windows voice now drops late lines instead of
  queueing them 10-20 s behind; back-to-back bot games (which all report match id "0") start fresh; a
  reconnect no longer starts a wrong look ("you just reached level 6"); no "unspent gold" on the victory
  screen; arcade games get no Dota timers; Ability Draft no longer nags about skill points; a second
  pick-off seconds after the first is called; last game's AI build isn't reused.
- **A second review of this pass's code** found seven more, all fixed with tests: a disconnect no longer
  wipes the coach's memory of the match; an Aegis revive no longer sets off "buy back" calls or the
  respawn look; the post-game review ignores Aegis revives and keeps back-to-back same-id games apart; calls
  about your respawn timer wait until you're dead; the next pick-off is called after you respawn; Armlet,
  Radiance and Midas are never "items you forgot to press"; the brief's game plan always matches the card.

## Built in the fifth pass (faster, simpler, two more reviews)

Researched first: Haiku 5.5, the live-look model, thinks before it answers unless told not to; the prompt
cache can be written ahead of the first request; pace is what makes a spoken warning sound urgent; and
OpenDota's item timings carry wins as well as games. The patch is still 7.41f.
- **Faster live tips.** Live looks no longer think before answering (allowed at Doty's effort level), and
  the prompt cache (system prompt and match brief) is written while the opening look runs, so the match's
  first live look doesn't start cold. Live tips are at most 14 words (about five seconds of speech).
- **Save calls, no AI needed.** A quarter of your HP gone in two seconds, or low and still dropping, with a
  save item ready: Doty names it ("Glimmer yourself", "BKB now", "Force Staff out", "Wand now" with enough
  charges); silenced mid-fight, the dispel ("Manta the silence"). Once per fight, voiced ahead and said a
  little faster; the AI's prepared calls wait their turn after one.
- **Simpler speech.** Every fixed line is shorter and starts with the action ("Head back, no TP ready",
  "Two enemies dead. Push a tower.", "They scale better. Win early."). Runes that spawn together are one
  line. Mid-fight, timers and shopping lines show but aren't said; nothing is said in a pause. "Stack in 7"
  instead of "Stack now" seven seconds early.
- **Item timings with their odds.** The coach sees how often your hero's players win when the next core
  item lands by each time, and the review puts a number on a late item ("players who finish it as late as
  you did win 40%; by 20:00, 55%").
- **Fixes from a review of the live pipeline (11):** the death tip was said again before respawning; a
  connection dropped mid-answer left the card hanging; lines only shown on screen counted as said and
  muted real tips; a slow reply could still be spoken 15 s after its screenshot; "Chatty" coaching repeated
  itself; calm moments were lost when a look couldn't start that second; prepared calls stopped if the
  coach went off; a prepared call cut off the rest of an answer you asked for; a vanished audio device made
  every line "too late"; a coach start-up error could stop the whole engine; non-Claude providers
  downloaded their whole model list every 45 s.
- **Fixes from a review of game state and rules (7):** "Buy Yasha" after Manta (parts of what you own) and a
  second pair of boots; shopping nags in pauses; a failed OpenDota download (your log shows HTTP 500s)
  counted as done for the session, and empty answers were cached for a day; two bot games with id "0"
  merged in the review; the same pick-off called twice when the reading dropped out; a cache file locked by
  another part of Doty could wipe hero and item data for the session.
- **Leaner:** removed code nothing used (an old screenshot helper, an unused card style, an animation that
  never ran, a check word the AI could write that never came true); a launch without a match no longer
  leaves an empty log file.

## Built in the sixth pass (learning, levels, cost, reliability)

- **The Learn tab.** Getting better at Dota, at your level: your plan (the three things that win most at your
  rank, plus your last game's review), last-hit targets and a Demo Hero drill, and tips on how strong players
  see the game, laning and last hits, leaving lane, farming, items against heroes, drafting and counter-picks,
  fights, dying less, climbing ranked and quick wins. Fact-checked against patch 7.41f (Shiva's Guard no longer
  cuts healing; Demo Hero is on each hero's page). Links to it from the post-game recap.
- **Five player levels** (New, Learning, Climbing, Experienced, Top), picked on Learn, in Get started or in
  Settings, or taken from the rank on the Profile page. New players hear the basics out loud in their first
  minutes; learning players hear the laning and farm checks; experienced and top players hear fewer shopping
  and skill reminders. The coach's wording changes with the level.
- **No more made-up skill points.** In a real match the AI read the level-up arrows off the screen and said
  "a point is unspent" every few seconds. A live tip that claims a point Doty's own count doesn't see is now
  dropped, and the count is careful (it doesn't guess past level 16, where some levels give no point).
- **OpenDota that doesn't go blank.** Real logs show OpenDota answering HTTP 500 at times. Doty now asks again
  after a short wait, uses its saved copy at once while a fresh one downloads in the background, stays under
  the free limit (60 a minute), retries what failed instead of giving up for the session, and the Heroes tab
  says so (with Try again) instead of loading forever.
- **What the AI costs, honestly.** The Coach status shows this game's looks and cost (Claude's prices); the
  presets' estimate is from today's prices with live looks on Haiku 5.5: about 8¢ a 40-minute game, not the
  "$1" it used to say. A routine look is skipped when nothing has changed since one that had nothing to say.
- **"Why?" on each death** in the recap asks the coach about that death.
- **From a UI review:** a faded message no longer blocks clicks (it sat over the Draft page's hero pool);
  restarts keep Mute and hidden cards; a voice download no longer restarts Doty mid-match; with the AI coach
  off, Pause, Mute and Hide are still one click away; the demo button has its own row; the overlay's cards dim
  only under the cursor and the status badge's panel no longer catches clicks meant for Dota; spinners stop
  while hidden; Get started notices a key saved in Settings; dead UI code removed.

## Built in the seventh pass (the look)

- **Splash:** the hairline seam in the logo's D is gone (its bowl is one outline now; a new icon file,
  `doty-9.ico`), and the launch screen is centered, with a glow behind the mark, the status centered over a
  loading rule, and the version and patch.
- **Learn:** a topic menu (or chips on narrow windows) with each topic's color and an item picture, search
  across every tip, an "Every level" switch, tips as cards with the items and heroes they're about, the plan as
  colored tiles, big last-hit targets, and **Read to me** in Doty's voice (a reader voice while Doty is paused).
- **Profile:** a header with the rank medal and stat tiles that count up, a form chart of the last 20 games,
  most-played heroes with records, recent games with result stripes and score bars; designed empty, loading
  and error states.
- **Heroes:** instant search with highlights (type anywhere), attribute/role/melee-ranged chips, tiles that
  scale with the window and show win rate, and a detail page with stats by rank, a build timeline, ability
  cards, matchups as portrait rows, and prev/next.
- **Settings:** a section rail with icons and colors, an icon per setting, grouped panels, search (Ctrl+F),
  unsaved-change marks and a save bar that lists them (Ctrl+S), and the cost of a game beside the look rhythm.
- Bigger header tabs.
- **The game's own rank medals** on Profile (fetched like hero art, never bundled).
- **This patch, for the coach:** Valve's patch notes for the latest letter patches (7.41c-f) go into the match
  brief: "Changed lately" for each enemy, and your own hero's changes.
- **Doty in 16 languages:** `doty/i18n.py` translates Qt's text setters and painted text from catalogs in
  `doty/locales/` (1,155 texts, from `tools/extract_strings.py`, checked by `tools/check_locales.py`): the screens,
  the Learn lessons, the alerts it says, push-to-talk (multilingual Whisper) and the AI coach. A Windows voice in the
  language is picked when there is one. Text with numbers in it uses `i18n.tr("{n} settings", n=...)` templates;
  `tests/test_i18n.py` fails when en.json is out of date or a language is missing a text.
- **A calmer, realistic demo:** 6x speed, lines never cut each other off, and the first Roshan at 15:00.
- **Coach page:** the recap leads with what to work on, in bigger type with room between items; the Coach menu
  and the demo button share a row; Keys to remember folds away beside Plan your next hero.

## Built in the ninth pass (game sense, the live strip, faster drawing)

No new match logs (the last real match is still 2026-11-07's); this round worked from a full read of the
code and from the log of a demo run with the real AI.

- **Items, part by part** (`Knowledge.parts_of`, `cost_left`, `missing_parts`, `next_buy`): Doty knows what an
  item is made of and what's left to pay once the parts you hold are counted. The shopping call names the
  part your gold covers, then the finish; the pace reminder, the coach's `next_item` and the prepared calls'
  new `afford <item>` use the same number.
- **Cleaner builds:** an item on the way to a bigger one (Perseverance, Yasha) is folded into it in a hero's
  usual items, so the start card, the pace reminders, the review and the coach talk about Battle Fury and
  Manta Style.
- **Role and place** (`knowledge/roles.py`, `knowledge/mapinfo.py`): the role from the lane and last hits;
  where you are in words. Both go to the coach; the farm reminders skip supports.
- **A burst warning** ("Get out now") when a quarter of your HP goes in two seconds and there's no save
  item to press.
- **The coach is told more, and checked more:** the Town Portal Scroll's state, item cooldowns and charges,
  the next spawns with their clock times, the next item's real price and when it lands, the answers to this
  enemy lineup and the allies' big spells. A tip or call that says to buy what you have, or a second pair of
  boots, is dropped (the demo log had "Buy Power Treads now" three times to a player in Phase Boots).
  Late in the game the shopping reminders keep buyback.
- **The live strip** on the overlay: countdowns to the next spawns and the item being built.
- **Drawings:** only the patches with marks are repainted (the layer used to repaint the whole 3440x1440
  window on every frame), the pointer darts in from nearby and the marks start sooner, screen drawings come
  down when you walk on, an arrow's label is readable again (it was flooded with the arrow's color), and
  everything hides while another app covers Dota.
- **Speed:** the top-bar reader grabs only its strip of the screen (about a twentieth of the frame); the
  coach's screenshot takes about 70 ms instead of 110; lines with names in them ("Buy Ogre Axe", "Level
  Blade Fury") are voiced ahead like the fixed ones.

Still unverified in a real match: the role guess at low last-hit counts, the burst line's timing, and how
the strip sits against the real HUD.

## Built in the tenth pass (Phase 0 of `NEXT_PLAN.md`: Turbo, one shopping answer)

The plan in `docs/NEXT_PLAN.md` found that every recent match on the developer's account was Turbo, and
that Doty coached Turbo with normal-game numbers. This pass is that plan's Phase 0, except the recorded
match (0.1), which needs a real game.

- **Turbo is detected and coached as Turbo** (`knowledge/mode.py`): from a setting, from the match
  (experience per minute), or from your recent matches. Item timings, enemy spikes and build phases on a
  Turbo clock; buyback from 15:00; Roshan and the Aegis on Turbo's timers; no courier or secret-shop lines;
  the coach is told the rule differences. The top bar shows TURBO.
- **One answer to "what to buy"** (`Knowledge.shopping`): the rules, the strip, the coach and the app's Buy
  Next panel read the same list. The panel used to compute its own and showed three items as "Buy now" with
  gold for one.
- **The plan no longer skips the core you haven't built:** a phase's items come after the big ones of the
  phase before. On real data, a Juggernaut with only Phase Boots at 16:40 used to be shown Manta Style, then
  (in Turbo, where 16:40 is late game) Swift Blink; now Battle Fury, and in Turbo Manta Style. Plain Boots of
  Speed no longer make Power Treads "the item you've started" on a hero who builds Tranquil Boots.
- **Demo games don't count as yours** in Learn; reviews record their mode.
- **Turbo games aren't scored** on the Profile (the score compared Turbo gold with normal-game benchmarks).
- **LIVE goes out at once** when the demo is stopped, and 12 seconds after Dota stops sending (it stayed
  for 35).
- **The feed uses the overlay's tags** (LEVEL, GOLD, DANGER), not the rules' category names.
- A 7:30 item timing was shown as "7:00" on the start card and the pace reminder.

Also fixed on the way (found by a second read of the change): a consumed Aghanim's Blessing no longer stays
"the item to finish" for the rest of the game; Travel Boots count as having boots; the Buy Next panel says
it's loading, not "you have the usual build", before the hero's data is in.

Known gap: a normal game played as a solo core and a Turbo game played as a support give the same experience
per minute, so there the match can't correct what your history says. The TURBO tag shows what Doty decided.

Still unverified, all waiting on one recorded Turbo match:
- `mode.PACE` (items in about half the time) is derived from Turbo's doubled gold and experience, not measured.
- The experience-per-minute thresholds that tell Turbo from normal.
- Turbo's neutral-tier times (taken as half of 7.41's) and the Tormentor's (not announced at all).
- Whether the game data has a field that gives the mode away after all (`sections.json` from a capture).

## Built after the tenth pass: choosing a language, and voices that speak it (2026-11-10)

- **Your language is asked first.** A new install opens on six languages by flag and own name (English, Russian,
  Simplified Chinese, Spanish, Brazilian Portuguese, Indonesian) with the other ten under "More languages"; the
  one your PC is set to is preselected. A flag in the top bar reopens it from any page, and Settings shows the
  same list with flags. Someone already on English is asked once too.
- **Why those six:** there's no official count of Dota players by language. Russia is the largest single country
  in every source found, then China, Spanish-speaking South America, Brazil and Southeast Asia (Indonesia; the
  Philippines mostly plays in English). The sixth is the least certain; `languages.FEATURED` is one line.
- **Kokoro speaks Spanish, Brazilian Portuguese and French** with its own voices for them (seven voices, 0.5 MB
  each, fetched only for that language). Checked: the phonemes for sample lines, every file's SHA-256, and that
  speech comes out at a natural pace (about 16 characters a second). **Not checked: how they sound.** Nobody has
  listened to them yet; Play sample in Settings > Voice does.
- **Each voice service is labelled with the languages it speaks** (Settings > Voice, a row of flags each), and the
  language choice says which voice will speak the one you point at.
- **No more wrong-language speech:** ElevenLabs' fast model isn't asked for Thai, Kitten and Kokoro aren't loaded
  for a language they don't have, and the Windows voice stays quiet (and says what to install) when it has no
  voice for the language, instead of an English voice reading Russian. The status says so when coaching starts,
  not at the first alert of a match.
- **The Windows voice now finds a speech pack's voice.** Windows keeps two lists of voices, and Doty only read the
  older one, which has nothing for Indonesian, Thai, Vietnamese or Turkish. It reads both now. Checked on this PC
  with an English voice from the newer list (real audio came out); not checked with one of those four languages,
  which aren't installed here.
- **Switching language with Kokoro already downloaded** fetches that language's voices by itself (1 to 2 MB) when
  Doty next opens, and switches over when they're in.
- **Restarting into a new language is reliable.** The new copy used to find the old one still closing, take it for
  a running Doty and exit, leaving nothing open. It now waits for the old one to finish.
- **A hand-typed language** ("EN", "pt-BR") in doty.toml is read as the language it means everywhere.

The last five points came from a second read of the change, and were fixed with it. So were two smaller ones: a
settings file with a typo no longer brings up the language choice (and isn't saved over), and Get started no
longer offers "one 0 MB download".

Not done: Kokoro's Chinese and Japanese voices (they need a separate text front end); a free natural voice for
Russian, the largest group, which no model Doty ships has; and Ukrainian, which Windows has no voice for at all,
so only ElevenLabs speaks it. Kitten chosen in Spanish, Portuguese or French doesn't switch itself to Kokoro: the
status and Get started say to pick Kokoro.

## Built in the eleventh pass (your own build, and a switch per subject; 2026-11-10)

From two real Turbo games on Spirit Breaker. Details in `ARCHITECTURE.md` ("The player's own build") and the
numbers in `ACCURACY_PLAN.md`.

- **Boots of Bearing and Guardian Greaves are boots.** "Buy Phase Boots" was said 22 times to a player
  wearing Boots of Bearing.
- **Doty takes no for an answer.** An item you buy around leaves the build; "You can afford X" is said twice,
  not forever; "skip Black King Bar" (voice), Ctrl+Alt+X and a button on Buy Next drop an item at once.
- **A switch for each subject** Doty brings up: items, skill points, farm, timers, enemy timings, gold,
  death reviews (Settings > Alerts > What Doty suggests).
- **The coach's lines are checked harder:** "buy now" against your gold, "buy a TP scroll" against your TP
  slot, an item brought up more than three times, streamed answers sentence by sentence, tags written on the
  answer's line, a change of mind mid-sentence, and prepared calls that are late or repeat themselves.
- **The build never runs out,** and items hardly anyone buys on the hero aren't in it.
- Tests: `tests/test_own_build.py`.

## Built in the twelfth pass (judgement, your way, hero cards, records and the bench; 2026-11-11)

One rule throughout: the AI supplies judgement, code supplies the facts. Details in `ARCHITECTURE.md`
("Judgement from the AI, facts from code"); status against the plan in `ACCURACY_PLAN.md`.

- **Counter items judged against the lineup** (`knowledge/judgement.py`). Black King Bar, Linken's, Pipe,
  Monkey King Bar, Silver Edge and the other situational items each get a score and a short reason from what
  the enemies' spells do ("it blocks Lion's Earth Spike and Hex"). No ability data, no verdict. A weak one
  leaves the build once all five enemies are known; only a strong, hard-data one is ever added to it. The parts
  in your bag decide the item you're building, in Doty's plan or not.
- **"When I go my own way"** (Settings > Alerts): say it once, with a reason from this game (the default),
  follow me silently, or keep reminding me. The rules and the coach share one record, so you hear an argument
  once.
- **Your instructions to the coach** (Settings > AI coach): free text the AI reads on every look as your
  preferences. On saving, what can be a setting (items never to suggest, your build on a hero, subjects off,
  how much it talks) becomes one, so the rules obey it too, with or without the AI.
- **Hero cards** (`knowledge/cards.py`, `knowledge/study.py`): per hero and patch, from public statistics only:
  builds by position, item timings with win rates, last-hit pace by rank, skill order, matchups, game length.
  The coach reads the card in every look, the build follows it for your role, and the 10:00 and 20:00 farm
  checks compare you with your rank and the one above. A background thread studies heroes politely (your
  match's heroes first; at most 1,200 requests a day); off in Settings > Advanced.
- **Drawings: thin, see-through, and placed by code.** A 2 px line at 65% (both settings, Settings > Overlay).
  The coach names a tower, rune, Roshan pit, your hero or a HUD slot and Doty places it; a mark on your hero
  follows you. Map and HUD positions aren't yet checked against a real frame.
- **A record of every match, on this PC** (`match_record.py`, on by default): the game data without identity,
  and every line Doty showed; no screenshots; the last 30 matches or 200 MB; "Delete records" in Settings >
  Advanced. `SECURITY.md` says exactly what is kept.
- **The bench** (`python -m doty.bench`): scores every line Doty says against the game as it stood, on match
  records, captures, past session logs or the scripted match. The first numbers are in `ACCURACY_PLAN.md`.
- **Smaller:** the session log names the rule behind each line; the build's self-test turns off the study, the
  record and the game-mode lookup explicitly; `doty.example.toml` lists the new settings.
- Tests: `tests/test_item_judgement.py`, `tests/test_coach_etiquette.py`, `tests/test_bench.py`,
  `tests/test_match_record.py`, `tests/test_drawings.py`, `tests/test_cards.py`, `tests/test_card_wiring.py`.

## Built in the thirteenth pass (how much it talks, the enemy, one thing to practise; 2026-11-11)

Details in `ARCHITECTURE.md` ("What is heard, the enemy, and one thing to practise").

- **One gate for everything said out loud** (`speech.py`): danger calls and your questions always; nothing else
  in a fight; a budget a minute with gaps between lines; one item named in buy lines only a few times; a
  line that can't be said soon is dropped, never said late; the line that most changes the next 30 seconds
  wins. **"How much Doty talks"** (Settings > AI coach: Automatic, Less, Normal, More) replaces "When it talks".
  The feed still shows everything.
- **"Why?"** (Ctrl+Alt+Y, or ask "why?"): the reason for the last tip, from Doty's own data where it has it.
- **Know the enemy** (`knowledge/threats.py`): a threat card per enemy hero, ranked for your hero and role, built
  from their spells; one spoken line for the biggest threat once all five are known; the death look names the
  spells that could have done it. The ability facts were reviewed for every hero (`judgement --audit`).
- **One thing to practise per game** (`knowledge/focus.py`, `rules/focus.py`): picked from your recent games,
  said at the horn with a target, checked once or twice, graded in the recap with the trend over your last
  games. The history (`%LOCALAPPDATA%\Doty\focus.json`) holds no ids.
- **The recap shows "How Doty did"**: the bench's verdict on the last match record, in plain sentences.
- Tests: `tests/test_speech.py`, `tests/test_threats.py`, `tests/test_focus.py`.

## Linux (partly done, November 2026: built and tested on Windows, not yet run on a Linux desktop)

Doty runs from source on Linux, with every Windows code path left as it was (`sys.platform` checks;
`tests/test_linux.py` runs the Linux branches with the platform, paths and X11 faked). What's built:

- **Folders:** settings in `~/.config/doty`, everything else (cache, logs, voices, reviews) in
  `~/.local/share/doty`, following `XDG_CONFIG_HOME` / `XDG_DATA_HOME`; Documents from `user-dirs.dirs`.
- **Steam and Dota:** `~/.steam/steam`, `~/.local/share/Steam`, Flatpak and Snap Steam, every library in
  `libraryfolders.vdf`, the native `linuxsteamrt64/dota2`; the launch option is read from the same
  `localconfig.vdf` as on Windows.
- **The Dota window, keys and mouse** through X11 (`doty/x11.py`, libX11 and libXi by ctypes, nothing to
  install): the window manager's own window list, XQueryKeymap for keys, XInput2 for the mouse's side buttons.
  Hotkeys are polled there (the game sees them too, unlike RegisterHotKey on Windows).
- **Screen capture** with mss over X11. No PrintWindow on Linux, so a window covering Dota is captured as is.
- **Overlay:** the same windows, outside the window manager's control (X11 bypass) and re-raised every 2 s.
  On a Wayland desktop Doty asks Qt for X11 (XWayland), since Wayland won't let a window place itself.
- **Voice:** ElevenLabs and Kokoro/Kitten as on Windows; the robotic backup is eSpeak NG or speech-dispatcher.
  Push-to-talk beeps through PipeWire/PulseAudio/ALSA; mics are listed from ALSA.
- **API keys** in the desktop keyring (Secret Service, via `keyring`), else a private `keys.env` (mode 600).
- **Shortcuts:** a `doty.desktop` entry in the applications menu (once, from a source checkout), tied to the
  window by its desktop file name.
- **Setup page:** a "Screen capture, overlay and hotkeys" check (X11, XWayland or neither) and the system voice.

Still to do:
- **A run on a real Linux desktop** (X11, then GNOME and KDE on Wayland): window finding, the overlay staying
  over the game (fullscreen too), side-button push-to-talk, and the screenshots not coming back black.
- **Wayland without XWayland:** no screen capture, overlay or hotkeys (Doty coaches from game data alone). A real
  fix needs the desktop portals (screen cast, global shortcuts) and a layer-shell overlay.
- **No installer or updates on Linux:** run from source (`pip install -e .`, update with git). PyInstaller could
  build a folder there, but there's no AppImage/Flatpak recipe yet, and Inno Setup is Windows-only.
- **Proton:** Doty looks for the native Linux Dota. Running Dota through Proton isn't checked.
- **Wording:** a few Settings and Setup lines still say "the Windows voice" on Linux.
- **GPU voice and GPU speech recognition:** `doty gpu-voice` is Windows-only; Whisper on CUDA isn't checked.

## Next

- **One real game with this version, then one with capture mode on** (`ACCURACY_PLAN.md`, "What I need from
  you"): the first scores the coach, the speech gate and the focus on a real match; the second checks the
  drawings' map and HUD positions (`hud.HUD_CALIBRATED`) and the screen readers against real frames.
- **Tune the gate on real games:** the budgets and `SCORES` are first guesses; the bench's rate and
  coach-budget checks on real records say whether they are right.
- **A score per release:** run the bench on a fixed set of match records for each version and keep the number.
- **The AI as strategist** (`ACCURACY_PLAN.md` point 5): a match plan the instant rules follow and you can veto.
- **Polish the translations:** a native speaker's pass over each catalog (they were machine-translated). Still
  English on purpose: hero, item and ability names, mic and language names. Still English by oversight: a few rare
  error and debug lines built at runtime (developer details, OpenDota error text).

1. **Calibrate the minimap reader (needs one captured match).** The top-bar reader works on real frames;
   the minimap's hero markers are still a guess. Once calibrated: missing-enemy counts ("three missing")
   as instant facts. Also check the dead-hero reading against captured frames.
2. **Check what the game feed really sends (same match).** Capture mode records every section Dota sends
   a player; if the minimap section lists only what your own minimap shows, Doty can use it.
3. **Creep aggro and lane equilibrium tips (after 1).** Need the creeps, which only the screen shows.
4. **Check the game's event feed (same capture).** If Dota sends Roshan kills and Aegis pickups to players
   with the team that took them (the kill feed shows it), Doty can time *their* Aegis ("their Aegis ends in
   30: fight after") and a courier kill. Not built until a capture shows the fields.
5. **A Doty data pack (once the repository exists).** One weekly file on GitHub with the OpenDota numbers
   Doty uses for every hero, instead of hundreds of requests: works offline and can't hit a rate limit.
6. **Spoken lines from a fixed list.** Live tips picked from ~60 pre-voiced lines ("Push top", "Buy BKB")
   would play instantly even on the CPU voice; the free-text tip would stay on the card.
7. **Code signing (before a wide release).** So Windows SmartScreen stops warning on first run. The route is
   SignPath Foundation, signing what GitHub Actions builds (`RELEASING.md`).

## Open-source release checklist

- [x] **License:** GPL-3.0 or later (`LICENSE`). Doty loads eSpeak NG (GPLv3) in its own process and the
      installer ships it, so a GPL license keeps the whole package simple.
- [x] **Repository:** public on GitHub; `app.update_repo` points at it, so the updater works.
- [x] No API keys or personal settings in the files (`doty.toml`, `dist/`, `build/` and the venv are in
      `.gitignore`; keys live in Windows Credential Manager).
- [ ] Remove `docs/brand/legacy/` (old generated art Doty no longer uses) to keep the repo small.
- [x] **Release:** `DotySetup-<version>.exe` and its `.sha256` on a GitHub Release (the updater needs both).
      From the release after 0.1.1, GitHub Actions builds them from the tag as a draft (`RELEASING.md`).
- [ ] **Code signing** through SignPath Foundation. Until then Windows SmartScreen warns on first run.
- [x] A short guide for players: [GUIDE.md](GUIDE.md), linked from the README.
- [x] `CONTRIBUTING.md` and issue templates ("A tip was wrong", "Something's broken").
