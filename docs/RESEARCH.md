# Research: Cluely, Clicky, and what a live Dota coach needs

Compiled 2026-11-04 from public sources only: product pages, published teardowns, and the
source code of open-source clones on GitHub. No installers were downloaded or run. Anything
labelled **inference** is our reading, not a published fact.

## TL;DR

- **Cluely and Clicky are both thin clients around the same loop:** grab context (screen and/or audio),
  send it to an LLM, stream an answer into a transparent always-on-top window or into your ear.
  Neither does anything a Python app can't do.
- **Neither one is actually fast.** Cluely advertises "300 ms", but every request still goes to a remote
  LLM. Clicky waits for the full reply before it starts speaking. Real sub-300 ms comes from
  **not calling an LLM on the hot path**.
- **Dota has something neither app has: Valve's Game State Integration (GSI).** The game itself
  POSTs JSON describing your hero, items, gold, and the game clock to a local HTTP server about 10 times
  a second. That makes a deterministic rule engine possible: sub-millisecond logic, with timers that
  fire *before* the event.
- **So Doty is two tiers:** (1) GSI → rules → overlay/voice in milliseconds; (2) Claude for the
  "what should I do now" judgment, pre-computed in the background so the hotkey answers instantly.

---

## 1. Cluely (cluely.com)

**What it is.** An AI meeting and interview copilot. It listens to the call and reads your screen,
then shows answers in an overlay. It doesn't join the call as a bot. Pricing: Free, Pro $19.99/mo, and
"Pro + Undetectability" $149.99/mo, so hiding from screen share is a paid upsell.

**How it's built** (desktopinsights.com dependency scan; Hacktron's July 2025 teardown):
- **Shell:** Electron 38 with React, Tailwind and Framer Motion. The macOS bundle is about 1.4 GB.
- **Native pieces:** Rust (tokio/hyper/rustls), the macOS system-audio tap `audiotee`,
  `webrtc-audio-processing` (echo cancellation), a voice-activity detector, and ONNX Runtime.
- **Speech-to-text:** Deepgram for real-time transcription, plus AssemblyAI and AWS Transcribe as alternates.
  Audio is **24 kHz mono 16-bit PCM**.
- **LLM SDKs:** OpenAI, Groq, OpenRouter, and the Vercel AI SDK. Groq suggests some requests go to a
  fast-inference provider.
- **Backend:** Clerk auth, Stripe, Pusher, PostHog, Datadog. **Inference:** LLM calls go through
  Cluely's servers.
- **Invisibility on Windows:** **inference** that it's Electron `setContentProtection(true)`, which calls
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` (Windows 10 2004+).
- **Security incident:** an unsandboxed renderer plus an IPC bridge with no allowlist let a malicious
  link in an AI answer reach the screenshot and recorder IPC. Hacktron reports it was "silently patched".
- **Prompt design (leaked, mid-2025):**
  - XML-tagged sections, one per input type (code, math, multiple choice, email, UI navigation), with a
    fixed fallback for an unclear screen.
  - The meeting prompt is a **numbered priority ladder**: answer the last question; define a term just
    heard; suggest follow-ups; handle objections; solve what's on screen; otherwise acknowledge passively.
  - Strict word limits and no meta-talk.

## 2. Clicky / HeyClicky (heyclicky.com)

**What it is.** By Farza (Humansongs, YC). Hold a hotkey and talk; it screenshots your screen,
answers out loud, and flies a blue cursor to the thing it's talking about. It runs on macOS and
Windows. It says it only looks at the screen on hotkey and never stores screenshots.

**How it's built** (the open-source original, `farzaa/clicky`, MIT licence):
- **Shell:** native Swift/AppKit. A full-screen transparent overlay that doesn't take focus and lets
  clicks through.
- **Hotkey:** hold Ctrl+Option, read through a listen-only `CGEvent` tap.
- **Audio:** `AVAudioEngine` streams PCM16 to **AssemblyAI streaming STT**. The client gets a
  short-lived (480 s) token from its proxy, so the real key never ships in the app.
- **Screenshots:** taken only after the hotkey is released. They exclude Clicky's own windows, are
  scaled to a **1280 px long side, JPEG q0.8**, and are labelled for the model ("cursor is on this screen").
- **LLM:** Claude (Sonnet by default) over SSE. Keeps the last 10 exchanges and pre-warms TLS at launch.
- **Output protocol:** the reply ends with a machine-readable tag `[POINT:x,y:label:screenN]`. The tag
  drives the cursor, and the text goes to TTS.
- **TTS:** ElevenLabs Flash. It waits for the **whole** reply before speaking.
- **Backend:** a Cloudflare Worker that holds every key, passes Anthropic's SSE stream straight through,
  proxies TTS, and mints STT tokens.

## 3. Open-source clones (useful as code references)

| Project | Overlay / invisibility | Capture | LLM |
|---|---|---|---|
| cheating-daddy (sohzm) | Electron, `transparent`, `setContentProtection(true)`, `setIgnoreMouseEvents` | `getDisplayMedia` → canvas → JPEG ≤1280 px; Windows loopback audio | Gemini Live with sliding-window context |
| free-cluely (Prat011) | Electron, `showInactive()` | Hides its own window, waits 100 ms, captures, shows again | Gemini Flash, **no streaming** |
| Glass (pickle-com) | Electron, content protection toggleable at runtime | `desktopCapturer` JPEG q70 | Streaming; aborts the previous request on a new ask; 2 parallel STT sessions (mic and system) with echo cancellation |

## 4. What we took, and what we didn't

**Took:**
- **One transparent, click-through, always-on-top window that never takes focus.** Implemented in PySide6
  with `WS_EX_NOACTIVATE`.
- **Screenshots only on demand, downscaled to 1280 px, Doty's own window masked out.** From Clicky.
  Doty captures the *Dota window*, not "monitor 1", because multi-monitor setups broke the naive version
  in testing.
- **Stream, and start speaking at the first finished sentence.** Neither app does this.
- **One request in flight; a new trigger cancels a stale background one.** From Glass.
- **Keep the HTTPS connection warm.** From Clicky.
- **A machine-readable trailing tag.** Clicky's `[POINT:...]` became Doty's `BUILD: item_x, item_y`
  line: Claude picks the build, and the deterministic engine then tells you the moment you can afford
  the next item.
- **A priority-ordered, word-limited prompt.** From Cluely.

**Didn't take:**
- **Stealth as a feature.** Cluely charges for invisibility; for a game tool that reads as cheating.
  Doty's overlay is visible to OBS and Discord by default. `exclude_from_capture` is an opt-in.
- **Keys in the client.** Cluely's prompt leaked, and a Glass mirror shipped a hardcoded key. Doty
  reads `ANTHROPIC_API_KEY` from the environment. A hosted version would need a Clicky-style proxy.
- **Continuous screen upload or voice-chat transcription.**
- **Electron.** A 1.4 GB bundle competing with Dota for CPU and GPU is the wrong trade.

---

## 5. Dota 2 Game State Integration (GSI)

- **Config file:** `…\steamapps\common\dota 2 beta\game\dota\cfg\gamestate_integration\gamestate_integration_<name>.cfg`
  in Valve KeyValues format. `doty install` writes it. Dota reads the file only at launch.
- **Launch option `-gamestateintegration` is required** (since the 2022-03-11 update, for performance
  reasons). Add it in Steam → Dota 2 → Properties → Launch Options.
- **Transport:** an HTTP POST of JSON to `uri`. The `auth.token` from the cfg comes back in every
  payload.
- **Timing keys:**
  - `buffer`: how long changes are batched.
  - `throttle`: minimum gap between sends.
  - `heartbeat`: send even if nothing changed.
  - We use 0.05 / 0.1 / 10 s, which gives about 10 updates per second.
- **What arrives while you play:** your own player only. That covers `map` (clock, day/night, state,
  paused), `player` (gold, GPM/XPM, KDA, LH/DN), `hero` (HP, mana, level, position, status flags,
  buyback, talents), `abilities`, `items` (slot0–8, stash0–5, teleport0, neutral0) and `events`
  (`roshan_killed`, `aegis_picked_up`, `courier_killed`, `bounty_rune_pickup`, `tip`).
  - `roshan`, `couriers`, all-player data and draft data are **spectator-only**.
  - Payloads in ticketed and league games are reported to be delayed.
- **Clock fields:** events carry `game_time`, which is not the clock. Doty converts it with
  `event.game_time − (map.game_time − map.clock_time)`.
- **Field names are community-documented** (antonpup/Dota2GSI, osztenkurden/dota2gsi); Valve publishes
  no schema. Run `doty run --record DIR` during a real match to capture ground truth.

## 6. Valve's rules, and why Doty is safe

**What Valve has banned:**
- **Reading client memory.** In Feb 2023 a honeypot patch led to 40,000+ permanent bans.
- **Client-state introspection** through console commands and `record`; Valve locked these down in 2023.
- **Pre-draft scouting of other players.** Overplus users got lifetime bans in 2024.
- **Input automation** and **code injected into the game process.**

**What Valve tolerates or does itself:**
- **GSI-based timers and coaching.** Valve's own Dota Plus "Plus Assistant" gives rune, pull and stack
  timers plus item and ability suggestions. Overwolf apps such as Dota Coach and commercial AI coaches
  such as LaneMind and KeenPlay run on GSI.

**Doty's rules for itself:**
- **Inputs:** GSI plus an on-demand screenshot of what's already on your screen. Nothing else.
- **Never:** reads memory, injects code, sends input, or scrapes console logs.
- **No hidden information.** The prompt forbids claiming hidden information. Doty deliberately
  ignores `map.roshan_state`, even if it ever appears in player mode, and works the Roshan window out
  from the public kill event.

**Overlay display mode.** Exclusive fullscreen bypasses the compositor, so overlays can't draw on top.
Players must use **Borderless Window**. This machine is already set that way: `setting.fullscreen 0`,
`nowindowborder 1`.

## 7. Patch 7.41f timings (encoded in `doty/knowledge/timings.py`)

| Objective | Timing |
|---|---|
| Bounty runes | 0:00 (river), then every **4:00** at the jungle spots; uncollected ones stack |
| Water runes | 2:00, 4:00 |
| Power rune | 6:00, then every 2:00 |
| Shrines of Wisdom (replaced wisdom runes in 7.38) | 7:00, then every 7:00 |
| Lotus pools | 1 per 3:00, max 6 |
| Tormentors | first **20:00**, respawn 10:00. Day: bottom-right (Radiant) corner. Night: top-left |
| Roshan | respawn 8–11 min. From 15:00: **top pit by day, bottom pit by night** (swapped in 7.41) |
| Aegis | 5:00 |
| Neutral items (Madstone) | tiers at 0:00, 15:00, 25:00, 35:00, 60:00 |
| Neutral camps | spawn every minute from 1:00; stack-pull around x:53–x:55 |
| Day / night | 5:00 each, day first. Roshan and Tormentors follow the *natural* cycle, not Night Stalker's |
| Buyback | cooldown 8:00; cost ⌊200 + net worth / 13⌋ (GSI gives `buyback_cost` directly) |

## 8. Data sources

- **odota/dotaconstants** (`raw.githubusercontent.com/odota/dotaconstants/master/build/`):
  - `heroes.json` is keyed by id, and its `name` field equals GSI `hero.name`.
  - `items.json` is keyed by short name: GSI's `item_blink` is `blink` there.
- **OpenDota API** (no key needed; 60 calls/min, 3,000/day):
  - `/heroes/{id}/itemPopularity` gives build plans.
  - `/benchmarks?hero_id=` gives percentile tables of *full-match* averages.
  - `/heroes/{id}/matchups`.
  - Doty caches responses for 24 h.
- **STRATZ GraphQL** needs a bearer token. Not used yet.

## 9. Prior art

- **Dota Plus** ($3.99/mo): Valve's in-game assistant.
- **Dota Coach** (Overwolf): GSI timers plus voice.
- **LaneMind** and **KeenPlay**: GSI + LLM + overlay; LaneMind supports bring-your-own-key.
- **Open-source GSI + LLM coaches:** BrightGir/dota-ai-coach (Go) and MajorTomLeee/dota-coach (GSI + Claude).
  All of them keep the LLM off the timing path, which matches Doty's split.
- **Minimap computer vision:** Dota2-Vis (2026), a YOLO11 dataset of player icons on TI 2025 minimaps.
  A possible later add-on, but its policy risk is less clear than GSI's.

## Sources

**Cluely:**
- cluely.com
- cluely.com/pricing
- desktopinsights.com/apps/cluely
- hacktron.ai/blog/hacking-cluely
- aiengineerguide.com/til/cluely-system-prompt-leak

**Clicky:**
- heyclicky.com
- github.com/farzaa/clicky (README, AGENTS.md, worker/src/index.ts, CompanionManager.swift, CompanionScreenCaptureUtility.swift)
- isaac-flath.kit.com/posts/point-and-talk-how-clicky-s-ai-interface-works

**Clones:**
- github.com/sohzm/cheating-daddy
- github.com/Prat011/free-cluely
- Glass mirror harshith-vaddiparthy/glass

**Platform:**
- electronjs.org/docs/latest/api/browser-window
- learn.microsoft.com SetWindowDisplayAffinity

**Dota:**
- dota2.com/datafeed/patchnotes (7.37–7.41f)
- liquipedia.net/dota2/March_11,_2022_Patch
- liquipedia.net/dota2/{Runes, Roshan, Tormentor, Lotus_Pool, Shrine_of_Wisdom, Madstone, Neutral_Creeps, Time_of_Day, Dota_Plus}
- github.com/antonpup/Dota2GSI
- github.com/osztenkurden/dota2gsi
- github.com/xzion/dota2-gsi

**Policy:**
- dota2.com/newsentry/3677788723152833273
- pcgamesn.com/dota-2/update-console-commands
- strafe.com (Overplus bans)

**Data:**
- api.opendota.com
- github.com/odota/dotaconstants

**Prior art:**
- dotacoach.gg
- lanemind.com
- keenplay.app
- github.com/BrightGir/dota-ai-coach
- github.com/RicardoRCarvalho/dota2-vis
