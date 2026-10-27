# Technical notes

## Binary layout

- `SCUS_942.43` (boot, text 0x80010000-0x80032000, entry 0x800155FC): logo/loader; it calls
  `LoadExec("cdrom:\SYS.EXE;1")`.
- `SYS.EXE` (text 0x80010000-0x80062000, entry 0x800116FC): PsyQ libraries (libetc, libcd,
  libgpu, libgte, libspu/libsnd, libcard, libapi's BIOS pad driver) and the overlay loader. It
  reads `BININDEX.BIN`: six 2 KB sectors, one per `BINPACKn.BIN`, each a list of
  (start sector, sector count) halfword pairs that ends with a zero count.
- Code lives in overlay entries of `BINPACK0-4.BIN` (word 0 of an entry is its id, usually
  followed by a table of function pointers). They are loaded at fixed link addresses in a few
  regions (0x8008C800: front end / game engine / ending; 0x80146000-0x801B2000: stage code;
  0x801CF000: front-end extras). Each load address was found by testing every 2 KB-aligned base
  and counting the entry's `jal` targets that land on function starts (all hits for every
  overlay listed in `config/images.txt`), and confirmed against RAM for the ones loaded in the
  first stages. `BINPACK5.BIN` holds only streamed data.
- Movies (`MOV*.STR`) and the stage streams (`STAGE*.STR`, XA music + data) are standard STR/XA.

## Code images and dispatch

Several images share addresses (the two executables at 0x80010000, overlays in each region).
For every image the recompiler emits a signature: instruction words spread over its code plus
words that differ from every other image covering the same addresses (only from functions found
by references, not by the gap sweep, so no data words). `lookup_func` (indirect calls,
`jalr`, calls between overlays) checks the signatures of the images covering the target (with a
small most-recently-used cache) and calls the recompiled function; code in no known image runs
in the interpreter. Direct calls inside an image, and from an overlay into `SYS.EXE`, are plain
C calls. Data words of an overlay (or of the executable) that point into another overlay loaded
alongside it (the engine's tables of stage handlers) are entry points too. `OVL_CAPTURE=dir` logs every interpreted entry point with RAM dumps, which is how
unknown overlays were located (`LOG=cdmap` logs which disc sector went to which address).

## Recompiler (`src/recomp.py`)

- Function discovery: entry point, `jal` targets, address-taken constants (`lui`/`addiu`),
  data words pointing at plausible function starts, and a final sweep of unreferenced
  `... jr ra` runs between known code. Iterates to a fixed point (~7100 functions over 25 images).
- One C++ function per guest function; branches become `goto`, calls become direct C calls,
  `jr ra` returns. Branch delay slots are emitted after the condition is evaluated. Jumps into
  the middle of a delay slot (hand-written asm loops) get a separate copy of the slot.
- R3000 load delay: a load followed by an instruction reading the same register is emitted with
  the old value visible to that instruction (the game's GCC code never needs it, but it is handled).
- Jump tables (`lui/addiu/addu/lw/jr`) become `switch`; other indirect jumps and `jalr` go through
  `rt_call`, which dispatches to recompiled code, HLE BIOS functions, or the interpreter.
- Cycle counting per basic block; `CHECK_EVENTS` at function entries and loop back-edges runs the
  hardware scheduler and takes interrupts.
- `config/functions.txt` `interp` entries run in the interpreter (`src/runtime/cpu.cpp`), for code
  that uses `ra` as a continuation pointer (none needed so far in this game).

## Runtime

- **BIOS** (HLE, `bios.cpp`): A0/B0/C0 functions used by the game, events, root counter events,
  interrupt chains (`SysEnqIntRP`) and libetc's `SetCustomExitFromException` dispatcher
  (`ReturnFromException` unwinds back to the interrupt point with a C++ exception).
  Kernel tables live in guest RAM at the real locations (table of tables at 0x100), and the
  runtime follows those pointers because PsyQ's `SetConf` re-allocates the event/thread tables.
  `SetConf` also decodes a `lui/addiu` pair at `[0x474]` to find BIOS variables; a scratch stub
  is provided there. `LoadExec` (A0:51) loads the next executable from the disc and restarts
  through a C++ exception that unwinds to the top level. The BIOS pad driver (`InitPAD`,
  `StartPAD`, `PAD_init`) fills the game's buffers every vblank; libapi's BIOS patches
  (`_patch_pad`, `_send_pad`, `EnablePAD`/`DisablePAD`) are no-ops. The game accepts pad ids
  0x41/0x53/0x73 but reads only the button bytes, and its `SendPAD` actuator buffers are
  initialised once and never written, so the port reports a digital pad (`PAD_ID=73` overrides).
- **Threads**: each guest thread runs
  on its own host thread; only one runs at a time (baton handoff), and the GL context follows.
  The game switches threads from its VSync interrupt by writing the PCB pointer directly, so the
  exception return resumes whatever TCB the PCB points at.
- **CD-ROM** (`cdrom.cpp`): register-level controller (libcd talks to it directly), 1x/2x sector
  timing, 0x800/0x924 sector modes, data FIFO + DMA3, XA-ADPCM decode with file/channel filter,
  CD-DA playback of an audio track (Einhander has none: its music is XA). The BIOS leaves CD interrupts enabled (IE = 0x1F) at boot; libcd
  relies on that.
- **MDEC**: decode per psx-spx (RLE/quant, scale-table IDCT, YUV->RGB at 4/8/15/24 bit). DMA1 waits
  for decoded data; a new decode command discards unread output.
- **GPU**: GP0/GP1 command processing in `gpu.cpp`; `gpu_gl.cpp` renders into a (1024*S)x(512*S)
  RGBA8 VRAM texture (true colour, mask bit in alpha). Texture pages and CLUTs are sampled in the
  shader from a copy of VRAM refreshed only where drawing touched it. Semi-transparency via
  dual-source blending (B-F mode in two passes). 24-bit display mode decoded in a shader for movies.
- **Enhancements** (all optional): texture filtering in the draw shader (four palette-decoded
  texels blended among the opaque ones, the shape taken from the nearest texel, clamped to the
  primitive's own texel bounds so atlas neighbours do not bleed); original 15-bit colour with the
  4x4 dither matrix per original pixel; precise geometry (`precise.cpp`): the GTE keeps an
  unrounded copy of each projected vertex (X = OFX + IR1 * H / SZ with the unshifted MAC values)
  next to the SXY FIFO, `swc2` of SXY stores it in a shadow of the RAM word (the recompiler emits
  `gte_store_sxy`), and the GPU uses it for polygon vertices whose DMA source word still holds
  that value (or, for copied words, matches a projection from this frame); with the depth it also
  gives perspective-correct texturing. About 87% of in-game polygon vertices come out precise.
  XA audio can be resampled with a 32-tap Kaiser-windowed sinc instead of linear; the screen
  filter is part of the final scaling shader.
- **GTE**: full command set per psx-spx (UNR divide, flags, IR3 quirk, MVMVA far-colour bug).
- **SPU**: 24 voices, Gaussian interpolation (hardware table), ADSR/sweep envelopes, pitch
  modulation, noise, reverb with the 39-tap resampling FIR, CD audio input and capture buffers.
- **Controllers**: SIO0 with a DualShock (digital/analog/config mode, rumble mapping). Memory cards
  are handled at BIOS level (`memcard.cpp`) on standard 128 KB card images; libcard's BIOS patches
  are no-ops (`config/hle.txt`).

## Packed disc file (`assets.dat`, `src/runtime/discpack.cpp`)

The whole disc in one file (Einhander: one data track, 277,846 sectors). The import reads a .chd
(libchdr) or a .cue/.bin. Header
(64 bytes: magic `MMAPACK` (format shared with the Muppet Monster port), version, track layout, block count, index offset, index CRC), then
deflate blocks of 16 sectors, then an index (offset, compressed size, size, CRC-32 per block).
A block holds one kind byte per sector followed by the payloads:

| kind | stored | rebuilt on read |
|------|--------|-----------------|
| 0 Mode 2 Form 1 | subheader + 2048 data | sync, MSF header, subheader copy, EDC, P/Q ECC |
| 1 Mode 2 Form 2 | subheader + 2324 data + EDC | sync, MSF header, subheader copy |
| 2 raw | 2352 | - (any sector that does not rebuild exactly) |
| 3 audio | 2352 | - |
| 4 zero | nothing | all zeros |

The import rebuilds every sector from its compact form and from the decompressed block and
compares it with the original, so the pack is bit-exact (an independent check against the .bin
files and a 2400-frame run from each source give identical RAM and frames). The 653 MB .bin
(or 331 MB .chd) becomes 302 MB; a .chd and a .cue/.bin of the same disc give an identical file. The reader keeps the last decoded block; the CD controller reads through the
same `read_raw` interface either way.

## Notable fixes (runtime shared with the Muppet Monster Adventure port, found by comparing against a reference emulator)

- Jump-table case labels must never become function entries (the data-pointer scan used to pick
  them up from `.rodata`); otherwise the dialogue state machine broke and the Hub cutscene hung.
- Overlays such as HUB1 are entered only through run-time pointers: the gap sweep covers the whole
  overlay image.
- GTE 16-bit data registers (IR0-3, VZ, SZ, OTZ) keep only the low half of the written word. The
  GTE was checked against an independent implementation on millions of random register states.
- Byte writes to DICR (libcd updates the enable byte with `sb`) must not acknowledge pending DMA
  IRQ flags; otherwise MDEC slice interrupts were lost and movies dropped frames.
- Memory cards: libmcrd looks up the "bu" device in the BIOS device table and hooks its
  `firstfile`; async file I/O returns 0 at once and completes by event from a later interrupt.
- BIOS string arguments can live in the scratchpad (the game's stack is there).
- BIOS threads: a retired host thread is told apart from its replacement by a generation number.

## Debugging aids

- `einhander.log` next to the exe lists code the game ran outside the recompiled images (it
  still works, in the interpreter); it only appears when that happens.
- `POKE=addr=value,...` (from frame `POKE_FROM`) writes RAM bytes every frame, e.g.
  `POKE=800813c4=3` keeps three lives (0x800813C4 is the lives counter; 0x80081496 and the
  stage request tables at 0x80059B64 + 4 x stage are where a stage jump would start).

- `LOG=bios,cd,irq,thread,ovl,interp,dma,gpu,disp,mdec,...` category logging.
- `SHOT_DIR`, `SHOT_EVERY`, `SHOT_FROM`, `FRAMES`, `NO_THROTTLE`, `STOP_AT_FRAME` (SIGTRAP for gdb;
  generated code is built with line tables so host backtraces map to guest labels).
- `WATCH=800bb224,gp+178c` prints RAM words when they change; `RAM_DUMP_AT=frame:file` dumps RAM.
- Bisect build (`-DCMAKE_CXX_FLAGS=-DRECOMP_BISECT`): `BISECT=lo-hi` runs those functions in the
  interpreter (from `BISECT_FROM_SEC`), to find code generation bugs.
- `MDEC_DUMP=1` writes MDEC input per frame; `tools/bs_decode.py` decodes a frame of an STR file
  independently for comparison.
