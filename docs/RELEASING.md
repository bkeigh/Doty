# Releasing Doty

How to turn this folder into `DotySetup-<version>.exe`, a normal Windows installer anyone can download.

## One-time setup

```bash
.venv\Scripts\pip install -e .[build]
```

Also install [Inno Setup 6](https://jrsoftware.org/isinfo.php), which builds the installer. It's free, and
`winget install JRSoftware.InnoSetup` does it.

## Build

```bash
.venv\Scripts\python packaging\build.py
```

About 3 minutes. It:

1. **Bundles Doty with PyInstaller** into `dist\Doty\` (`Doty.exe` plus its libraries, ~345 MB).
2. **Self-tests the bundle.** It runs `Doty.exe --selftest` with no window, and the build stops if
   anything is missing. It checks:
   - settings location, bundled art and the Qt image plugins;
   - that the window builds and the engine starts and stops;
   - a verified TLS handshake;
   - audio output, the Windows voice, shortcut creation, speech recognition and screen capture.
3. **Builds the installer** with Inno Setup: `dist\DotySetup-<version>.exe` (~98 MB), plus a `.sha256` file
   to publish alongside it.

`--no-installer` stops after step 2; `--reuse` skips step 1.

The pieces live in `packaging/`:

| File | Purpose |
|---|---|
| `doty_app.py` | The `Doty.exe` entry point: opens the app. It also handles `--selftest` and `--uninstall-cleanup`. |
| `doty.spec` | The PyInstaller recipe. |
| `doty.iss` | The Inno Setup script. |
| `build.py` | Runs everything. It also cuts the installer's side art from the splash image. |

## What people get

- **Install:** no admin prompt (per-user, into `%LOCALAPPDATA%\Programs\Doty`). There's a Start-menu
  shortcut and an optional desktop one, and both pin to the taskbar. There's a "Launch Doty" box at the end.
- **First run:** the Setup page walks them through installing Dota's game-data file, the launch option,
  and their Claude API key. Optionally it also downloads the free Kokoro voice (330 MB, one click) or
  takes an ElevenLabs key.
- **The Kokoro model isn't in the installer.** Doty downloads it from Hugging Face (pinned revision,
  SHA-256-checked) when someone clicks Download. The installer only carries eSpeak NG (~2 MB with just
  English) and ONNX Runtime, which Whisper already needed.
- **Settings:** stored in `%APPDATA%\Doty\doty.toml`, so they survive upgrades.
- **Upgrade:** run a newer `DotySetup`. It asks you to close Doty first if it's open.
- **Uninstall:** Windows Settings > Apps. This also removes Doty's file from the Dota folder. Some
  files stay behind:
  - settings and logs;
  - the voice cache;
  - the Kokoro model (`%LOCALAPPDATA%\Doty\models`);
  - the downloaded speech-recognition model (`%USERPROFILE%\.cache\huggingface`).
- **Speech recognition runs on the CPU** (about 0.8 s per question; a ~145 MB model downloads on first
  use). NVIDIA's CUDA libraries are 2 GB, so they're not bundled. A PC with the CUDA 12 runtime installed
  still uses its GPU.

## New version

1. Bump the version in `doty/__init__.py` (`__version__`) and `pyproject.toml`.
2. Build.
3. Test on a PC that has never had Doty (a friend's, or a virtual machine). Windows 10 Home has no
   Windows Sandbox.

Never change `AppId` in `doty.iss`; it's how Windows recognises an upgrade of the same app.

## Publish

**From now on, release through GitHub:** bump the version, commit, then

```bash
git tag v0.1.2
```

```bash
git push origin main v0.1.2
```

The "Release build" workflow builds the installer from that commit and leaves a draft release with the
installer and its `.sha256`; look it over on the Releases page and publish it. You can also run the
workflow by hand from the Actions tab to test a build without releasing anything.

By hand from your own PC (how 0.1.0 and 0.1.1 were made; such a file can't be signed by SignPath):

```bash
gh release create v0.1.0 dist\DotySetup-0.1.0.exe dist\DotySetup-0.1.0.exe.sha256 --title "Doty 0.1.0" --notes "First release"
```

Link to the release page from a small landing page (for example on Vercel).

**Updates:** Doty's updater (Settings > App > Updates, `doty/update.py`) reads the latest release of
`app.update_repo` ("owner/name"). It needs both assets above: the installer and its `.sha256` (it refuses a
download that doesn't match). Tag releases `v<version>` with a higher version than the last. Set the
default `update_repo` in `doty/config.py` is `bkeigh/Doty` (from 0.1.1; 0.1.0 shipped without it, so 0.1.0 users install 0.1.1 by hand once).

## Code signing

Signing puts a verified publisher name on `Doty.exe` and the installer in place of "Unknown publisher",
lets Doty's updater refuse anything that publisher didn't sign, and makes antivirus false alarms rarer.
It does **not** switch the SmartScreen warning off on day one: Microsoft's own documentation says the
prompt goes once a signed file has built up download history. Checked 2026-11-10; prices and rules change,
so check each provider's page before paying.

**Ways to get a signing identity** (each verifies a person or a project, so the maintainer applies):

| | Cost | The name people see | Notes |
|---|---|---|---|
| **Microsoft Artifact Signing** (was Trusted Signing) | A monthly Azure fee (Basic tier, 5,000 signatures a month) | Your legal name | Individuals must be in the United States or Canada. You verify with government ID in the Azure portal. Needs a paid Azure subscription whose billing account type is Individual, with your name and address exactly as on the ID. The key never leaves Microsoft. |
| **SignPath Foundation** | Free for open-source projects | "SignPath Foundation" | They vouch that the file was built from this repository, so the installer has to be built by an automated build (GitHub Actions) and each release approved by hand. Needs an application, a "Code signing policy" section on the download page, and two-factor sign-in on GitHub. |
| **Certum Open Source certificate** | About 49 euros a year (cloud version) | "Open Source Developer, your name" | A normal certificate kept in Certum's SimplySign cloud app, which shows up in the Windows certificate store. |

**The route chosen (2026-11-10): SignPath Foundation.** It is free, and it fits how Doty is released from
now on: GitHub builds the installer, SignPath signs what GitHub built, a person approves each signing.

What is already in the repository for it:

- `.github/workflows/release.yml` builds the installer on GitHub's Windows machine when a `v<version>` tag
  is pushed (or by hand from the Actions tab), with the library versions pinned in
  `packaging/constraints.txt`. It records a build attestation and leaves a **draft** release for you to
  publish. Until SignPath is set up it builds unsigned and says so.
- The "Code signing policy" section in the README, which SignPath requires on the project's home page.
- An open-source license on everything (GPLv3 or later), and `SECURITY.md` as the privacy policy.

What only the maintainer can do, in order:

1. **Turn on two-factor sign-in for your GitHub account** (Settings > Password and authentication).
   SignPath requires it of everyone with access.
2. **Make one release with the GitHub workflow** (see "Publish" above), so there is a public release built
   the way SignPath will sign it. They only take projects that are already released and maintained.
3. **Apply** at https://signpath.org/apply. Have ready: the repository link, the license, the download page,
   and a sentence on what Doty does. They may ask questions; there is no guaranteed timeline, and they are
   free to say no.
4. **When accepted**, in SignPath:
   - link the predefined trusted build system "GitHub.com" to the Doty project, and install the SignPath
     GitHub App on the repository when they ask for it;
   - create two artifact configurations with these slugs. The workflow hands SignPath one zip holding one
     file each time:

     `app`:

     ```xml
     <?xml version="1.0" encoding="utf-8"?>
     <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
       <zip-file>
         <pe-file path="Doty.exe">
           <authenticode-sign/>
         </pe-file>
       </zip-file>
     </artifact-configuration>
     ```

     `installer`: the same, with `path="DotySetup-*.exe"`.

     (SignPath's onboarding will tell you if they want product name and version restrictions added; the
     files already carry both.)
   - create a CI user and copy its API token.
5. **In the GitHub repository** (Settings > Secrets and variables > Actions):
   - secret `SIGNPATH_API_TOKEN`: that token;
   - variables `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_SIGNING_POLICY_SLUG`.

   Setting `SIGNPATH_ORGANIZATION_ID` is what switches signing on in the workflow.
6. **Change the README's "Code signing policy" status line** to the sentence SignPath requires: "Free code
   signing provided by SignPath.io, certificate by SignPath Foundation." Put the same heading, or a link to
   it, on the website's download page.

A signed release then goes: push the tag; the workflow builds and asks SignPath to sign `Doty.exe`; you
approve that in SignPath; it builds the installer around the signed file and asks again; you approve; it
checks both signatures match, writes the checksum and drafts the release. Not signed by this route: the
uninstaller inside the installer (Inno Setup can only sign it with a certificate on the build machine).

**The other ways, for the record.** With a certificate of your own (Microsoft Artifact Signing, or a Certum
certificate in the Windows store) the build signs locally instead. Install the Windows SDK's "Signing Tools
for Desktop Apps" for `signtool.exe`, then set one environment variable before running `build.py`; it holds
whatever goes between `signtool sign` and the file name:

```
set DOTY_SIGN_ARGS=/fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 /dlib "C:\signing\Azure.CodeSigning.Dlib.dll" /dmdf "C:\signing\metadata.json"
```

`build.py` then signs `Doty.exe` before the self-test, has Inno Setup sign the uninstaller and the installer,
and stops unless Windows reports one valid publisher on both. Nothing about a certificate is stored in the
repository.

**What changes for updates.** A signed Doty only installs an update signed by the same publisher name
(`doty/update.py`). The unsigned 0.1.x builds can't check this, so they will take the first signed release
normally. After that, **changing the publisher name strands the updater**: people would have to download
the next version by hand. Pick the identity you mean to keep.

**Until it's signed**, the honest things to offer are the ones already on the site and the README: the
checksum, the open source, and `SECURITY.md`.

## Things to know before going public

- **"Windows protected your PC."** Unsigned installers trigger SmartScreen. People can click **More info >
  Run anyway**, but many won't. See "Code signing" above: the build is ready for it, and what's missing is
  a signing identity, which only the maintainer can get.
- **API keys.** Each person currently uses their own Claude key, plus an ElevenLabs key if they want that
  voice. No key is in the installer or the repository, and on Windows a saved key lives in Credential
  Manager (`doty/keystore.py`, `SECURITY.md`). That's fine for friends. For the public, run a small server that holds your key and charges
  users (Clicky works this way).
- **License: GPLv3 or later (decided 2026-11-10).** Doty's source is published under the GNU General
  Public License, version 3 or later: `LICENSE` at the repository root, named in `pyproject.toml` and
  the README. The reason is the free voice:
  - **eSpeak NG is GPLv3 or later, and the installer includes its DLL**, which Doty loads into its own
    process. Publishing Doty under the same license is the simple, clean answer.
  - **What each release has to carry:** the license text (the installer puts it in the install folder
    as `LICENSE.txt`; `doty.iss` does this) and a pointer to the source: this repository for Doty, and
    https://github.com/espeak-ng/espeak-ng for eSpeak NG. The README's License section has both.
  - **Kokoro's model (Apache 2.0):** fine to use and to point people at. Credit hexgrad. The phoneme
    mapping in `doty/kokoro.py` comes from misaki (also Apache 2.0).
  - **Tag each release on the commit it was built from**, so the source for a given installer can be
    found. (0.1.0 and 0.1.1 were tagged before the source was in the repository.)
  - This is a summary, not legal advice. Ask a lawyer before selling Doty.
- **Branding.** Call it "a coach for Dota 2". Keep Valve's logos and art out of Doty's branding (icon,
  splash, installer, store pages).
  - **Doty's art:** the mark (`doty/ui/assets/doty.svg`, the app icon) is original vector art;
    the wordmark, splash, panels and installer images are drawn in code.
  - **Hero portraits and item icons inside the app:** these are Valve's images, loaded at runtime from
    Valve's public CDN (the same ones OpenDota, Dotabuff and Overwolf apps show) to identify heroes and
    items. They're never bundled in the installer or the repo. It's still Valve's art, so if Doty is
    ever sold, have that use checked first.
  - **Fonts:** Barlow and Barlow Condensed by Jeremy Tribby (SIL Open Font License,
    `assets/fonts/Barlow-OFL.txt`), bundled.
  - **Old artwork:** the earlier Higgsfield images are kept in `docs/brand/legacy/` and aren't shipped.
- **Continue with ChatGPT.** OpenAI's sign-in for open-source apps asks apps to follow its UI
  guidelines (label: "Continue with ChatGPT").
  - **Button style:** check the current guidelines at developers.openai.com/siwc before publishing.
  - **Paid or hosted versions:** these need OpenAI's interest form.
- **AI provider terms.** Doty never offers a Claude subscription sign-in: Anthropic's terms forbid it
  for third-party apps. Claude users bring an API key (Max/Ultra plans include API credits).
- **Players' privacy and Valve's rules.** Doty only uses hero-level public statistics, never
  lookups of the people in a lobby. Keep it that way.
- **Testing on your own PC.** A test install shares the "Doty" Start-menu shortcut name with your source
  checkout. A silent test install (`DotySetup.exe /VERYSILENT /DIR=<folder> /MERGETASKS=!desktopicon`)
  replaces it until you uninstall. Opening the source Doty again repairs it. Uninstalling also removes
  the game-data file from Dota; Setup > Install puts it back.
