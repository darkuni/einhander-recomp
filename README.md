# Einhänder - native recompilation

A static recompilation of **Einhänder** (PlayStation, NTSC-U, `SCUS-94243`, Squaresoft, 1998) to
native code. The game's two MIPS executables (`SCUS_942.43`, which boots and `LoadExec()`s
`SYS.EXE`) and its 23 code overlays (entries of the `BINPACK*.BIN` archives) are translated to C++
and run on Windows (and Linux) with a runtime that replaces the PlayStation hardware and BIOS.
Graphics are re-rendered with OpenGL at higher resolution.

**No game data is included.** You need your own copy of the game as a disc image (`.chd`, or
`.cue` + `.bin`).

## Status

- Boots through the Sony/Squaresoft logos, the memory card check, the intro movie, the title
  screen, fighter and arm selection, and into Stage 1 gameplay, entirely in recompiled code.
- Verified on the headless Linux test build against a reference emulator. The Windows build is
  produced by CI.

See [NOTES.md](NOTES.md) for the technical write-up.

## Running (Windows)

1. Download the latest `Einhander-<version>-win64.zip` from [Releases](https://github.com/darkuni/einhander-recomp/releases)
   (or the `einhander-win64` artifact of any **Build Windows** run on the Actions tab) and unzip it.
2. First run only: put your disc image (`Einhander.chd`, or a `.cue` + its `.bin`) next to
   `einhander.exe` or in a `game` folder beside it, then run `einhander.exe` - or drag the image
   onto it.
3. The disc is imported once into **`assets.dat`** (about 300 MB, under a minute) and checked
   sector by sector to be an exact copy. From then on the game runs from `assets.dat` and the
   disc image can be deleted. The finished install is just:

   ```
   einhander.exe  SDL2.dll  assets.dat  (memcard1.mcd, einhander.ini once created)
   ```

`einhander.exe --import <image> [out.dat]` builds the file without starting the game.

Saves go to `memcard1.mcd` next to the exe - a standard 128 KB PlayStation memory card image, so
saves can be copied to and from emulators. Settings are in `einhander.ini`.

### In-game settings (F1 or Esc, Start+Select on a controller)

| Setting | Options |
|---|---|
| Resolution | 1x (original) to 6x internal resolution (restart); the window follows it (240 lines x the setting, shrunk to fit the desktop) |
| Widescreen 16:9 | wider view of the 3D world (Einhander: the extra width shows enemies entering and leaving at the edges; off by default) |
| VSync, Fullscreen (F11 / Alt+Enter) | |
| Scaling | sharp or smooth |
| Texture filtering | off (original), smooth 3D (2D art and text stay sharp), smooth everything - bilinear on the palette-decoded texels, so transparent edges stay clean |
| Colour | true colour, or the original 15-bit colour with the PlayStation's dither pattern |
| Precise geometry | sub-pixel polygon positions and perspective-correct textures (no wobble or texture warping), PGXP-style |
| Screen filter | off, scanlines, CRT (scanlines + aperture grille + glow; needs a window 600+ pixels tall) |
| Audio resampling | standard, or high quality (windowed-sinc) for the streamed XA music and voices |
| Vibration | **new in this port** (the original has none): off, hits and explosions, or also when firing; strength 25-100% |
| Swap A/B and X/Y | face buttons by position |
| Fast CD loading | levels load faster; music and movies stay real-time |
| Game speed | 1x-8x fast-forward (hold Tab for a burst) |
| Frame rate display | game frames per second and emulation speed (100% = a real console) |
| Keyboard keys | rebind every PlayStation button (or reset to defaults) |
| Controller buttons | rebind every PlayStation button to controller buttons, triggers or stick directions |

### Controls

Any XInput/SDL gamepad works. Einhander reads only the d-pad and buttons, so the left stick also
drives the d-pad. The original game has no vibration (it never sends motor data, and a reference
emulator with a DualShock attached never rumbles either); this port adds it - see Vibration above.
Controllers SDL has no mapping for are used as generic joysticks (buttons, hats, axes), and a
`gamecontrollerdb.txt` next to the exe adds more mappings. Start+Select opens the settings menu.
Keyboard defaults: arrows = d-pad, X = Cross, Z = Circle, A = Square, S = Triangle, Q/W = L1/R1,
1/2 = L2/R2, Enter = Start, Backspace = Select, I/J/K/L = right stick, Tab = fast-forward (hold).
Change them in the F1 menu under **Keyboard keys** (saved in `einhander.ini`).

## Layout

| Path | What it is |
|---|---|
| `src/recomp.py`, `src/mips.py` | Recompiler: the two executables + 23 overlays -> `src/gen/*.cpp` (one C++ function per MIPS function) |
| `src/gen/` | Generated code (committed so CI can build without the disc) |
| `src/runtime/` | Runtime: memory map, HLE BIOS (events, threads, interrupts, memory cards), GTE, GPU -> OpenGL, SPU, CD-ROM (XA/CD-DA), MDEC, controllers, timers, DMA |
| `config/images.txt` | The code images: executables and the BINPACK overlay entries with their load addresses |
| `config/functions.txt` | Extra entry points and functions that run in the interpreter |
| `config/hle.txt` | Guest functions replaced by host code |
| `config/psyq.txt` | PsyQ library names found by signature |
| `tools/` | Disc preparation, disassembler, signature matcher, reference STR/BS decoder |

## Regenerating the code

```sh
python3 tools/prepare_disc.py Einhander.cue game     # executables, archives, overlays (a .chd needs chdman)
python3 src/recomp.py game config src/gen 300
```

## Building

Linux headless test build (EGL, screenshots): `cmake -S src -B build -G Ninja && ninja -C build`,
then `SHOT_DIR=shots SHOT_EVERY=60 FRAMES=3000 ./build/einhander Einhander.chd`.

Windows (cross-compiled with mingw-w64): see `.github/workflows/build-windows.yml`.

## Disclaimer

This is an unofficial fan project. It is **not affiliated with, endorsed by, or sponsored by** Square
Enix, Sony Interactive Entertainment or any of their affiliates. *Einhänder* and all related names and
marks belong to their respective owners. This repository and its releases contain no game data
(graphics, sound, music, movies): those come from your own legally obtained disc image. The recompiled
source and the release exe contain code machine-translated from the game's executables.

## License

The project's own code is released under the [MIT License](LICENSE). Third-party components keep
their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
