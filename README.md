<p align="center">
  <img src="docs/brand/banner.png" alt="Doty: a coach for Dota 2. Reads your game. Watches the map. Talks you through it." width="820">
</p>

<p align="center">
  <a href="https://github.com/bkeigh/Doty/releases/latest"><img alt="Windows" src="https://img.shields.io/badge/Windows-10%20%26%2011-3fd4ff?style=for-the-badge&labelColor=06080b&logo=windows&logoColor=3fd4ff"></a>
  <a href="#linux"><img alt="Linux" src="https://img.shields.io/badge/Linux-from%20source-3fd4ff?style=for-the-badge&labelColor=06080b&logo=linux&logoColor=3fd4ff"></a>
  <img alt="Free" src="https://img.shields.io/badge/Free-yes-ffc94d?style=for-the-badge&labelColor=06080b">
  <img alt="Open source" src="https://img.shields.io/badge/Open%20source-yes-4fd16a?style=for-the-badge&labelColor=06080b">
</p>

<h3 align="center">Reads your game. Watches the map. Talks you through it.</h3>

<p align="center">
  <a href="https://dotyweb.vercel.app"><b>Website</b></a> ·
  <a href="https://github.com/bkeigh/Doty/releases/latest"><b>Download</b></a> ·
  <a href="https://github.com/bkeigh/Doty/issues"><b>Report a bug</b></a>
</p>

---

**Doty is a free Dota 2 coach that sits next to your game and calls it.**
It's the Dota Plus assistant plus a human coach, without either.

It won't save you from a 0/7 Pudge. It *will* tell you he's coming.

## What it says

Every card is colour-coded, so you know what it is before you read it.

| | Tag | Means | Sounds like |
|---|---|---|---|
| 🔴 | **DANGER** | Act now | *"Back off now. Low HP, their side."* |
| 🟠 | **TIMER** | Coming up | *"Bounty and power runes in 15."* |
| 🔵 | **DOTY** | The coach | *"Lion is off the map. Wait for your team."* |
| 🟢 | **LEVEL** | Good | *"Skill point: take Greater Bash."* |
| 🟡 | **GOLD** | Money | *"You can afford Black King Bar."* |

## What it does

- **Instant calls.** Rune timers, objective windows, save items. Plain rules, under a millisecond, no AI needed.
- **An AI coach that points.** It circles the hero on your minimap and draws an arrow back to your tower.
- **Ask out loud.** Hold Mouse 5, say *"can I fight now?"*, let go.
- **Draft help.** Counter picks and bans from millions of public games at your rank.
- **Builds and counters** for every hero, by game phase.
- **Post-game review.** What killed you, and what to fix next game.

## Get it

| | |
|---|---|
| **Windows 10 & 11** | **[Download DotySetup from Releases](https://github.com/bkeigh/Doty/releases/latest)** and run it. No admin prompt. |
| <a id="linux"></a>**Linux** | Runs from source today (native Linux Dota 2, X11). A packaged build is on the way. |

> **Windows says "protected your PC"?** The installer isn't code-signed yet. Click **More info**, then **Run anyway**. Each release ships a SHA-256 checksum if you want to check it.

## Set up in a minute

1. Install Doty and open it. The **Setup** page walks you through the rest.
2. **Connect Dota.** One click, and Doty drops its game-data file into your Dota folder.
3. In Steam, add `-gamestateintegration` to Dota 2's **launch options**.
4. In Dota, set the display mode to **Borderless Window**. Exclusive fullscreen hides overlays.
5. *(Optional)* Pick an AI for the coach.

## Fair play

Doty only uses what you could see yourself.

- Never reads the game's memory.
- Never presses a key for you.
- Never looks up who's in your lobby. All its stats are about heroes, never players.

It runs on Valve's official **Game State Integration**.

## Bring your own AI

Timers, alerts, draft and heroes work with no AI at all. For the talking coach, pick one:

**Claude** (default) · **ChatGPT plan** · **Gemini** or **OpenRouter** free tiers · a **local model** with Ollama

On Claude, a 40-minute game costs roughly **5 to 12 cents**.

## Credits

Doty is an independent community project, not affiliated with or endorsed by Valve Corporation. Dota 2 is a trademark of Valve Corporation. Hero and item art shown in the app is Valve's, loaded from their public servers and never bundled.

Type set in [Barlow](https://github.com/jpt/barlow) by Jeremy Tribby, under the SIL Open Font License.

<p align="center"><sub>Built for people who've been told "just ward more."</sub></p>
