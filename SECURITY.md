# Security

What Doty does on your PC, what leaves it, how to check a download, and how to report a problem. Everything
here can be checked against the source in this repository.

## What Doty does, and what it never does

**It does:**

- receive Dota 2's official Game State Integration data, which the game itself sends to Doty on your PC;
- take screenshots of your own screen while a match is on, for the AI coach to look at;
- listen to your microphone **only while you hold the push-to-talk key**, and turn that into text on your PC.

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
| The AI you chose (Anthropic, OpenAI, Google, OpenRouter; or nobody, with a local Ollama model) | A screenshot of the game, a summary of the game state, and your question | When the coach looks, and when you ask |
| ElevenLabs | The words Doty is about to say | Only if you set an ElevenLabs key |
| OpenDota | Requests for public hero statistics (about heroes, never players); your own match history if you put your account on the Profile page | Start-up, and the Heroes and Profile pages |
| Valve's image servers | Requests for hero and item pictures | When a page shows them |
| GitHub | Downloads of the public hero and item tables; a request for the latest release | Start-up; and when you ask Doty to check for updates |
| Hugging Face | Downloads of the voice and speech models | When you click Download, and the first time you use push-to-talk |

Your voice is transcribed on your PC. Nothing else is sent anywhere.

## What stays on your PC

- Settings: `%APPDATA%\Doty\doty.toml`.
- Logs of what Doty said, and post-game reviews: `%LOCALAPPDATA%\Doty`.
- A ChatGPT sign-in, if you use one: `%LOCALAPPDATA%\Doty\auth\chatgpt.json`.
- "Wrong tip?" reports and capture mode (off unless you turn it on): `Documents\Doty`. If OneDrive backs up
  your Documents folder, it will sync these too.

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
