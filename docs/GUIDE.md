# Doty: a quick guide

Doty is a free, open-source coach for Dota 2. It runs next to the game on Windows, watches your match
through Valve's official game data, and tells you what to do: rune and objective timers, "back off, low
HP", which skill to level and what to buy next, plus an AI coach that looks at your screen and answers
questions you ask out loud.

It never reads the game's memory, never presses keys for you, and never looks up the players in your
lobby. It only uses what you could see yourself.

## What you need

- Windows 10 or 11 and Dota 2 (Steam). Linux works from source too: see [Linux](#linux).
- Dota in **Borderless Window** mode (Settings → Video → Display Mode). Exclusive fullscreen hides
  anything drawn over the game.
- For the AI coach (optional): a Claude API key, your ChatGPT Plus or Pro plan, an OpenAI API key, a
  free Gemini or OpenRouter key, or a free local model with Ollama. Timers, alerts, Draft and Heroes work
  without any of them.

## Install

1. Download `DotySetup-<version>.exe` from the project's Releases page and run it. It installs for your
   Windows user only (no admin needed) and adds Doty to the Start menu and desktop.
2. Open Doty. The **Setup** page walks you through the rest, one step at a time:
   - **Connect Dota:** Doty writes its game-data file into your Dota folder. Then in Steam, right-click
     Dota 2 → Properties → General → Launch Options, and add `-gamestateintegration`.
   - **Pick your AI** (Settings → AI coach): paste a key, or click **Continue with ChatGPT**.
   - **A natural voice (optional):** click **Download** next to "Free natural voice" (Kokoro, 330 MB,
     once). Otherwise Doty uses the Windows voice.

Running from source instead: see the README's "Setup" section.

## During a game

**Cards** appear at the side of the screen, color-coded by what they're about:

| Tag | Color | Means |
|---|---|---|
| DOTY | cyan | the AI coach |
| DANGER | red | act now (low HP, a gank coming) |
| TIMER / RUNES | amber | something is about to happen |
| GOLD | gold | items and money ("you can afford Black King Bar") |
| LEVEL | green | skill points and level-ups ("Skill point: take Greater Bash") |

**The status badge** sits above the cards. An eye means Doty is watching your game; turning arrows mean
it's waiting for Dota's game data. Hover it to see what's going on. **Drag it to move the cards**
anywhere you like; double-click it to put them back.

**The live strip** sits at the top of the cards all game. Its first row counts down to the next runes,
shrines and objectives (it turns amber in the last twenty seconds; a support also gets the next stack
time). Its second row is the item you're building: its name, the gold still to pay and about when you'll
have it at your gold per minute, over a bar of how far along you are. Parts you've already bought count: an
Ogre Axe and a Mithril Hammer leave 1,450 of Black King Bar, not 4,050. Turn it off in Settings > Overlay.

**Drawings:** sometimes the coach circles a tower on your minimap, points an arrow from your hero back to
it, or boxes an item slot. The coach only says *what* to point at; Doty knows *where* it is (the map's towers,
runes and Roshan's pits, your hero from the game data, the fixed parts of the HUD), so a drawing doesn't land
on a guess. A drawing on your hero moves with you. Allies are green, enemies red. They're thin, see-through
lines that fade after a few seconds, and a drawing on the game world comes down as soon as you've walked on, a
fight starts or you die. Make them thicker or more solid in Settings > Overlay (**Drawing thickness**,
**Drawing opacity**). These positions are new and haven't been checked against a real game yet: if a
drawing on the minimap or an item slot lands off, please report it with **Wrong tip?**.

**Alt-tabbed?** While another app's window is in front of Dota, Doty's cards and drawings hide. The voice
keeps going.

**At the horn** a card shows your plan from data: the skill order pro players use on this patch and your
core items with when they usually land (the finished items: Battle Fury and Manta Style, not the
Perseverance and Yasha on the way to them). **For the first ten minutes** (five in Turbo) a slim bar under
the strip tracks your last hits a minute against a target for your hero, unless you're playing support.

**What to buy, part by part:** gold in your pocket does nothing and you lose some when you die. So the
moment it covers a real part of your next item, Doty names the part ("Buy Ogre Axe": a part of Black King
Bar), and when it covers everything that's left, "Finish Black King Bar". A part that's only sold at the
secret shop says so. From 30:00 it leaves your buyback gold alone: only gold on top of buyback counts.
Every part of Doty gives the same answer: the spoken call, the strip, the coach, and the app's **Buy next**
panel, which lists the item you're building now and what comes after it. Only the first one can say Buy
now, because your gold goes to one thing at a time.

**Turbo:** Dota doesn't tell Doty which mode a match is, so Doty works it out. It goes by your recent
matches (the same public data as your Profile page), and a few minutes in by the match itself: experience
comes in twice as fast in Turbo. When Doty is coaching a Turbo game, the app's top bar says **TURBO** next
to LIVE. In Turbo:

- Item timings, the enemy's power spikes and the phases of your build are on a Turbo clock (about half a
  normal game's). That's an estimate from Turbo's doubled gold and experience, not measured yet.
- Buyback gold is kept from 15:00, Roshan's respawn window is 4:00 to 5:30 after he dies, and the Aegis
  warning is for 4 minutes.
- "Courier your stash" and the secret-shop note are dropped: Turbo's couriers deliver by themselves and
  every shop sells everything.
- The Tormentor isn't announced (its Turbo time isn't in Doty's source), and the 10:00 and 20:00 farm
  checks stay quiet (their benchmarks are from normal games). The 5:00 lane check still runs.

If Doty gets it wrong, set it yourself: Settings > Alerts > Game mode (Automatic, Normal or Turbo). The
demo is always a normal game.

**Your role:** Doty works out which role you're playing (carry, mid, offlaner, soft or hard support) from
the lane you spend the first minutes in and your last hits, with the hero and your starting items as a
first guess. The coach is told, so a support hears about wards, stacks and staying with a core instead of
last hits, and the farm reminders stay quiet for supports. Few last hits alone never make you a support
(a core who's losing the lane has few too): it takes support purchases, or a support hero who's hardly
touching the creeps. Until then it stays a guess, and the farm reminders keep coming.

**Save calls:** the moment you lose a quarter of your HP in two seconds (or you're low and still losing
it), Doty names the save item you have ready: "Glimmer yourself", "BKB now", "Force Staff out", "Eul's
yourself", "Wand now" (with enough charges). Silenced in a fight with Manta, Lotus or Eul's ready: "Manta
the silence". Doty works these out itself in milliseconds, with no AI key needed, once per fight, and says
them a little faster than other lines so they stand out. Stunned or hexed, there's nothing you can press, so
it stays quiet. With nothing to press, a burst like that gets "Get out now" while you still have the HP to
walk away, instead of "Back off, low HP" once it's nearly gone.

**Instant calls:** at calm moments the AI coach thinks ahead and prepares calls like "if your HP drops
under 35% on their side of the map: back off" or "if two of them are dead: push mid now". Doty checks them
ten times a second and says them the moment they come true, so they land on time even mid-fight, cutting in
over anything else being said (an answer you asked for picks up again after the call). Tips that arrive late
are dropped instead, and so is anything the voice couldn't start in time.

**Quiet in fights:** while you're taking damage, Doty only talks about the fight. Timers and shopping
reminders still show on screen, but aren't said out loud. Runes that spawn together are one line ("Bounty
and power runes in 15"), and nothing about shopping or skills is said while the game is paused.

**When you die** the coach says what really killed you, from your own last seconds: how fast your HP went,
how long you were stunned, and any item you had ready and never pressed. A few seconds before you respawn
it looks again, so the tip for what to do next is ready the moment you're back. (An Aegis or Wraith King
revive isn't counted as a death.)

**Who's in the game:** Doty reads the ten portraits on the top bar itself, a few seconds after the game
loads, so the coach knows your allies and enemies (and what each enemy does) from the first tip. Once it
knows all ten it also sees who's dead: "Two enemies dead. Push a tower."

**The game plan:** once Doty knows all ten heroes, it checks how often each hero wins short games versus
long ones (public match data). If their lineup gets stronger the longer the game goes, it tells you to win
early; if yours does, to play safe and scale. When neither side clearly gains, it says nothing.

**Your item timings:** the coach knows how often players of your hero win when the next core item is
finished by each time (public match data: Battle Fury on Juggernaut wins about 65% of games when it's done
by 12:00 and 33% by 20:00). When yours is going to be late, it can tell you to change the build; the
post-game review puts a number on a late item too.

**Their power spikes:** early on, a card lists when the enemy heroes usually finish their big items
(Black King Bar, Blink Dagger, Battle Fury...), from public match data for those heroes. Shortly before
the first few, Doty gives you a heads-up, so you can fight before the item lands or play safe after.

**No repeats:** a live tip that says again what you heard in the last two minutes goes to the app's feed
instead of your ears, and an instant call isn't said twice within a few minutes. A tip that tells you to
buy something you already have (or a second pair of boots) is dropped.

**Ask out loud:** hold **Mouse 5** (the forward side button), ask "what should I buy?" or "can I fight
now?", and let go. Your voice is turned into text on your PC; only the text goes to the AI.

**Hotkeys:**

| Keys | Does |
|---|---|
| Ctrl+Alt+A | "What should I do right now?" |
| Ctrl+Alt+P | A 5-minute plan and item build |
| Ctrl+Alt+M | Mute / unmute the voice |
| Ctrl+Alt+H | Hide / show the cards |
| Ctrl+Alt+X | "Not that one": drop the item Doty is pointing at from this game's build |

## The app

Five tabs:

- **Coach:** what Doty sees right now: your hero, timers, the build, everything it said, and a live view
  of the game. **Ask now**, **5-min plan**, and one **Coach** menu for how chatty it is plus Mute, Hide
  cards and Pause. Hover a coach line in the feed and click **Wrong tip?** to save what it saw, for a bug
  report.
- **Heroes**, with a **Draft | All heroes** switch at the top:
  - **Draft:** during hero selection, the best picks against the enemy lineup, from millions of public
    games. Picks fill in from your screen, or build a draft by hand: drag heroes from the **hero pool**
    (filter by name, attribute, role, melee or ranged; sort by how well each does against their picks)
    onto a slot, drag between slots to swap, or drag a slot back to the pool to clear it. Double-click a
    hero to add it to your team. Suggested picks and bans can be dragged in too.
  - **All heroes:** every hero as a portrait tile with its win rate at your rank. Start typing anywhere to
    search (Enter opens the top match); filter with the attribute, melee/ranged and role chips; sort by
    attribute, win rate, pick rate or name. A hero's page has its stats and win rate by rank, the build as a
    timeline of items, its abilities, what counters it and what it's strong against (click a hero to jump to
    it), and what to buy against it. ← and → step through heroes, Esc goes back to the grid.
- **Learn:** getting better at Dota, at your level: your plan (the three things that win the most games at
  your rank, plus what your last game's review found), last-hit targets and a Demo Hero drill, and tips on
  how strong players see the game, laning and last hits, when to leave lane, farming, items against heroes,
  drafting and counter-picks, fights, dying less, climbing ranked and quick wins. Pick your level at the top:
  **New** (never played), **Learning** (Herald to Guardian), **Climbing** (Crusader to Legend),
  **Experienced** (Ancient to Divine) or **Top** (Immortal). Until you pick, it follows the rank on your
  Profile. Each topic has its own color and picture; pick one from the menu, search every tip (try "BKB",
  "Roshan" or a hero's name), or turn on **Every level** to see them all. Tips show the items and heroes
  they're about. **Read to me** reads a topic or your plan out loud in Doty's voice.
- **Profile:** your rank medal, record, win rate, average KDA, score, GPM and XPM; your form over the last
  20 games as a chart (bar height is the game's performance score, green for a win); the heroes you play most
  with your record on each; and every recent game (click one to open it on OpenDota). From OpenDota, if your
  match data is public.
- **Settings:** the AI coach, the voice, push-to-talk, the overlay, alerts, the app and Advanced, each with
  its own color and icon (what's new is below, under "Make it coach your way"). **Ctrl+F** finds a setting; a changed setting is marked until you save (**Ctrl+S**), and
  the bar at the bottom lists what changed. "How often it looks" shows what a game costs. The **Setup
  checklist** button (top right) walks through connecting Dota, the AI and the voice; the Settings tab
  shows a dot when something there needs you.

The same status icon is in the app's top bar, on the overlay and in the tray: an eye while Doty watches
(green), turning arrows while it waits (amber), red when something stopped, grey when paused.

**Your level changes how Doty coaches you in a match:**

| Level | What's different |
|---|---|
| New | The basics, said once each in your first minutes: stand behind your creeps, deny, watch the minimap, use the courier, walk back to heal. Level 6, laning and farm checks are said out loud. The coach uses plain words and points at things. |
| Learning | Level 6, laning and farm checks out loud; the coach favors safety, last hits, a TP scroll and your save item. |
| Climbing | The default. |
| Experienced | Fewer shopping and skill reminders out loud (they still show); short calls. |
| Top | Only what you might miss: timings, map, tempo, cooldowns. |

**Make it coach your way** (all in Settings):

- **When I go my own way** (Alerts > What Doty suggests): what Doty does when you build something other than
  what it suggests. **Say it once, with a reason** (the default): it may say so one time per item in a game,
  at a moment you can buy, and only with a reason from this game ("it blocks Lion's Earth Spike and Hex"). With
  no such reason it says nothing, and then it follows your build. **Follow me silently**: it never argues.
  **Keep reminding me**: the old way, a few reminders per item.
- **Your instructions** (AI coach > Your instructions): tell the coach how you play, in your own words, like
  custom instructions in a chat app. For example:
  - "I play position 4. Keep it short, I know the basics."
  - "On Spirit Breaker I always build Boots of Bearing."
  - "Never suggest Radiance."

  When you save, Doty turns what it can into settings and shows them to you, to untick if it got one wrong:
  items never to suggest, your build on a hero, subjects to leave alone, how much it talks. Those are kept by
  the instant calls too, even with the AI off. The rest (like "keep it short") the AI reads as your style on
  every look. Something with a condition ("no BKB talk unless they have three stuns") stays style. Saying
  "never suggest Radiance again" over push-to-talk adds it to the same list.
- **Drawing thickness and opacity** (Overlay): how thick and how solid the coach's circles and arrows are.
  The drawings on screen change as you move the sliders.
- **Study heroes in the background** (Advanced): while Doty runs, it builds a card for each hero from public
  statistics for this patch (what players of each role build and by when, skill order, last hits at your rank,
  matchups), so the coach knows a hero before you play it. The heroes of your match go first. It asks for
  numbers about heroes only, never about you, a few requests at a time. The row shows how many heroes are done.
- **Keep a record of each match** (Advanced): on by default. Doty keeps each match's game data and every line
  it said, on your PC only, so its coaching can be checked against what really happened. No screenshots, and
  your Steam ID, name and the match id are taken out first. It keeps the last 30 matches (about 1 MB each).
  **Delete records**, next to the switch, removes them all (click twice: they can't be brought back).

**Language:** the first time Doty opens it asks for your language: English, Русский, 简体中文, Español, Português
(Brasil) and Bahasa Indonesia up front, each by its flag, with ten more under **More languages** (Français, Deutsch,
Polski, Українська, Türkçe, 한국어, 日本語, ไทย, Tiếng Việt, Filipino). The language your PC is set to is already
selected. Change it any time with the **flag at the right of the top bar**, or in Settings › App › Language; Doty
restarts to switch. It sets the screens, the alerts said out loud, the Learn tab's lessons, the AI coach, and what
push-to-talk listens for (a multilingual speech model, downloaded once). A few lines with numbers built in may still
show in English.

**Which voice speaks your language** (Settings › Voice shows this as a row of flags per voice):

| Voice | Speaks |
|---|---|
| Kokoro (free, on this PC) | English, Spanish, Brazilian Portuguese, French. The Spanish, Portuguese and French voices are a 1 to 2 MB download on top of Kokoro, fetched when that's your language; there are fewer of them and they're less polished than the English ones. |
| Kitten (free, on this PC) | English only. |
| ElevenLabs (paid) | Every Doty language except Thai (its fast model doesn't have Thai). |
| Windows voice (robotic) | The languages whose speech is installed in Windows: add one in Windows Settings › Time & language › Speech, and Doty picks it. |

If no voice on your PC speaks your language, Doty stays quiet rather than have an English voice mangle it; the advice
still shows on screen, and Settings › Voice says what to add.

**What the AI costs:** in a match the Coach status shows how many looks the AI took and about what they cost
(Claude's prices). A 40-minute game is about 8¢ at the usual rhythm: live looks use the fast model, and the
parts that repeat (instructions, the match brief) are read from Anthropic's cache at a tenth of the price. A
look is skipped when nothing has changed since one that had nothing to say.

## After a game

The Coach page shows a recap of the match: what to work on (up to three points), each death with what
likely led to it (low HP in a fight, no TP scroll, deep on their side, right after a warning), and
**Ask Doty about this game** for the coach's take out loud.

## When something's off

- **The badge shows amber arrows:** Dota isn't sending game data. Check the launch option
  (`-gamestateintegration`), then restart Dota. The Setup page tells you which step is missing.
- **No cards over the game:** set Dota to Borderless Window. Ctrl+Alt+H shows hidden cards again.
- **Tournament or ticketed games:** Dota delays its game data there (to stop cheating), so Doty's timers
  and calls would come late. It's meant for regular matchmaking, bot and practice games.
- **Arcade (custom) games:** Doty stays quiet: Dota's own timings and rules don't apply there.
- **"AI coach off":** add a key or sign in under Settings → AI coach. Claude Pro and Max plans can't be
  used by other apps (Anthropic's rule), so Claude needs an API key; Max and Ultra plans include API
  credits for one.
- **The voice is too sharp or rings:** Settings → Voice → Tone → **Warm** (it removes the whistle the free
  voice makes and tames "s" sounds), try the **Heart** or **Nicole** voice, or switch the voice service to
  **Kitten** (free, softer and darker; an 82 MB download).
- **Help Doty read your screen better:** Settings → Advanced → **Capture matches**, for a match or two.
  Doty saves frames of the game (only while Dota is in front) and its data, without your Steam ID or
  name, to `Documents\Doty\captures`. Doty sends none of it anywhere, but if OneDrive backs up your
  Documents folder, it syncs them too. About 150 MB a match.
- **A tip came late or seemed wrong:** Doty already drops tips that arrive more than a few seconds late,
  come during a fight, name heroes who aren't in your game, or suggest items your hero doesn't buy. If one
  still gets through, note the game time next to it in the Coach page's feed when you report it.

## Linux

Doty also runs on Linux, from source. This is new and hasn't had a full test on a Linux desktop yet: tell us
what works and what doesn't.

1. Install Dota 2 from Steam (the native Linux version) and set **Borderless Window**.
2. Get Python 3.11 or newer, then in the Doty folder: `python3 -m venv .venv`, `.venv/bin/pip install -e .`
   and `.venv/bin/python -m doty app`. The first start adds Doty to your applications menu
   (`doty shortcut` adds a desktop icon too).
3. Optional extras from your distribution's packages: `espeak-ng` (a backup voice when Kokoro or ElevenLabs
   can't speak) and `libportaudio2` (for the push-to-talk mic, if it isn't there already).
4. Connect Dota on the Setup page, and add `-gamestateintegration` to Dota's launch options in Steam, as on
   Windows.

What's different:
- **X11 works best.** On a Wayland desktop Doty runs through XWayland. If the coach's screenshots come back
  black or the overlay doesn't stay over the game, sign in with an X11 session ("GNOME on Xorg", "Plasma
  (X11)"). Without X11 or XWayland, Doty still gives timers, alerts and AI advice from the game data, without
  screenshots, overlay or hotkeys. The Setup page shows which you have.
- **Hotkeys** (Ctrl+Alt+A and the rest) still reach the game too, so keep them on chords Dota doesn't use.
- **API keys** go into your desktop's keyring (GNOME Keyring, KWallet). Without one, they're saved in
  `~/.config/doty/keys.env`, which only your user can read.
- **Folders:** settings in `~/.config/doty`, logs and downloads in `~/.local/share/doty`.
- No installer or automatic updates yet: update with `git pull`.

## Privacy

What Doty learns about your game stays on your PC, except what goes to the AI you chose: a screenshot of the
game, a summary of your game state and your instructions, when the coach looks or you ask. Your voice is
transcribed on your PC. Doty also asks OpenDota for public statistics about heroes (the hero study among them:
hero numbers only, nothing about you), and for your own public profile and recent matches: on the Profile page,
and, with Game mode on Automatic, once for your account to tell whether you usually play Turbo.

On your PC: logs in `%LOCALAPPDATA%\Doty\logs`, and the match records in `%LOCALAPPDATA%\Doty\records` (on
Linux, under `~/.local/share/doty`). Neither is sent anywhere, and neither is in Documents, so OneDrive doesn't
sync them. Your AI key is kept in Windows Credential Manager (on Linux, the desktop keyring) and goes only to
that AI. [SECURITY.md](../SECURITY.md) has the full list of what leaves your PC and when, and exactly what a
match record keeps.

## More

- [README](../README.md): every feature and setting in detail.
- [ARCHITECTURE](ARCHITECTURE.md): how it's built.
- [RELEASING](RELEASING.md): building the installer.
