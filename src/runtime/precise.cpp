#include "precise.h"
#include "config.h"
#include <unordered_map>
#include <vector>

struct Shadow { uint32_t val; PreciseVtx p; bool ok; };
static std::vector<Shadow> g_ram_shadow(0x200000 / 4);
static Shadow g_scratch_shadow[256];
static std::unordered_map<uint32_t, PreciseVtx> g_by_value;

static Shadow* slot(uint32_t addr) {
    uint32_t a = addr & 0x1fffffffu;
    if (a < 0x800000u) return &g_ram_shadow[(a & 0x1ffffc) >> 2];
    if ((a & 0xfffffc00u) == 0x1f800000u) return &g_scratch_shadow[(a & 0x3fc) >> 2];
    return nullptr;
}

void precise_store(uint32_t addr, uint32_t val, const PreciseVtx& p) {
    if (Shadow* s = slot(addr)) { s->val = val; s->p = p; s->ok = true; }
    if (g_by_value.size() < 65536) g_by_value[val] = p;
}

bool precise_lookup(uint32_t addr, uint32_t val, PreciseVtx* p) {
    if (!g_cfg.subpixel) return false;
    if (addr != 0xffffffffu) {
        Shadow* s = slot(addr);
        if (s && s->ok && s->val == val) { *p = s->p; return true; }
    }
    auto it = g_by_value.find(val);
    if (it == g_by_value.end()) return false;
    *p = it->second;
    return true;
}

void precise_frame() { g_by_value.clear(); }
