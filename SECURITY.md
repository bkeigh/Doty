# Security

What Doty does on your PC, what leaves it, how to check a download, and how to report a problem. Everything
here can be checked against the source in this repository.

## What Doty does, and what it never does

**It does:**

- receive Dota 2's official Game State Integration data, which the game itself sends to Doty on your PC;
- take screenshots of the Dota window while you play, for the AI coach to look at and for Doty's own reader of
  the top bar's hero portraits, which runs on your PC;
- listen to your microphone **only while you hold the push-to-talk key**, and turn that into text on your PC;
- keep a record of each match on your PC (see below; you can turn it off).

**It never:**

- reads the game's memory, injects anything into the game, or presses keys or moves the mouse for you;
- installs a keyboard hook (hotkeys use Windows' own `RegisterHotKey`, the way Discord and OBS do);
- looks up the players in your lobby;
- collects statistics about you. There is no Doty server and no telemetry.

## Your AI key

Doty has no key of its own and the installer contains none. The AI coach uses **your** key, for the provider
**you** pick, and the key is sent only to that provider.

- **Windows:** saved in Windows Credential Manager (Control Panel > Credential Manager > Windows
  Credentials, entries named `Doty/...`), which Windows encrypts for your account. You can view or remove
  them there.
- **Linux:** the desktop keyring, or a file only your user can read if no keyring is running.
- It is never written to the settings file, the logs, or a "Wrong tip?" report.

> Doty 0.1.0 and 0.1.1 saved the key as a Windows *user environment variable* (for example
> `ANTHROPIC_API_KEY`), where every program you start gets a copy. Newer versions don't do that, but they
> also don't delete a variable they can't be sure is theirs. If you don't need it for anything else, remove
> it: Start > "Edit environment variables for your account" > select it > Delete. Then enter the key again
> in Doty's Settings.

## What leaves your PC

| Goes to | What | When |
|---|---|---|
| The AI you chose (Anthropic, OpenAI, Google, OpenRouter; or nobody, with a local Ollama model) | A screenshot of the game, a summary of the game state (with public statistics about your hero), your question, and your instructions to the coach if you wrote any | When the coach looks, and when you ask. Saving your instructions in Settings also sends them once, to turn them into settings |
| ElevenLabs | The words Doty is about to say | Only if you set an ElevenLabs key |
| OpenDota | Requests for public hero statistics (about heroes, never players) | Start-up, the Heroes pages, and the background study (below) |
| OpenDota | Your own account id, to read your public profile and recent matches. The id comes from your own game data, or you type it in | The Profile page; and, while Game mode is Automatic (Settings > Alerts), once for your account when Doty first gets its game data, to tell whether you usually play Turbo. Set Game mode to Normal or Turbo and this lookup isn't made |
| Valve's Dota 2 website (`www.dota2.com`) | Requests for the list of patches and their patch notes | Start-up, and the background study |
| Valve's image servers | Requests for hero and item pictures | When a page shows them |
| GitHub | Downloads of the public hero, item and ability tables; a request for the latest release | Start-up and the background study; and when you ask Doty to check for updates |
| Hugging Face | Downloads of the voice and speech models | When you click Download; the first time you use push-to-talk; and, once Kokoro is downloaded, its Spanish, Portuguese or French voice when that is Doty's language |

**The background study.** While Doty runs it builds a card for each hero (the builds, skill orders, item
timings, pace by rank and matchups players have on this patch) from public statistics, so the coach knows a hero
before you play it. It asks OpenDota for hero-level numbers only: per hero, its benchmarks, its item timings,
and two queries over public matches (pro matches of this patch, and the last few days of public ranked and Turbo
matches), plus the shared tables above. Each request names a hero, never a player: nothing about you is sent.
The heroes of your current match and your most-played heroes (read from the recent matches Doty already saved
for the Profile page or the game-mode lookup above, not fetched for this) are studied first, so the order of the requests follows them. It
makes one request at a time, a few every 20 to 90 seconds, at most 1,200 a day, and stops for half an hour when
OpenDota keeps failing. Turn it off in Settings > Advanced > "Study heroes in the background".

Your voice is transcribed on your PC. Nothing else is sent anywhere.

## What stays on your PC

- Settings: `%APPDATA%\Doty\doty.toml`. Your instructions to the coach are kept there too.
- Logs of what Doty said, and post-game reviews: `%LOCALAPPDATA%\Doty`.
- Downloaded tables, statistics and the hero cards the study builds: `%LOCALAPPDATA%\Doty\cache`.
- A ChatGPT sign-in, if you use one: `%LOCALAPPDATA%\Doty\auth\chatgpt.json`.
- A record of each match (on unless you turn it off): `%LOCALAPPDATA%\Doty\records`. See below.
- "Wrong tip?" reports and capture mode (off unless you turn it on): `Documents\Doty`. If OneDrive backs up
  your Documents folder, it will sync these too.

On Linux the same files are in `~/.config/doty` (settings) and `~/.local/share/doty` (everything else).

### The match record

So that Doty's coaching can be checked against what really happened, it keeps a light record of each match you
play, on by default. Each match gets its own folder, named by the time it started, with:

- **The game data** Dota sent (`gsi.jsonl.gz`): at least one update a second, plus every update where your
  items, life, level or the game's events changed. Before anything is written Doty removes the game's auth
  token; Steam IDs, account IDs, profile and avatar links, party and lobby ids wherever they appear; in the
  player section (yours, and all ten when Dota sends them), the names and any other text except the team and
  the activity; and the match id, which anyone could look up to see who played.
- **Every line Doty showed** (`advice.jsonl`): the text, the game clock, which part of Doty said it, its
  priority, and whether it was said out loud. What you say over push-to-talk is not kept.
- **What the match was** (`match.json`): your hero and team, the game mode, the ten heroes once Doty knows
  them, and when the match began and ended.

No screenshots, and nothing is sent anywhere. Demo games and custom (arcade) games aren't recorded. The folder
is `%LOCALAPPDATA%\Doty\records`, not Documents, so OneDrive doesn't sync it; a match takes about 1 MB once
packed. Doty keeps the last 30 matches and at most 200 MB (`[record]` `keep_matches` and `keep_mb` in the
settings file), deleting the oldest first.

To turn it off: Settings > Advanced > "Keep a record of each match". No new match is recorded after that; the
records already kept stay until you delete them. "Delete records" next to the switch removes them all, the match being
recorded included, and they can't be brought back. You can also delete the folder yourself while Doty is closed.

If you share a record with someone, know that the start time and the ten heroes can be enough to find the match
on a public site such as OpenDota, and with it the players' public profiles.

## The local listener

Dota sends its data to `127.0.0.1:53000`, which only programs on your own PC can reach. Web pages run on
your PC too, in your browser, so Doty also refuses any request a browser made and any request addressed to
a name other than this PC. A website can't feed Doty a fake match.

## Updates

Doty only checks for an update when you ask. The installer it downloads must match the SHA-256 checksum
published with the release. Once Doty is code-signed, the update must also be signed by the same
publisher, or it is deleted without being run.

## Checking a download yourself

1. **Checksum.** Each release has a `.sha256` file next to the installer. In a terminal:

   ```
   certutil -hashfile DotySetup-<version>.exe SHA256
   ```

   The result must match the contents of that file.
2. **Signature.** Right-click the installer > Properties > Digital Signatures. **Releases up to 0.1.1 are
   not code-signed**, which is why Windows shows "Windows protected your PC" (choose More info, then Run
   anyway). That warning means "Windows doesn't know this publisher yet", not "this file is harmful".
3. **Source.** The code is all here, under the GPL. `docs/RELEASING.md` describes how the installer is
   built from it.
4. **Where it was built.** Releases after 0.1.1 are built by GitHub Actions from the tagged commit, and
   GitHub records that. With the GitHub CLI:

   ```
   gh attestation verify DotySetup-<version>.exe --repo bkeigh/Doty
   ```

   (0.1.0 and 0.1.1 were built on the maintainer's PC, so this doesn't apply to them.)

Some antivirus products flag unsigned programs built with PyInstaller (the tool that packages Doty) on
sight. If yours does, please open an issue saying which product and which Doty version.

## Reporting a vulnerability

Please don't open a public issue for a security problem. Use **Security > Report a vulnerability** on the
GitHub repository, which reaches the maintainer privately. If that option isn't shown, open an issue that
says only "security contact wanted" and you'll be given a private way to send the details.

Fixes go into the next release; only the latest release is supported.
