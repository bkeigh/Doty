"""Measure where Dota draws the top-bar portraits at your resolution, from frames saved in capture mode.

    .venv\\Scripts\\python.exe tools\\calibrate_hud.py "%USERPROFILE%\\Documents\\Doty\\captures\\<match>"
    .venv\\Scripts\\python.exe tools\\calibrate_hud.py frame-*.jpg --draw checks

Prints the measured layout as a TopBarLayout(...) line to paste into DEFAULT_LAYOUT in doty/vision.py.
With --draw it also saves each frame's top bar with the found slots outlined, to check by eye before
pasting. A capture folder is read for its topbar-*.png crops (else its frame-* images). It only reads
the files you name: nothing is sent anywhere.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from doty.vision import BAR_CONTRAST, calibrate_from_frames  # noqa: E402


def expand(args: list[str]) -> list[Path]:
    """Files, wildcards (cmd.exe doesn't expand them) and capture folders -> image paths."""
    out: list[Path] = []
    for arg in args:
        path = Path(arg)
        if path.is_dir():
            out += sorted(path.glob("topbar-*.png")) or sorted([*path.glob("frame-*.jpg"), *path.glob("frame-*.png")])
        elif any(ch in arg for ch in "*?["):
            out += sorted(Path(m) for m in glob.glob(arg))
        else:
            out.append(path)
    return out


def draw(entry: dict, folder: Path) -> Path:
    """The frame's top bar with the measured slots outlined (Radiant cyan, Dire red), saved as PNG."""
    with Image.open(entry["file"]) as im:
        img = im.convert("RGB")
    u = entry["unit_px"]
    rects = entry["rects"]
    x0 = max(0, int(rects[0][0] - 0.05 * u))
    x1 = min(img.width, int(rects[9][0] + rects[9][2] + 0.05 * u))
    img = img.crop((x0, 0, x1, min(img.height, int(0.1 * u) + 1)))
    d = ImageDraw.Draw(img)
    for i, (x, y, w, h) in enumerate(rects):
        d.rectangle((x - x0, y, x - x0 + w - 1, y + h - 1), outline=(0, 229, 255) if i < 5 else (255, 80, 80))
    img = img.resize((img.width * 2, img.height * 2), Image.NEAREST)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{Path(entry['file']).stem}-slots.png"
    img.save(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure Dota's top-bar layout from saved frames (doty/vision.py).")
    ap.add_argument("frames", nargs="+", help="frames of the Dota window, topbar-*.png crops, or capture folders")
    ap.add_argument("--height", type=int, default=None,
                    help="the game window's height in px, for top-bar crops (else guessed from the crop)")
    ap.add_argument("--draw", metavar="DIR", help="save each frame's top bar with the found slots outlined")
    ap.add_argument("--min-contrast", type=float, default=BAR_CONTRAST[1],
                    help=f"leave out frames whose slots stand out less than this (default {BAR_CONTRAST[1]})")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    args = ap.parse_args(argv)
    paths = expand(args.frames)
    if not paths:
        print("No frames found.", file=sys.stderr)
        return 2
    result = calibrate_from_frames(paths, window_height=args.height, min_contrast=args.min_contrast)
    for f in result["per_frame"]:
        name = Path(f["file"]).name
        if "error" in f:
            print(f"--  {name}: {f['error']}")
            continue
        u = f["unit_px"]
        print(f"{'ok' if f['ok'] else '--'}  {name}: contrast {f['contrast']:.2f}, "
              f"slots {f['slot_w'] * u:.1f}x{f['slot_h'] * u:.1f} px, gap {f['gap'] * u:.1f}, "
              f"clock gap {f['clock_gap'] * u:.1f}, top {f['top'] * u:.1f}, centre {f['center'] * u:+.1f}")
    if args.json:
        print(json.dumps(result, indent=1))
    if args.draw:
        for f in result["per_frame"]:
            if "rects" in f:
                draw(f, Path(args.draw))
        print(f"Outlined slots saved to {args.draw}")
    if not result["used"]:
        print(f"No frame showed the top bar clearly (contrast under {args.min_contrast}). Use frames from "
              "inside a match with the top bar on screen, or check the outlines (--draw) and lower --min-contrast.")
        return 1
    spread = ", ".join(f"{k} {v}" for k, v in result["spread"].items())
    print(f"\nUsed {result['used']} of {result['frames']} frames (spread between them: {spread}).")
    if not result["reliable"]:
        print("Only JPEG frames were usable, and JPEG smears the gaps between portraits, so this isn't reliable:")
        print(result["layout"])
        print("Use the topbar-*.png crops capture mode saves (pass the capture folder).")
        return 1
    print("Paste into DEFAULT_LAYOUT in doty/vision.py once the outlines look right:")
    print(result["layout"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
