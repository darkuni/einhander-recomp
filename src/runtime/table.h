// Lookup tables produced by the recompiler (gen/table.cpp)
#pragma once
#include "recomp.h"
typedef void (*GuestFn)(Cpu*);
struct FuncEntry { uint32_t addr; GuestFn fn; };
// A code image (an executable or an overlay) and the words that identify it in RAM.
struct ImageEntry {
    const char* name;
    uint32_t base, end;
    bool exe;
    const FuncEntry* funcs;
    int count;
    const uint32_t* sig;   // (address, word) pairs
    int nsig;
};
extern const ImageEntry g_images[];
extern const size_t g_images_count;
