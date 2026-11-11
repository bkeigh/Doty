# Doty architecture

## Stack decision

**Python 3.11+ as one process, with a PySide6 overlay. No Electron, no web server, no cloud backend (yet).**

| Option | Verdict |
|---|---|
| Python script + PySide6 overlay | **Chosen.** The fastest to build and iterate, with first-class Claude, aiohttp, OpenCV and YOLO libraries. Qt draws a real click-through layered window, and the install is a venv. |
| Electron, like Cluely | Too heavy: ~1.4 GB, and Chromium competes with Dota for CPU and GPU. It also adds nothing GSI needs. |
| Tauri (Rust + webview) | Right for a polished v2 product: small binary, signed installer, auto-update. Port the overlay later; the Python core can stay as a sidecar or be rewritten. |
| C#/.NET | Strong on Windows (antonpup/Dota2GSI exists), but the LLM and CV ecosystem is weaker and it's slower to iterate. |

## Data flow

```
Dota 2 ──HTTP POST JSON ~10/s──► gsi/server.py (aiohttp, 127.0.0.1:53000, token-checked)
                                     │
                                     ▼
                              state.GameStateStore   (typed Snapshot + per-match memory)
                              │                 │
               (every update) ▼                 ▼ (death, game start, every 60 game-s)
                 rules/engine.py            coach/llm.py ──► backends.py: Claude / ChatGPT / Gemini / … (streamed)
                 timers · survival ·          │  ▲            screenshot of the Dota window
                 economy · progression        │  └─ BUILD: line feeds the rule engine's item plan
                 (0.1–0.8 ms)                 │
                          ▼                   ▼
                        AdviceBus (thread-safe fan-out)
              ┌──────────┬──────────┬───────────┬────────────┐
              ▼          ▼          ▼           ▼            ▼
          overlay.py  voice.py   console    session log   coach context
          (Qt cards)  (Kokoro /             (jsonl)       ("recent alerts")
                      ElevenLabs / SAPI)
```

The app (`doty app`, the desktop shortcut) wraps the same engine:

- `ui/controller.py` owns a `Doty` instance: start, stop, restart on settings change, and the demo match.
  It relays bus advice to the GUI thread through a Qt signal.
- `ui/window.py`: the top bar (wordmark, page tabs with a sliding underline, and Doty's status or, in a
  match, `_show_match`: LIVE, team, day or night, clock) over the pages. They poll `Doty.status()` every
  500 ms; it includes a `game` view (HP, mana, gold, item keys, next timers) built from the latest snapshot.
  - `ui/coach_page.py`: the home page, a left column beside a right rail (`_outer`). The column switches
    between Get started, the pre-game hub (last-game recap), top picks during hero selection, and the
    hero strip (`live.GamePanel`); the views sit in a `PageStack` (only as tall as the view showing). The
    screen view leads in a match and follows the view otherwise (`_view`). The rail holds
    `live.TimersPanel` and `live.BuildPanel` (in a match), the feed and the controls. A window taller
    than wide stacks it all (`_set_portrait`). Problem chips carry in-place fixes (`KeyEntry`,
    `Popover`, which opens above its button when there's no room below). `ui/pages.py`: Setup and
    Settings, plus `COACH_PRESETS` and `save_claude_key`. `ui/stats_pages.py`: Draft and Heroes.
    `ui/profile_page.py`, `ui/ai_settings.py`.
  - `ui/window.py` follows the game (`_follow`: hero selection opens Draft, the match opens Coach,
    `app.follow_game`), and Esc goes back. Status gains `post_game` and `win_team` for the recap.
  - Colors: `advice.tone()` gives every line one meaning (now / soon / coach / you / info), drawn with
    `widgets.TONE_COLORS` in the window and `overlay.CARD_TONES` over the game.
- `ui/live.py`: the hero strip, the timers, what to buy, and "What Doty sees": ~2.5 frames/s of the Dota
  window via `capture.FrameGrabber`, displayed locally in a painted broadcast frame, with Doty's latest
  coach line captioned over it (`ScreenView.show_line`). It films screen pixels, so it only runs while the Doty
  window can be seen and doesn't overlap Dota (`window._live_view_state`: "seen", "hidden", or
  "covering", which keeps the last frame marked PAUSED instead of filming Doty itself).
- `ui/art.py` + `ui/visuals.py`: hero portraits and item/ability icons from Valve's public CDN (paths from
  OpenDota `heroStats` and dotaconstants). Downloaded on first use by a 6-thread pool, cached under
  `%LOCALAPPDATA%\Doty\cache\art`, never bundled. Until an image arrives a portrait shows initials.
  `DOTY_ART=0` turns downloads off (the tests set it).
- `ui/motion.py`: page crossfade (`FadeStack`), the sliding tab underline, fold-out sections,
  shimmer placeholders and counting numbers. Everything runs under half a second.
- `overlay.py` + `hud.py` + `marks.py`: the in-game layer, in the app's look (cut-corner panels, condensed
  capitals).
  - **Advice cards** sit in a column placed from the game window's size (`hud.card_column`), color-coded
    and tagged by category (`overlay.card_style`: DOTY, TIMER, GOLD, LEVEL, DANGER).
  - **The status badge** (`StatusBadge`) is the one overlay window that takes the mouse (never the
    focus: `WS_EX_NOACTIVATE`). An eye or turning arrows show the engine's state (`Doty._overlay_status`);
    hover shows it in words; dragging it moves the column, saved as fractions of the game window in
    `overlay.position`.
  - **The coach's drawings** (`DRAW:` lines become `Mark`s) fill a click-through layer over the window.
    It's shown only while something is drawn. A pointer glides from the card to the first mark.
- `lineup.py` + `coach/llm.py`: keeping the AI honest.
  - **Lineup votes:** each `LINEUP:` read is one vote. Two agreeing reads confirm a hero; the player's own
    hero (GSI) anchors their side and flips swapped reads.
  - **Checks before showing:** live tips that are late (`WATCH_STALE_S`), arrive after a death or during a
    fight (`GameStateStore.in_fight`), name heroes outside a complete lineup, or suggest items the hero's
    players don't buy are dropped. Screen drawings are dropped once the camera has moved or a few seconds
    have passed; hero marks are recolored by team.
- `triggers.py`: instant calls the AI prepares ahead (`TRIGGER: <condition> => <line>`). Conditions are
  parsed from a small fixed vocabulary (never run as code), checked on every game-data update in
  `Coach.on_snapshot`, and fired once when they turn from false to true, within six game minutes. The
  vocabulary includes screen readings (`enemies_dead`, `allies_dead`, from `Doty._screen_signals`); a call
  that needs one waits while there's no fresh reading. It also has `ready <item>` (in the inventory, off
  cooldown, castable: `Snapshot.ready_items`), `hp_lost` (percent of max HP lost in the last 2 s,
  `GameStateStore.hp_trend`), `buyback` (dead and able to buy back) and what GSI says is on your hero
  (`STATUS_FLAGS`: silenced, stunned, hexed, disarmed, muted, broken, magic_immune, smoked), so the coach can
  prepare "silenced and ready item_manta => Manta now". `dead`, `respawn_s` and `buyback` exist only after
  a real death (`memory["death_open"]`), never during an Aegis revive. Clauses join with "and" in three
  values: one false part makes the call false even while another isn't known. Armed lines go to `Voice.prepare_now` so they're
  voiced ahead; a fired call cuts in (`Advice.cut_in`) and is dropped if it can't be heard within 3 s.
- Live speed (`coach/llm.py`, `voice.py`): a live tip is published the moment its line is complete
  (`_tip_ready`, `_early_tip`), before the TRIGGER and DRAW lines finish streaming; drawings join its card
  afterwards without saying it again. `Coach._repeats` keeps a tip that says again what was heard in the
  last two minutes (coach, calls or rule alerts, compared by content words) off the voice. Every spoken
  line can carry `Advice.stale_s`: the voice drops it when the audio already waiting would make it start
  later than that (alerts 8 s, live tips 6 s, calls 3 s). The Windows voice keeps its own queue, so the
  voice thread waits for each SAPI line to finish (unless one cuts in) before judging the next deadline.
  While you're dead, `Coach._pre_respawn_look` starts a live look 3 s before respawning
  (`PRE_RESPAWN_S`), so its tip lands as you're back; `_too_late` lets a look started while dead publish
  after the respawn.
- `coach/prompts.py: match_brief`: who's in the game and, per enemy, abilities with cooldowns (and which
  go through BKB) and when their players usually finish their big items (`Knowledge.enemy_notes`,
  `ensure_enemy`: itemPopularity plus up to three item timings), whether each effect can be dispelled
  (dotaconstants `dispellable`, as "dispel: any / strong only / none"), and once all ten are known a Timing
  line from `knowledge/curves.py`. Sent first in each request with `cache_control`, after the cached system
  prompt, so it's a cache read on every look after the first.
- `knowledge/curves.py`: the game plan. Each hero's public win rate in games under 25:00 and over 40:00
  (OpenDota `/heroes/{id}/durations`, fetched for all ten by `Knowledge.ensure_durations`), averaged per team;
  if one side gains 3+ points more from a long game, that's the plan ("their lineup is stronger late: win
  early"). Public hero statistics, nothing about the players.
- Deaths (`state.py: GameStateStore._note_death`): a death is the death count going up while you're dead
  (Aegis and Reincarnation flip `alive` without one; a count arriving an update before or after the flag is
  allowed for). `just_died` is set for that one update and drives the death look and `DeathWatch`.
  `death_summary` keeps what happened in the last 10 s from your own data (HP before, seconds stunned,
  hexed or silenced, active items you had ready and never pressed, where you were, TP ready), which the
  death look gets as `how_you_died`.
- New matches (`GameStateStore._new_game`): besides a new match id, hero selection after a played game or
  the clock jumping back 60+ s resets the match (bot and local games can all say "0"). `generation` counts
  resets, and the coach keys its per-match state on it (`Coach._match_key`). Rules don't run in hero
  selection, on the victory screen or in arcade games (`map.customgamename`).
- `rules/matchup.py`: `GamePlan` (the curves plan as a card and a line, once the lineup is known),
  `EnemySpikes` (a card with the enemy's usual power spikes, and a heads-up 40 s before the first three) and
  `PickOff` (two or more enemies greyed out on the top bar: push; two or more allies down while you're deep:
  back off; a moment ends once fewer are dead for 5 s, so a flicker isn't called twice).
- `review.py`: the post-game review. `MatchRecorder` sees every update (before the rules) and every line Doty
  says; at post-game (or when Doty stops) it saves `%LOCALAPPDATA%\Doty\reviews\match-<id>.json` with deaths
  and their likely causes, item timings (each core item against when half the hero's players have it),
  farm pace and three focus points. The Coach page's recap shows it.
- `match_capture.py`: capture mode (Settings > Advanced). Frames of the game window at deaths, fights, the
  horn and every 30 s, top-bar and minimap crops, and the raw game data to `Documents\Doty\captures`, for
  calibrating the screen readers. Off by default; nothing leaves the PC.
- `update.py`: checks `app.update_repo` on GitHub Releases, downloads `DotySetup-<version>.exe`, verifies it
  against the `.sha256` asset beside it, and runs it (Settings > App > Updates). Off until a repo is set.
- `vision.py`: Doty's own screen readers, without the AI. `TopBarReader` matches the ten top-bar portraits
  against Valve's portraits in the art cache (and flags greyed-out dead heroes) in about 20 ms on a
  3440x1440 frame; `MinimapReader` finds hero markers by color (not calibrated yet). `Doty._vision_loop`
  runs the top-bar reader once a second in a match. With `coach.vision = "on"` (the default),
  `Doty._use_reading` sends two agreeing reads in a row (portraits read at 0.6+ confidence) to
  `Coach.screen_read` every 3 s, which counts them in `Lineup.add` like an AI read; once all ten are known
  a second read compares only them, and greyed-out portraits become `memory["screen"]` (who's dead, kept 3 s)
  for the state summary's `dead_now`, the calls and `PickOff`. "shadow" only logs (`logs\vision-*.jsonl`).
- `capture.py`: screenshots. When Dota isn't the window in front, or the screen grab comes back black, the
  window's own picture is used (`win.print_window`, PrintWindow with PW_RENDERFULLCONTENT, ~70 ms at
  3440x1400). A capture that's still black is sent as a note (`views["unavailable"]`), never as an image.
- `kitten.py`: Kitten TTS Mini (KittenML, Apache 2.0) as a second free local voice, same shape as
  `kokoro.Kokoro` (voice.py picks it when `voice.engine = "kitten"`).
- Coach timing (`coach/llm.py`): live looks use `coach.watch_model` (Haiku 5.5) and a 1024 px screenshot;
  besides the regular rhythm it looks at calm moments (`_calm_moment`: respawn, fight over, level 6, a big
  item done), and never mid-fight (`GameStateStore.in_fight`: HP falling fast, or your ultimate just cast).
  The opening look waits for the lineup until `START_LOOK_BY` (0:30 before the horn).
- `knowledge/constants.py`: hero knowledge from data. dotaconstants gives ability descriptions (and which
  are innate); OpenDota's explorer gives the pro skill order per hero and patch (`SKILL_SQL`, cached a
  week), which also decides which abilities take skill points (`unspent_points`, `next_skill`); item
  popularity gives `typical_items` and `allowed_items`.
  - **One layout:** `hud.py` is the single source of HUD geometry. The coach's minimap crop and the
    overlay's minimap marks use the same rectangle, so a circle lands where the coach meant it.
- `knowledge/stats.py` + `draft.py` + `counters.py`: public hero statistics for the Draft and Heroes
  pages and for the coach's draft looks.
  - **Sources:** OpenDota `heroStats`, matchups and item popularity; dotaconstants abilities and items.
  - **Picks:** scored as the expected edge over 50%, meaning the hero's win rate at your rank plus its
    shrunk matchup advantages.
  - **Counter items:** `counters.py` is the hand-written trait table (lockdown, evasion, healing, …).
- `ui/brand.py` + `ui/theme.py`: the "broadcast" look. The mark (SVG, the app icon), the wordmark, the
  fonts (Barlow and Barlow Condensed), the shared shapes (a panel's cut corner, a button's leaning edge,
  the striped backdrop) and the palette (`theme.BRAND` cyan is Doty; `theme.GOLD` is money). The splash
  screen, panels, banner and installer art are all painted from these; there are no raster art files.
  The splash is a plain frameless window, not `QSplashScreen` (that one waits up to a second for Windows
  to report it exposed). The AI SDKs load in a background thread once the window is up
  (`coach/backends.py: preload`), so they don't hold up the launch.
- `shortcuts.py`: Start-menu and desktop `.lnk` files that run `pythonw -m doty app`. They carry the
  same AppUserModelID as the running window, so "Pin to taskbar" pins the shortcut.
- One instance only: a second launch asks the first, over a QLocalSocket, to show its window. It sends
  its code stamp (`ui/main.py code_stamp`, the newest source file's time, or the exe's for an install);
  if the running copy is older, it closes and the new launch takes over.
- Things that change your PC on their own (adding shortcuts once, rewriting an out-of-date GSI file) run
  only for your real settings file (`config.is_user_config`), never for tests or previews.
- Without a console (pythonw), output goes to `%LOCALAPPDATA%\Doty\logs\app.log`.

Threads:
- Qt on the main thread.
- asyncio (`doty-loop`) for GSI, rules, coach and HTTP.
- One voice thread: ElevenLabs streaming when a key is set, or Kokoro on the CPU once it's downloaded,
  with SAPI on the same thread as the fallback. Plus PortAudio's playback callback. In idle moments the
  voice thread prepares Kokoro versions of the rules' fixed lines.
- One Win32 hotkey thread.
- One push-to-talk poller, plus a 2-worker pool for the screenshot and Whisper.

Overlay updates cross threads through a Qt signal.

Push-to-talk path:

```
hold key ─► Doty hushes, beep, mic opens (8–29 ms), screenshot captured in parallel
release  ─► beep ─► Whisper on GPU (~110 ms) ─► green "YOU" card ─► coach.ask_question()
                                                            ─► streamed answer, spoken sentence by sentence
```

## The 300 ms budget

| Path | Budget | Measured |
|---|---|---|
| Dota emits update (`buffer` 0.05 + `throttle` 0.1) | ≤150 ms | needs a real-match capture |
| POST → parsed → all rules evaluated | <5 ms | 1.5 ms p50 / 2.8 ms p99 round-trip; rules 0.1–0.8 ms |
| Advice → overlay card | <16 ms | one Qt frame |
| Advice → speech starts (Windows voice) | <50 ms | `Speak()` returns in ~9 ms after warm-up |
| Advice → speech starts (ElevenLabs) | ~200–400 ms | Flash model ~100 ms + network + ~100 ms device buffer; cached lines skip the network. Needs a real key to measure |
| Advice → speech starts (Kokoro, a fixed rule line) | <5 ms | prepared ahead and cached on disk (25 lines in 19 s, once per voice and speed) |
| Advice → speech starts (Kokoro, a new line) | ~0.6–1.1 s | measured on a Ryzen 9 3900X, 6 ONNX threads: ~0.35 s of compute per second of speech, padding trimmed. Long answers speak clause by clause |
| Advice → speech starts (Kokoro on the GPU, a new line) | ~150 ms | RTX 4070 Ti via the `doty gpu-voice` helper process (startup ~5.5 s) |
| Timer alerts (runes, shrines, tormentors, neutral tiers, Roshan window) | n/a | fire **15 s early**, so the latency is effectively negative |
| Save calls (`rules/survival.SaveItems`: burst or silence + a ready save item) | <5 ms + voice | a rule, no AI; the lines are voiced ahead and said at 1.12x (`voice.URGENT_PACE`) |
| Coach hotkey, cached answer | <50 ms | instant from the speculative cache (tested) |
| Coach hotkey, cold | ~1–2 s | time-to-first-token depends on the model; text streams in and speech starts at the first sentence |
| Push-to-talk: mic opens on press | <30 ms | 8–29 ms (MME, 16 kHz) |
| Push-to-talk: release → transcript | ~100 ms | 86–136 ms, Whisper small.en on an RTX 4070 Ti; ~740–950 ms base.en on CPU |
| Push-to-talk: release → coach starts answering | ~120 ms + Claude TTFT | 110–270 ms against a fake 0 ms-TTFT Claude |

The LLM never sits on the alert path. That's the main lesson from Cluely and Clicky: both feel slow
exactly where they put a network round trip between the event and the response.

## Player levels (`CoachConfig.level`, `knowledge/lessons.py`)

- `new`, `learning`, `some`, `experienced`, `pro`. `level_from_rank` (on until the player picks one) lets the
  Learn tab set it from the Profile's OpenDota `rank_tier` (`lessons.level_for_rank`).
- `RuleEngine.level_of` reads it live: `SPEAK_MORE` raises a level's teaching rules (level 6, farm and laning
  checks) to spoken; `SPEAK_LESS` keeps shopping and skill reminders shown-only for experienced and top players;
  `progression.Basics` says the basics once each for `new`.
- The coach adds `prompts.LEVEL_NOTES[level]` to each request.

## OpenDota (`Knowledge._cached_json`)

- Fresh cache file: used. Old one: used at once, a new copy downloads in the background (`_refresh_later`).
  None: the caller waits for `_download`, which retries 5xx/429/timeouts (`RETRY_DELAYS_S`), never retries a
  4xx, doesn't save empty answers, and is paced to `REQUESTS_PER_MIN` (`_pace`).
- `ensure_hero` / `ensure_enemy` count a hero as done only when every part came back; otherwise the 5-minute
  back-off and another try.

## What's said out loud

- Mid-fight (`GameStateStore.in_fight`) the rule engine marks priority NORMAL and lower as shown-only, and
  in a pause it returns nothing. Spawns at the same second share one line (`rules/timers.joined`).
- A live tip's voice deadline counts from when its look began (`WATCH_STALE_S`), not from when it arrived.
- An interrupting line drops queued lines that have a deadline (alerts, tips) but keeps the rest of an
  answer the player asked for, which plays after it (`Voice.say`).
- A live tip that claims a skill point or talent Doty's count doesn't see is dropped (`Coach._false_skill_claim`).
- A routine look is skipped (at most `MAX_SKIPS` in a row) when `Coach.situation` is unchanged since a look that
  had nothing to say. `Coach.spend` tallies each match's looks, tokens and dollars (`backends.cost_usd`, `PRICES`).
- The coach's repeat filter (`Coach._heard`) counts only what the voice says (`Coach.spoken_max` =
  `VoiceConfig.min_priority`), plus the coach's own spoken answers.

## Languages and who speaks them (`languages.py`)

- **One list:** `languages.LANGUAGES` (code -> English name, own name), `FEATURED` (the six offered first) and
  which voice service speaks which. It imports nothing from Doty, so the first-run choice can show before the
  slow imports. `coach/prompts.LANGUAGES` is the same object.
- **The choice:** `ui/main.py` asks once (`language_dialog.first_run_wanted`: your real settings, English, and
  `app.language_picked` not yet set), before `i18n.set_language`, so nothing is built in the wrong language and no
  restart is needed. The flag in the top bar (`MainWindow.choose_language`) and Settings open the same list; those
  save and relaunch. `DOTY_LANGUAGE_PROMPT=0` turns the opening choice off.
- **The dialog's words** follow the language you point at: `i18n.text_in(code, text)` reads another catalog, and
  `with i18n.as_is():` stops Qt's patched text setters translating them back into the app's language.
- **Flags** are painted (`ui/flags.py`): no image files, and Windows has no flag emoji.
- **Kokoro's other languages** (`kokoro.LANG_VOICES`: es, pt, fr): the voices Kokoro v1.0 has that need only
  eSpeak NG. `files_for(language)` adds that language's voice files to the download, so `installed()` for an
  English install is unchanged. `phonemize(language=)` uses eSpeak's voice for it and misaki's plain eSpeak
  mapping (`_E2M_PLAIN`); the English rewrites ("4:00" as "4 minutes") aren't applied. Kokoro's Chinese and
  Japanese voices need their own text front ends (misaki[zh], misaki[ja]) and aren't offered.
- **The voice thread** (`voice.py`): `kokoro_voice(cfg, language)` picks `kokoro_lang_voice` for es/pt/fr; a
  local engine that can't speak the language isn't loaded (and with an ElevenLabs key, ElevenLabs speaks
  instead); ElevenLabs is skipped when its model lacks the language (`languages.ELEVEN_MISSING`); and when the
  Windows voice has no voice for it (`SapiSpeaker.speaks_language`), lines aren't spoken and `Voice.problem`
  says why and what to add, from start-up (`_note_if_nothing_speaks`).
- **Windows' two voice lists:** `SpVoice.GetVoices()` returns the desktop voices only. A language's speech pack
  installs "OneCore" voices under `HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices`, and for Indonesian, Thai,
  Vietnamese and Turkish that's the only kind there is. `voice._onecore_voices()` enumerates them through
  `SAPI.SpObjectTokenCategory`; `SpVoice` speaks with those tokens like any other.
- **Kokoro's language voices arrive by themselves** when the base model is already here:
  `Controller.fetch_language_voices` (1.5 s after the window opens, real settings only).
- **Restarting itself** (`window.relaunch_doty`): the new copy is started with `DOTY_TAKEOVER=1`, so it waits for
  the old one's mutex to go (`main._wait_until_gone`) instead of asking it to come to the front.

## Game sense from the game data (the ninth pass)

- **Items are made of parts** (`knowledge/constants.py`): `parts_of` (dotaconstants `components`, plus the
  recipe when it costs gold), `cost_left(item, owned)` (the item's price minus the parts you hold, a part
  of a part included; `Snapshot.item_list()` is inventory, backpack and stash with repeats),
  `missing_parts`, and `next_buy` (the dearest part the gold covers, never a lone recipe or anything under
  `PART_MIN_COST`; "finish" when the gold covers all that's left). `next_target` is the first item of the
  build plan you don't have. Used by `rules/economy.AffordableBuildItem`, `ItemPace`, the coach's
  `next_item`, the overlay's strip and the prepared calls' `afford <item>`.
- **Usual items without the stepping stones:** `typical_items` drops an item that's inside another item on
  the list when the bigger one is bought at least `FOLD_SHARE` as often (kept per hero until the data
  changes, since rules ask on every update). `build_plan` (the phase's most-bought items) puts the bigger
  item in a stepping stone's place too, since the tenth pass.
- **Buyback:** from `economy.BUYBACK_FROM` (30:00), `spendable()` is gold on top of buyback, for the unspent-gold
  and shopping reminders.
- **Role** (`knowledge/roles.py`, `roles.observe` on every update before the rules): the lane strip the hero
  spends 0:30-6:00 in (`mapinfo.band`), last hits a minute against a threshold that leans on the hero's role
  tags, and support purchases before the horn. `Role.support` (sure and not a core) turns off `FarmCheck`,
  the 5:00 lane pace line and the laning gauge, and adds the stack time to the strip. A support is only
  "sure" with a sign of supporting (support purchases, or a support hero under `SUPPORT_SURE_LH`); a carry
  hero is a core however the lane goes. Few last hits alone stay a guess, which silences nothing.
- **Owned but in no slot:** `Snapshot.item_list()` and `all_item_names()` include a consumed Aghanim's Shard
  or Scepter (the hero's flags). `next_target` prefers the plan item you've started paying for.
- **Place** (`knowledge/mapinfo.py`): `x + y` is how far along the base-to-base diagonal, `y - x` which side
  of the middle lane. Coarse on purpose (no landmark coordinates, which move between patches).
- **Burst:** `survival.LowHp` also fires when `BURST` of max HP went in two seconds and you're under
  `BURST_BELOW`, with no save item to press (`SaveItems` goes first), except on heroes who spend their own HP
  (`SELF_HURT`). Once per fight (`SAVE_GAP_S` after it or after a save call), never while stunned, hexed
  or spell-immune. Same key as the low-HP line, so one of the two per ten seconds.
- **The coach's state** gains `where`, `role`, `tp`, item cooldowns and charges, `coming_up` (the next
  spawns with clock times, from `timings.SPAWNS`) and `next_item`; `popular_next_items` and `typical_items`
  leave out what you own, parts of it and a second pair of boots. The match brief gains "Answers to their
  lineup" (`counters.counter_items`, the ones the hero's players buy first) and "Allies' big spells".
- **Checks on what the coach says:** `Coach._already_have` (`Knowledge.buy_mentions`: an item that follows a
  buy word within four words, with no "after / once / use" between) drops live tips and prepared calls that
  sell you what you have; a prepared call is checked again when it fires. `TriggerBook` won't say the same
  words twice within `SAME_WORDS_GAP_S` game seconds.

## Game mode: normal or Turbo (the tenth pass, `knowledge/mode.py`)

Dota's game data has no game-mode field, so `mode.observe(snap, store, setting, usually)` decides on every
update (before the rules) and keeps a `Mode` in match memory (`mode.of(memory)`); `Doty._on_payload` copies
it to `Knowledge.mode`. Most trusted first:

1. `RulesConfig.game_mode` when it's "normal" or "turbo" (Settings > Alerts > Game mode).
2. `mode.evidence`: between 2:30 and 10:00, experience per minute of `XPM_SURELY_TURBO` or more (no normal
   lane gives it), or from 5:00 `XPM_SURELY_NORMAL` or less (even a shared Turbo lane gives more); after
   10:00 the higher `LATE_` bars (for a Doty started mid-match). Kept for the match once seen. In between
   it says nothing: a normal game's solo lane and a Turbo game's support look alike, and then 3 decides.
   That gap is why the top bar shows the mode and the setting exists.
3. `Profile.usual_mode(account)`: Turbo if at least half of the account's recent matches are OpenDota
   `game_mode` 23. `Doty._learn_usual_mode` fetches it once per account on its own thread, only while the
   engine is running and the setting is "auto".

The demo (`Snapshot.is_demo`: match ids starting `doty-sim`) is always normal.

What follows the mode:

- `Mode.at(normal_s)` puts a normal-game time on this game's clock and `Mode.as_normal(clock)` the other
  way (`PACE` = 2: Turbo doubles creep gold and all experience. An estimate, to check against a recorded
  Turbo match). Through `Knowledge.mode`: `typical_time` and `spikes` (so the start card, `ItemPace`, the
  enemy-spike calls and the review), and `build_plan`'s phases. `item_odds` and `win_rate_by` return nothing
  in Turbo (normal-game win rates by timing don't carry over).
- `timings.spawns(turbo)`, `roshan_window(kill, turbo)`, `aegis_s(turbo)`: Turbo has no Tormentor entry
  (its time isn't in the source) and neutral tiers at half time.
- Rules: `economy.spendable(snap, mode)` and `survival.Buyback` keep buyback from `mode.at(30:00)`;
  `StashItems` and the secret-shop note are off in Turbo; `FarmCheck` is off in Turbo (no benchmark);
  `EnemySpikes` and `GamePlan` scale their windows; the laning gauge ends at `mode.at(10:00)`.
- The coach: `state["game_mode"]` (`mode.prompt_note`, Turbo only) lists the rule differences and overrides
  the system prompt's normal-game assumptions; `_phase`, `coming_up` and the Roshan line follow the mode.
- `Review.mode` records it (the recorder sees each update before the mode is set for it, so a match is
  closed under its own mode); a Turbo review makes no farm or item-pace focus point and tells the AI the
  mode. The Profile gives Turbo games no score (`Game.turbo`).

Sources for Turbo's rules: the Dota 2 wiki's Game modes page (read 2026-11-09), listed in the module's
docstring. Not from a source and so not used: the Tormentor's Turbo time, Turbo farm benchmarks.

## One answer to "what to buy" (the tenth pass)

`Knowledge.shopping(hero, clock, owned, gold, n)` is the only place that decides it: the item being built
now (`next_target`) first, then the rest of `build_plan`, then `typical_items`; one pair of boots at most;
nothing owned. `build_plan` is the current phase's `PLAN_PER_PHASE` most-bought items after those of the
phase before that are worth buying late (`CARRY_OVER_MIN_COST`, or boots), so a player with nothing built at
30:00 (15:00 in Turbo) is pointed at Manta Style before Swift Blink; it's kept per hero and phase until the
data changes. `next_target` doesn't count plain Boots of Speed as a start on any one pair of boots. The first entry carries `short`, `buy` (the item, or the part the gold covers), `buy_cost`
and `finish` from `next_buy`. Readers: `rules/economy.AffordableBuildItem`, `Doty._hud` (the strip),
`Doty._voice_ahead`, the coach's `next_item` (which now also lists `buy_now` and `after_it`), and
`Doty._game_view()["build"]`, which `ui/live.BuildPanel` draws as is. The panel no longer computes a build
of its own from `Stats`.

## Smaller things in the tenth pass

- **LIVE goes out when a match stops:** `Doty._match_running` needs an update in the last `LIVE_QUIET_S`
  (12 s, just past Dota's 10 s heartbeat, so a reconnect or a frozen client isn't taken for the end;
  `STALE_S`, 35 s, while paused). Stopping the demo calls `Doty.match_gone()`, which is instant. The status
  phase, the laning gauge and the overlay's strip follow it.
- **Demo games aren't yours:** `Review.demo`; `review.latest_review()` skips demo reviews unless asked
  (`demos=True`, the demo's own recap), so Learn's "From your last game" is a game you played.
- **One tag per kind of line:** `overlay.card_tag(source, category, priority)` names it for both the
  overlay's card and the app's feed.

## The overlay's strip and drawings (the ninth pass)

- **`HudStrip`** (top of the card column): fed once per game second from `Doty._each_second` ->
  `overlay.set_hud(Doty._hud(snap))`. `Doty._timers` is shared with the app's timers panel.
- **`MarkLayer`** is still one window over the whole game, but `_frame` calls `update(region)` with the marks'
  patches (`_mark_region`) and the pointer's old and new spots, so Qt clears, paints and hands Windows only
  those. The pointer starts at most `POINTER_FROM` from its target for the same reason.
- **Stale drawings:** `Doty._note_marks` remembers where the hero stood when a card with screen drawings went
  up; `_stale_marks` calls `overlay.drop_marks("screen")` once the hero has moved `MARKS_MOVED`, a fight
  starts or you die.
- **Covered:** every 500 ms `win.dota_covered()` (another process's window in front, over half of Dota's
  rectangle; the desktop and taskbar don't count). While true the column's opacity is 0, the badge hides and
  nothing is drawn.
- **Screen reads:** `capture.StripGrabber` + `TopBarReader.read_band` grab and read only the reader's strip;
  the whole-window grab (with PrintWindow) is the fallback. `capture._rgb` and `_shrink` cut the coach's
  screenshot from ~110 ms to ~70 ms at 3440x1440.
- **Voiced ahead:** `Doty._voice_ahead` (every 10 game seconds) hands `Voice.prepare_now` the lines with
  names in them that the rules may say soon.

## Coach design

- **Kinds:**
  - `start` (lane plan + BUILD) at the horn
  - `death` (why + what to buy) on death
  - `now` (one decision) on the ask hotkey
  - `watch` (live coaching) every `speculative_interval_s` game seconds while alive: the reply starts
    `SAY:` (spoken + overlay card) or `QUIET:` (app feed only), and either way it is cached so the ask
    hotkey answers instantly. What it said recently goes back in as `coach_said_recently`, so it doesn't
    repeat itself.
  - `draft` during hero selection (GSI `game_state`): reads the picks on screen and suggests counters;
    same `SAY:`/`QUIET:` contract as `watch`.
  - Screen-only `watch` when no GSI data has arrived and Dota is the foreground window: same prompt, a
    state that says "no game-data feed".
  - `speak_up` decides what a look says aloud: `important` (`SAY:` only), `every`, `never`.
  - `plan` (5-minute plan + BUILD) on its hotkey
- **Prompt:**
  - Frozen system prompt (cacheable), containing the brevity rules, a fair-play rule and the
    current patch timings.
  - The system prompt also carries a coaching playbook: map reading, counter-items by enemy type,
    timings, laning, objectives and roles.
  - The user turn holds a compact state JSON (~1.5 KB), the last 6 Doty alerts, and screenshots from
    `capture.grab_views`: a 1280 px JPEG of the Dota window with Doty's cards masked out, plus
    full-resolution crops of the minimap and the top bar.
  - The lineup: until it's known, the state asks the model to add a hidden `LINEUP:` line (read from the
    top bar). Doty parses it (`Knowledge.find_hero` handles names and nicknames), stores it per match, and
    from then on sends `allies`, `enemies` and `your_matchups` (OpenDota `/heroes/{id}/matchups`).
- **Request settings:**
  - Model `claude-sonnet-5-5` at effort `low` (half Opus 5.5's price). `claude-opus-5-5` and
    `claude-haiku-5-5` are options in Settings / `doty.toml`.
  - Server-side refusal fallback (`fallbacks: "default"`) on models that support it.
  - Keep-alive HTTP pool plus a free `models.retrieve` ping every 45 s.
  - Live looks (`watch` on `claude-haiku-5-5`) send `thinking: {"type": "disabled"}`: Haiku 5.5 thinks by
    default, and a one-sentence tip plus a few prepared calls don't need it (allowed up to effort `high`).
  - When the opening look starts, `Coach._prewarm_watch` sends a `max_tokens: 0` request with the live
    looks' model, thinking and effort, so the system prompt and match brief are already cached for the
    first live look (`ClaudeBackend.prewarm`; best effort).
- **Cost:**
  - Each call is ~3–4K input tokens (state + image) and ~100–300 output tokens. On Opus 5.5 that's
    ~$0.02 a call, so roughly $1.50 for a 40-minute game at the default 30-second look interval
    ($0.50–1.00 at 60 s).
  - Haiku 5.5 is a few cents a game.
  - Set `speculative_interval_s = 0` to make calls only when you ask.

## AI providers (`coach/backends.py`)

| Provider | API | Notes |
|---|---|---|
| Claude (default) | Anthropic Messages (SDK), prompt caching, effort, server-side fallbacks | API key. Max/Ultra plans include API credits |
| ChatGPT | OpenAI Responses API, `stream=true`, `store=false` | "Continue with ChatGPT" (`chatgpt_auth.py`): OpenAI's sign-in for open-source apps. Loopback `127.0.0.1:1455`, PKCE S256, `chatgpt.tokens.use.direct`, RS256 ID token checked in plain Python, rotating refresh |
| OpenAI / Gemini / OpenRouter | OpenAI-compatible Chat Completions (images as data URLs) | API keys in the Windows user environment. Gemini and OpenRouter have free tiers |
| Ollama | the same, at `127.0.0.1:11434/v1` | Free and local; needs a vision model |

- **One stream:** every backend yields text chunks, so streaming cards, sentence-by-sentence speech,
  `SAY:`/`QUIET:`, and the `LINEUP:`/`BUILD:`/`DRAW:`/`DRAFT:` lines work the same everywhere.
- **No "sign in with Claude":** Anthropic's terms don't allow third-party apps to use Claude
  subscriptions.

## Coach drawing (`DRAW:` lines)

- **Pixel sizes:** the request says each image's pixel size ("game screen 1280x536 px", "minimap
  440x440 px").
- **Up to three marks:** the coach may add up to three lines such as `DRAW: circle minimap 312 140
  "3 missing" danger`.
- **Normalized:** `marks.parse` turns them into fractions of that image, and drops anything off-image
  or for an image that wasn't sent.
- **Mapped back:** the overlay maps them onto the live game window.
- **Fair play:** the system prompt forbids marking anything not visible in the screenshots.

## Draft looks

- **Reading the screen:** during hero selection the coach writes a hidden `DRAFT: you=… | radiant=… |
  dire=… | bans=…` line.
- **Stats in the next look:** the next look's state carries `counter_picks_from_stats` from
  `draft.suggest`, so the AI's picks are grounded in the same numbers as the Draft page.
- **Auto-fill:** the Draft page fills itself from the same line.
- **Never the players:** Valve hides lobby identities until picks end (2023 patch), and Overwolf's Dota
  rules require the same. Doty never looks up the players in your game.

## Fair-play guardrails (in code, not just docs)

- **Inputs:** GSI (your own player, as Valve designed it) plus screenshots of your own screen.
- **No:** memory reads, DLL injection, synthetic input, or console/log scraping.
- **Roshan:** the window comes from the public `roshan_killed` event. `map.roshan_state` is never read.
- **System prompt:** forbids claiming enemy positions, items or cooldowns that aren't visible.
- **Overlay:** visible to screen capture by default (`exclude_from_capture = false`).

## Roadmap

1. **Validate against a real match.** Run `doty run --record recordings` for one bot game and one
   real game, check field names (`neutral1`, `attributes_level`, events), and measure real GSI cadence.
2. ~~**Push-to-talk questions**~~ **Done.**
   - **Hold detection:** `holdkey.py` polls `GetAsyncKeyState` at 100 Hz, so it works with mouse side
     buttons; it uses no hooks and injects nothing.
   - **Recording and transcription:** `listen.py` records 16 kHz mono and transcribes locally with
     faster-whisper (small.en on CUDA, base.en on CPU), primed with Dota vocabulary and the current hero.
   - **Screenshot:** captured at key-press.
   - **Coach:** answers with the last 4 Q&As as context.
   - **Cloud STT not used:** Deepgram/AssemblyAI streaming would cost money and send audio off the machine
     for a gain the GPU already gives.
3. ~~**Neural TTS**~~ **Done (ElevenLabs, and Kokoro for free).**
   - **Module:** `eleven.py` streams `pcm_24000` from `/v1/text-to-speech/{voice}/stream` with the
     `eleven_flash_v2_5` model over one kept-alive HTTPS connection, into a callback-driven
     `sounddevice` buffer (`latency="low"`, ~100 ms device buffering).
   - **Pipelining:** the next line is fetched while the current one plays.
   - **Cache:** short repeated lines are cached as PCM in `%LOCALAPPDATA%\Doty\cache\voice`.
   - **Interrupts:** CRITICAL advice and push-to-talk clear the buffer and drop the queue.
   - **Fallback:** SAPI stays loaded. A fatal error (bad key, quota, missing voice) switches to it for
     the session. A transient error speaks that line with SAPI, and three in a row pause ElevenLabs
     for 2 minutes.
   - **Kokoro (done): the free local engine** behind the same `Voice` (`kokoro.py`).
     - **Model:** Kokoro-82M v1.0, the onnx-community fp32 export, at a pinned revision. It's
       downloaded on request (330 MB, SHA-256-checked) into `%LOCALAPPDATA%\Doty\models\kokoro`.
     - **Front end:** eSpeak NG through ctypes (no phonemizer package), then misaki's eSpeak-to-Kokoro
       phoneme mapping, plus fixes for game times and numbers ("4:00", "4,250").
     - **Runtime:** ONNX Runtime on the CPU, a quarter of the logical cores (2–6), no spinning.
     - **Measured:** Whisper small.en transcribes its output word for word (apart from homophones like
       buy/by and TP/teepee), with the Warm filter on as well as off.
     - **Clean-up (`kokoro.clean`, tones Warm and Clear):** the vocoder leaves steady whistles at 4.8 and
       9.6 kHz (24 kHz ÷ its 5-sample hop) 13-22 dB above the sound around them, and its 5-10 kHz band
       sits 20+ dB brighter than ANSI S3.5's speech spectrum. Zero-phase FFT notches (±30 Hz) and a tilt
       above 3.5 kHz, then a de-esser on 20 ms frames (4.5-11 kHz over the 300 Hz-4 kHz body, 2.5:1
       above 0 dB). ~7 ms per second of speech. `FILTER_VERSION` is in the phrase-cache key.
     - **GPU (source checkouts):** `kokoro_gpu.py`, set up by `doty gpu-voice`.
       - **Separate folder:** `onnxruntime-gpu` 1.26 (the last PyPI build for CUDA 12) and cuFFT/cudart
         go in `.venv\kokoro-gpu`. They can't share a process with faster-whisper's CPU `onnxruntime`
         (same package).
       - **Helper process:** Kokoro runs in a child process over pipes (token ids and style in, audio
         out).
       - **STFT rewrite:** the model's STFT has no CUDA kernel (~140 ms a line on the CPU), so the
         helper rewrites it in memory as a strided Conv, exact to ~1e-5.
       - **Measured:** ~150 ms to first audio on an RTX 4070 Ti, versus 0.6–1.1 s on the CPU.
       - **Fallback:** if the helper fails, the CPU takes over.
     - **DirectML** (any GPU) fails on the model's ConvTranspose, so the installer stays CPU-only.
4. ~~**Draft helper**~~ **Done:** the Draft page plus stat-backed draft looks. It uses public hero
   aggregates only; it never scouts the specific players in the lobby (that's what got Overplus banned).
5. **Post-game review.** Feed the session log (`%LOCALAPPDATA%\Doty\logs`) plus the OpenDota match
   (by `matchid`) to Claude for a written review.
6. **Minimap vision** (optional). Use Dota2-Vis YOLO weights to read *visible* enemy positions off the
   minimap for the coach. Policy risk is less clear than GSI's, so keep it opt-in.
7. ~~**Packaging**~~ **Done:** a PyInstaller folder build plus an Inno Setup per-user installer. See
   `docs/RELEASING.md`.
   - **Installed mode:** `config.FROZEN` keeps settings in `%APPDATA%\Doty`, and shortcuts point at
     `Doty.exe`, which the installer owns.
   - **Self-test:** `Doty.exe --selftest` runs on every build.
   - **Next:** code signing, auto-update, and a Clicky-style edge proxy so users don't need their own
     API key.
