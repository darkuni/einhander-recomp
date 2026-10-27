// Einhander (NTSC-U, SCUS-94243) - game-specific constants the runtime needs.
#pragma once
#define GAME_NAME "Einh\xC3\xA4nder"   // UTF-8 (window title, message boxes)
#define GAME_INI "einhander.ini"
#define GAME_BIN "einhander.exe"
#define GAME_EXE_NAME "SCUS_942.43"    // boot executable (it LoadExec()s SYS.EXE)
#define GAME_SERIAL "SCUS-94243"
static const unsigned GAME_EXE_SIZE = 141312u;
static const unsigned GAME_EXE_PC = 0x800155fcu;

// SYSTEM.CNF: TCB = 4, EVENT = 10, STACK = 801FFF00
static const int SYS_TCBS = 4;
static const int SYS_EVENTS = 10;
static const unsigned SYS_STACK = 0x801FFF00u;
