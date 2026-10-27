#!/usr/bin/env python3
"""Static recompiler: PlayStation MIPS (R3000A) -> C++.

    python3 recomp.py <game dir> <config dir> <out dir> [functions per file]

Inputs (game dir, made by tools/prepare_disc.py), listed in config/images.txt:
    exe <file>                     a PS-X EXE (several executables may load at the same address)
    ovl <file> <base hex> <exe>    a raw code overlay loaded at <base> while <exe> is running
Config:
    images.txt           the code images
    psyq.txt             PsyQ library names (tools/psyq_match.py): "<image> <addr> <name>"
    functions.txt        extra entry points / names / jump tables (hand-maintained)
    hle.txt              guest functions replaced by host code

Output: one C++ function per guest function (<image prefix>_XXXXXXXX), plus a table of images
(address range, signature words that tell it apart from every other image at those addresses,
and its functions) that the runtime uses for indirect calls.
"""
import os
import re
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mips import decode, reads, writes, LOADS  # noqa: E402

RAM_END = 0x80200000

REGN = ['zero', 'at', 'v0', 'v1', 'a0', 'a1', 'a2', 'a3', 't0', 't1', 't2', 't3', 't4', 't5', 't6', 't7',
        's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 't8', 't9', 'k0', 'k1', 'gp', 'sp', 'fp', 'ra']


class Image:
    """A block of code+data loaded at a fixed address (the executable or one overlay)."""

    def __init__(self, name, prefix, base, data, host=None, entry=None):
        self.name = name
        self.prefix = prefix          # function name prefix
        self.base = base
        self.data = data
        self.end = base + len(data)
        self.host = host              # overlays: the executable that loads them
        self.entry = entry            # executables: entry point
        self.cache = {}

    def has(self, a):
        return self.base <= a < self.end - 3

    def word(self, a):
        o = a - self.base
        return struct.unpack_from('<I', self.data, o)[0]

    def insn(self, a):
        i = self.cache.get(a)
        if i is None:
            i = decode(self.word(a), a)
            self.cache[a] = i
        return i


def is_plausible_entry(img, a):
    """Cheap filter for address-taken constants / data pointers that might be functions."""
    if a & 3 or not img.has(a):
        return False
    i = img.insn(a)
    if i.kind == 'invalid' or i.word == 0:
        return False
    # Accept a stack-frame setup, or anything that follows a 'jr ra' + delay slot (end of previous function)
    if i.op == 9 and i.rs == 29 and i.rt == 29 and i.simm < 0:
        return True
    if img.has(a - 8) and img.word(a - 8) == 0x03e00008:
        return True
    return False


class Func:
    def __init__(self, img, entry):
        self.img = img
        self.entry = entry
        self.addrs = set()        # instruction addresses that belong to this function (incl. delay slots)
        self.delay = set()        # delay slot addresses
        self.labels = set()       # branch targets inside the function
        self.calls = set()        # jal targets
        self.tails = set()        # tail-call targets (jumps/fallthrough into another entry)
        self.tables = {}          # jr address -> (table addr, [targets])
        self.dyn_jumps = set()    # jr addresses that need runtime dispatch
        self.consts = set()       # address-taken constants seen (lui/addiu, lui/ori)
        self.bad = False

    @property
    def name(self):
        return '%s_%08x' % (self.img.prefix, self.entry)


class Recompiler:
    def __init__(self, gamedir, cfgdir):
        self.images = []
        self.exes = []
        self.overlays = []
        byname = {}
        for line in open(os.path.join(cfgdir, 'images.txt')):
            line = line.split('#')[0].strip()
            if not line:
                continue
            p = line.split()
            d = open(os.path.join(gamedir, p[1]), 'rb').read()
            if p[0] == 'exe':
                assert d[:8] == b'PS-X EXE', p[1]
                pc, gp, t_addr, t_size = struct.unpack_from('<4I', d, 0x10)
                img = Image(p[1], 'e%d' % len(self.exes), t_addr, d[0x800:0x800 + t_size], entry=pc)
                self.exes.append(img)
            else:
                host = byname[p[3]]
                img = Image(p[1], 'o%02d' % len(self.overlays), int(p[2], 16), d, host=host)
                self.overlays.append(img)
            byname[p[1]] = img
            self.images.append(img)
        self.byname = byname
        self.names = {}
        self.extra = defaultdict(set)       # image name -> extra entries
        self.notfunc = set()
        self.forced_tables = {}
        self.interp_only = set()
        self.hle = {}                       # (image name, addr) -> host function
        self.load_cfg(cfgdir)
        self.funcs = {}                     # (image name, entry) -> Func
        self.swept = set()                  # entries found only by the gap sweep

    # ---------------- config ----------------
    def load_cfg(self, cfgdir):
        p = os.path.join(cfgdir, 'psyq.txt')
        if os.path.exists(p):
            for line in open(p):
                if line.startswith('#') or not line.strip():
                    continue
                im, a, n = line.split()[:3]
                self.names[(im, int(a, 16))] = n
        p = os.path.join(cfgdir, 'functions.txt')
        if os.path.exists(p):
            for line in open(p):
                line = line.split('#')[0].strip()
                if not line:
                    continue
                parts = line.split()
                kind = parts[0]
                img = parts[1]
                a = int(parts[2], 16)
                if kind == 'func':
                    self.extra[img].add(a)
                    if len(parts) > 3:
                        self.names[(img, a)] = parts[3]
                elif kind == 'notfunc':
                    self.notfunc.add((img, a))
                elif kind == 'interp':   # run in the interpreter (code that jumps through ra)
                    self.interp_only.add((img, a))
                    self.extra[img].add(a)
                elif kind == 'table':   # table <img> <jr addr> <table addr> <count>
                    self.forced_tables[(img, a)] = (int(parts[3], 16), int(parts[4]))
        p = os.path.join(cfgdir, 'hle.txt')
        if os.path.exists(p):
            for line in open(p):
                line = line.split('#')[0].strip()
                if not line:
                    continue
                im, a, host = line.split()[:3]
                self.hle[(im, int(a, 16))] = host

    # ---------------- analysis ----------------
    def target_image(self, img, a):
        """Image that a call/jump target in `img` resolves to statically (None if dynamic/other)."""
        if img.has(a):
            return img
        if img.host is not None and img.host.has(a):
            return img.host
        return None

    def explore(self, img, entry, entries):
        f = Func(img, entry)
        work = [entry]
        seen = set()
        while work:
            a = work.pop()
            while True:
                if a in seen:
                    break
                if not img.has(a):
                    f.bad = True
                    break
                if a != entry and a in entries:
                    # ran into another function: tail call
                    f.tails.add(a)
                    break
                seen.add(a)
                i = img.insn(a)
                f.addrs.add(a)
                if i.kind == 'invalid':
                    f.bad = True
                    break
                if i.has_delay:
                    d = a + 4
                    if not img.has(d):
                        f.bad = True
                        break
                    f.addrs.add(d)
                    f.delay.add(d)
                    di = img.insn(d)
                    if di.has_delay:
                        f.bad = True    # branch in delay slot: not supported
                        break
                    self.note_consts(img, f, a)
                    k = i.kind
                    if k == 'branch':
                        t = i.target
                        if i.is_link:
                            f.calls.add(t)
                        elif t != entry and t in entries:
                            f.tails.add(t)
                        else:
                            f.labels.add(t)
                            work.append(t)
                        # conditional: fallthrough after delay slot
                        a = a + 8
                        if a != entry and a in entries:
                            f.tails.add(a)
                            break
                        continue
                    if k == 'j':
                        t = i.target
                        if t == entry:
                            f.labels.add(t)
                        elif t in entries or not img.has(t) or self.target_image(img, t) is not img:
                            f.tails.add(t)
                        else:
                            f.labels.add(t)
                            work.append(t)
                        break
                    if k == 'jal':
                        f.calls.add(i.target)
                        a = a + 8
                        continue
                    if k == 'jalr':
                        a = a + 8
                        continue
                    if k == 'jr':
                        if i.rs != 31:
                            tab = self.find_table(img, f, a)
                            if tab:
                                f.tables[a] = tab
                                for t in tab[1]:
                                    f.labels.add(t)
                                    work.append(t)
                            else:
                                f.dyn_jumps.add(a)
                        break
                else:
                    if i.kind == 'sys' and i.mn == 'break':
                        pass
                    a = a + 4
        return f

    def note_consts(self, img, f, a):
        pass

    def find_table(self, img, f, jr_addr):
        forced = self.forced_tables.get((img.name, jr_addr))
        jr = img.insn(jr_addr)
        reg = jr.rs
        tab_addr = None
        bound = None
        if forced:
            tab_addr, bound = forced
        else:
            # Backtrack: lw reg, lo(rX); addu rX, rA, rB; lui rA, hi  (GCC switch pattern)
            a = jr_addr - 4
            lw = None
            for _ in range(12):
                if not img.has(a):
                    break
                i = img.insn(a)
                if i.mn == 'lw' and i.rt == reg:
                    lw = i
                    break
                if writes(i) == reg:
                    break
                a -= 4
            if lw is None:
                return None
            base_reg = lw.rs
            hi = None
            lo = lw.simm
            b = lw.pc - 4
            idx_regs = set()
            for _ in range(16):
                if not img.has(b):
                    break
                i = img.insn(b)
                w = writes(i)
                if w == base_reg:
                    if i.mn == 'addu':
                        # one of rs/rt holds the table base (lui), the other the scaled index
                        cands = (i.rs, i.rt)
                        base_reg = None
                        # find a lui for either
                        c = b - 4
                        for _ in range(16):
                            if not img.has(c):
                                break
                            j = img.insn(c)
                            wj = writes(j)
                            if wj in cands:
                                if j.mn == 'lui' and hi is None:
                                    hi = j.imm << 16
                                    idx_regs = set(cands) - {j.rt}
                                    break
                                if j.mn == 'addiu' and j.rs == j.rt and hi is None:
                                    # lui x, hi; addiu x, x, lo ; addu ...
                                    lo += j.simm
                            c -= 4
                        break
                    elif i.mn == 'lui':
                        hi = i.imm << 16
                        break
                    elif i.mn == 'addiu':
                        lo += i.simm
                        base_reg = i.rs
                    else:
                        break
                b -= 4
            if hi is None:
                return None
            tab_addr = (hi + lo) & 0xffffffff
            # bound: look for sltiu x, idx, N before
            c = lw.pc - 4
            for _ in range(40):
                if not img.has(c):
                    break
                j = img.insn(c)
                if j.mn == 'sltiu':
                    bound = j.imm
                    break
                c -= 4
        timg = self.target_image(img, tab_addr)
        if timg is None:
            return None
        targets = []
        n = bound if bound else 512
        for k in range(n):
            ta = tab_addr + 4 * k
            if not timg.has(ta):
                break
            t = timg.word(ta)
            if not img.has(t) or t & 3:
                if bound:
                    return None
                break
            targets.append(t)
        if not targets:
            return None
        return (tab_addr, targets)

    def scan_consts(self, img, f):
        """Address-taken function candidates: lui/addiu & lui/ori pairs inside the function."""
        out = set()
        his = {}
        for a in sorted(f.addrs):
            i = img.insn(a)
            if i.mn == 'lui':
                his[i.rt] = (i.imm << 16, a)
                continue
            if i.mn in ('addiu', 'ori') and i.rs in his:
                hi, ha = his[i.rs]
                if a - ha <= 40:
                    v = (hi + (i.simm if i.mn == 'addiu' else i.imm)) & 0xffffffff
                    out.add(v)
            w = writes(i)
            if w in his and i.mn != 'lui':
                del his[w]
            if i.has_delay and i.kind in ('j', 'jr'):
                his.clear()
        return out

    def scan_data_ptrs(self, img, code):
        """Words outside code that point at plausible function starts in `img` or the exe."""
        out = set()
        for o in range(0, len(img.data) - 3, 4):
            a = img.base + o
            if a in code:
                continue
            v = struct.unpack_from('<I', img.data, o)[0]
            if img.has(v) and (img.name, v) not in self.notfunc and is_plausible_entry(img, v):
                out.add((img, v))
            elif img.host is not None and img.host.has(v) and is_plausible_entry(img.host, v):
                out.add((img.host, v))
            else:
                # data of one overlay (or the executable) pointing into other overlays loaded
                # alongside it, e.g. the game engine's tables of stage handlers
                for o in self.overlays:
                    if o is not img and o.has(v) and (o.host is img or o.host is img.host) \
                            and (o.name, v) not in self.notfunc and is_plausible_entry(o, v):
                        out.add((o, v))
        return out

    def analyze(self):
        entries = defaultdict(set)      # image name -> entries
        byname = {im.name: im for im in self.images}
        for ex in self.exes:
            entries[ex.name].add(ex.entry)
        for (imn, a) in self.names:
            if byname.get(imn) and byname[imn].has(a):
                entries[imn].add(a)
        for imn, s in self.extra.items():
            entries[imn] |= s
        for (imn, a) in self.hle:
            entries[imn].add(a)
        # Overlay roots: calls from an executable to addresses outside it may land in its overlays
        ovl_calls = defaultdict(set)
        dptr = defaultdict(set)         # entries found only through data words (recomputed each round)
        changed = True
        rounds = 0
        while changed:
            rounds += 1
            changed = False
            self.funcs = {}
            for im in self.images:
                es = entries[im.name] | dptr[im.name]
                for e in sorted(es):
                    if (im.name, e) in self.notfunc:
                        continue
                    f = self.explore(im, e, es)
                    self.funcs[(im.name, e)] = f
            # gather new entries
            code = defaultdict(set)
            for (imn, e), f in self.funcs.items():
                code[imn] |= f.addrs
            new = defaultdict(set)
            for (imn, e), f in self.funcs.items():
                im = byname[imn]
                for t in f.calls | (f.tails - dptr[imn]):
                    ti = self.target_image(im, t)
                    if ti is not None:
                        new[ti.name].add(t)
                    elif im.host is None:
                        ovl_calls[im.name].add(t)
                for t in self.scan_consts(im, f):
                    for cand in (im, im.host):
                        if cand is not None and cand.has(t) and (cand.name, t) not in self.notfunc \
                                and t not in code[cand.name] and is_plausible_entry(cand, t):
                            new[cand.name].add(t)
                    if im.host is None and not im.has(t):
                        ovl_calls[im.name].add(t)
            # jump table words are data pointers into their own function: never entries
            twords = defaultdict(set)
            for (imn, e), f in self.funcs.items():
                for (tab, tgts) in f.tables.values():
                    for k in range(len(tgts)):
                        twords[imn].add(tab + 4 * k)
            ndptr = defaultdict(set)
            for im in self.images:
                for (ti, t) in self.scan_data_ptrs(im, code[im.name] | twords[im.name]):
                    if t not in entries[ti.name]:
                        ndptr[ti.name].add(t)
            if any(ndptr[k] != dptr[k] for k in set(ndptr) | set(dptr)):
                changed = True
            dptr = ndptr
            for ov in self.overlays:
                for t in ovl_calls[ov.host.name]:
                    if ov.has(t) and is_plausible_entry(ov, t):
                        new[ov.name].add(t)
            if not changed and not any(s - entries[k] for k, s in new.items()):
                # converged on references: sweep gaps for unreferenced functions (dead or pointer-only code)
                for im in self.images:
                    for a in self.sweep_gaps(im, code[im.name]):
                        new[im.name].add(a)
                        self.swept.add((im.name, a))
            for imn, s in new.items():
                add = s - entries[imn]
                if add:
                    entries[imn] |= add
                    changed = True
            print('round %d: %d functions' % (rounds, len(self.funcs)), file=sys.stderr)
        for k, v in dptr.items():
            entries[k] |= v
            for a in v:
                self.swept.add((k, a))   # data-pointer-only entries: not used for signatures either
        self.entries = entries

    def sweep_gaps(self, im, code):
        """Starts of clean instruction runs ending in 'jr ra' between known code."""
        # the whole image: an overlay may be entered only through pointers the analysis cannot see
        lo, hi = im.base, im.end - 4
        lo = min(lo, min(code)) if code else lo
        out = set()
        a = lo
        while a < hi:
            if a in code:
                a += 4
                continue
            # skip padding
            if im.word(a) == 0:
                a += 4
                continue
            # candidate start: decode until jr ra + delay; all must be valid and outside code
            b = a
            ok = False
            while b < hi and b not in code:
                i = im.insn(b)
                if i.kind == 'invalid' or (i.has_delay and im.insn(b + 4).has_delay):
                    break
                if i.word == 0x03e00008:
                    ok = True
                    break
                b += 4
            if ok and (im.name, a) not in self.notfunc:
                out.add(a)
                a = b + 8
            else:
                a = b + 4
        return out

    # ---------------- code generation ----------------
    def fname(self, im, a):
        return '%s_%08x' % (im.prefix, a)

    def call_expr(self, img, t):
        """C++ statement that calls guest address t from code in img."""
        ti = self.target_image(img, t)
        if ti is not None and (ti.name, t) in self.hle:
            return '%s(c);' % self.hle[(ti.name, t)]
        if ti is not None and t in self.entries[ti.name] and (ti.name, t) in self.funcs \
                and not self.funcs[(ti.name, t)].bad:
            return '%s(c);' % self.fname(ti, t)
        return 'rt_call(c, 0x%08xu);' % t

    def gen_insn(self, img, i, out, ind):
        """Emit a non-control-flow instruction."""
        mn = i.mn
        rs, rt, rd = 'R(%d)' % i.rs, 'R(%d)' % i.rt, 'R(%d)' % i.rd
        if i.rs == 0:
            rs = '0u'
        if i.rt == 0 and i.kind != 'store' and mn not in ('mtc0', 'mtc2', 'ctc2', 'ctc0'):
            rt_src = '0u'
        else:
            rt_src = 'R(%d)' % i.rt
        e = None
        if i.word == 0:
            return
        k = i.kind
        if k == 'load':
            addr = '%s + 0x%xu' % (rs, i.imm) if i.simm >= 0 else '%s - 0x%xu' % (rs, -i.simm)
            if i.simm == 0:
                addr = rs
            fn = {'lb': '(uint32_t)(int32_t)(int8_t)MEM_RB', 'lbu': 'MEM_RB', 'lh': '(uint32_t)(int32_t)(int16_t)MEM_RH',
                  'lhu': 'MEM_RH', 'lw': 'MEM_RW'}.get(mn)
            if mn in ('lwl', 'lwr'):
                expr = '%s(%s, %s)' % ('MEM_LWL' if mn == 'lwl' else 'MEM_LWR', addr, rt_src)
            else:
                expr = '%s(%s)' % (fn, addr)
            if i.rt == 0:
                out.append(ind + '(void)%s;' % expr)
            else:
                out.append(ind + 'R(%d) = %s;' % (i.rt, expr))
            return
        if k == 'store':
            addr = '%s + 0x%xu' % (rs, i.imm) if i.simm >= 0 else '%s - 0x%xu' % (rs, -i.simm)
            if i.simm == 0:
                addr = rs
            fn = {'sb': 'MEM_WB', 'sh': 'MEM_WH', 'sw': 'MEM_WW', 'swl': 'MEM_SWL', 'swr': 'MEM_SWR'}[mn]
            out.append(ind + '%s(%s, %s);' % (fn, addr, rt_src))
            return
        if k == 'cop':
            if mn == 'lwc2' or mn == 'swc2':
                addr = '%s + 0x%xu' % (rs, i.imm) if i.simm >= 0 else '%s - 0x%xu' % (rs, -i.simm)
                if mn == 'lwc2':
                    out.append(ind + 'gte_write_data(%d, MEM_RW(%s));' % (i.rt, addr))
                elif 12 <= i.rt <= 15:
                    # screen coordinates: also remember the precise (sub-pixel) values for the GPU
                    out.append(ind + 'gte_store_sxy(%s, %d);' % (addr, i.rt))
                else:
                    out.append(ind + 'MEM_WW(%s, gte_read_data(%d));' % (addr, i.rt))
                return
            if mn == 'cop2':
                out.append(ind + 'gte_command(0x%08xu);' % i.word)
                return
            if mn == 'mfc2':
                if i.rt:
                    out.append(ind + 'R(%d) = gte_read_data(%d);' % (i.rt, i.rd))
                return
            if mn == 'cfc2':
                if i.rt:
                    out.append(ind + 'R(%d) = gte_read_ctrl(%d);' % (i.rt, i.rd))
                return
            if mn == 'mtc2':
                out.append(ind + 'gte_write_data(%d, %s);' % (i.rd, rt_src))
                return
            if mn == 'ctc2':
                out.append(ind + 'gte_write_ctrl(%d, %s);' % (i.rd, rt_src))
                return
            if mn == 'mfc0':
                if i.rt:
                    out.append(ind + 'R(%d) = cop0_read(c, %d);' % (i.rt, i.rd))
                return
            if mn == 'mtc0':
                out.append(ind + 'cop0_write(c, %d, %s);' % (i.rd, rt_src))
                return
            if mn == 'rfe':
                out.append(ind + 'cop0_rfe(c);')
                return
            out.append(ind + 'rt_bad_insn(c, 0x%08xu, 0x%08xu);' % (i.pc, i.word))
            return
        if k == 'sys':
            if mn == 'syscall':
                out.append(ind + 'rt_syscall(c, 0x%08xu);' % i.pc)
            else:
                out.append(ind + 'rt_break(c, 0x%08xu);' % i.pc)
            return
        if k == 'invalid':
            out.append(ind + 'rt_bad_insn(c, 0x%08xu, 0x%08xu);' % (i.pc, i.word))
            return
        imm = i.imm
        simm = i.simm
        if mn in ('addi', 'addiu'):
            e = (rt, '%s + 0x%xu' % (rs, simm & 0xffffffff) if simm >= 0 else '%s - 0x%xu' % (rs, -simm))
            if simm == 0:
                e = (rt, rs)
        elif mn == 'slti':
            e = (rt, '(int32_t)%s < %d' % (rs, simm))
        elif mn == 'sltiu':
            e = (rt, '%s < 0x%xu' % (rs, simm & 0xffffffff))
        elif mn == 'andi':
            e = (rt, '%s & 0x%xu' % (rs, imm))
        elif mn == 'ori':
            e = (rt, '%s | 0x%xu' % (rs, imm))
        elif mn == 'xori':
            e = (rt, '%s ^ 0x%xu' % (rs, imm))
        elif mn == 'lui':
            e = (rt, '0x%xu' % (imm << 16))
        elif mn == 'sll':
            e = (rd, '%s << %d' % (rt_src, i.sa))
        elif mn == 'srl':
            e = (rd, '%s >> %d' % (rt_src, i.sa))
        elif mn == 'sra':
            e = (rd, '(uint32_t)((int32_t)%s >> %d)' % (rt_src, i.sa))
        elif mn == 'sllv':
            e = (rd, '%s << (%s & 31)' % (rt_src, rs))
        elif mn == 'srlv':
            e = (rd, '%s >> (%s & 31)' % (rt_src, rs))
        elif mn == 'srav':
            e = (rd, '(uint32_t)((int32_t)%s >> (%s & 31))' % (rt_src, rs))
        elif mn in ('add', 'addu'):
            e = (rd, '%s + %s' % (rs, rt_src))
        elif mn in ('sub', 'subu'):
            e = (rd, '%s - %s' % (rs, rt_src))
        elif mn == 'and':
            e = (rd, '%s & %s' % (rs, rt_src))
        elif mn == 'or':
            e = (rd, '%s | %s' % (rs, rt_src))
        elif mn == 'xor':
            e = (rd, '%s ^ %s' % (rs, rt_src))
        elif mn == 'nor':
            e = (rd, '~(%s | %s)' % (rs, rt_src))
        elif mn == 'slt':
            e = (rd, '(int32_t)%s < (int32_t)%s' % (rs, rt_src))
        elif mn == 'sltu':
            e = (rd, '%s < %s' % (rs, rt_src))
        elif mn == 'mfhi':
            e = (rd, 'c->hi')
        elif mn == 'mflo':
            e = (rd, 'c->lo')
        elif mn == 'mthi':
            out.append(ind + 'c->hi = %s;' % rs)
            return
        elif mn == 'mtlo':
            out.append(ind + 'c->lo = %s;' % rs)
            return
        elif mn == 'mult':
            out.append(ind + '{ int64_t m = (int64_t)(int32_t)%s * (int32_t)%s; c->lo = (uint32_t)m; c->hi = (uint32_t)((uint64_t)m >> 32); }'
                       % (rs, rt_src))
            return
        elif mn == 'multu':
            out.append(ind + '{ uint64_t m = (uint64_t)%s * %s; c->lo = (uint32_t)m; c->hi = (uint32_t)(m >> 32); }'
                       % (rs, rt_src))
            return
        elif mn == 'div':
            out.append(ind + 'cpu_div(c, %s, %s);' % (rs, rt_src))
            return
        elif mn == 'divu':
            out.append(ind + 'cpu_divu(c, %s, %s);' % (rs, rt_src))
            return
        if e is None:
            out.append(ind + 'rt_bad_insn(c, 0x%08xu, 0x%08xu);' % (i.pc, i.word))
            return
        dst, val = e
        if dst in ('R(0)', '0u'):
            return
        out.append(ind + '%s = %s;' % (dst, val))

    def gen_func(self, f):
        img = f.img
        out = []
        ent = f.entry
        nm = self.names.get((img.name, ent))
        out.append('// %s %08x%s' % (img.name, ent, (' ' + nm) if nm else ''))
        out.append('void %s(Cpu* c) {' % f.name)
        if f.bad:
            out.append('    rt_interp(c, 0x%08xu);  // analysis failed; run in the interpreter' % ent)
            out.append('}')
            return out
        if (img.name, ent) in self.interp_only:
            out.append('    rt_interp(c, 0x%08xu);  // uses ra as a continuation pointer: interpreted' % ent)
            out.append('}')
            return out
        addrs = sorted(f.addrs)
        aset = f.addrs
        back_targets = set()
        for a in addrs:
            if a in f.delay:
                continue
            i = img.insn(a)
            if i.kind in ('branch', 'j') and i.target in f.labels and i.target <= a:
                back_targets.add(i.target)
            if a in f.tables:
                for t in f.tables[a][1]:
                    if t <= a:
                        back_targets.add(t)
        labels = set(f.labels) | {ent} | getattr(f, 'extra_labels', set())
        out.append('    FUNC_ENTRY(c, 0x%08xu);' % ent)
        out.append('    CHECK_EVENTS(c);')
        if addrs[0] < ent:
            # code reached by jumps lies before the entry point: start at the entry
            out.append('    goto L_%08x;' % ent)
        ind = '    '
        cyc = 0
        # determine load-delay hazards: insn at a+4 reads the register a load at a writes
        def emit_body_insn(a, i, nxt):
            pass

        def emit_jump(t, ind2):
            if t in labels and t in aset:
                return ['%sgoto L_%08x;' % (ind2, t)]
            return ['%s%s return;' % (ind2, self.call_expr(img, t))]

        prev_fall = None    # address the previous emitted instruction falls through to
        k_iter = 0
        n = len(addrs)
        while k_iter < n:
            a = addrs[k_iter]
            k_iter += 1
            i = img.insn(a)
            if a in f.delay:
                # Only reached directly when something jumps into a delay slot.
                if a in labels:
                    if prev_fall is not None:
                        # the branch before us falls through past this delay slot
                        if prev_fall in aset:
                            out.append('    goto L_%08x;' % prev_fall)
                            labels.add(prev_fall)
                        else:
                            out.append('    %s return;' % self.call_expr(img, prev_fall))
                    out.append('L_%08x:' % a)
                    lines = []
                    self.gen_insn(img, i, lines, ind)
                    out.extend(lines)
                    out.append('    c->cycles += 1;')
                    nxt = a + 4
                    if nxt in aset and nxt not in f.delay:
                        out.append('    goto L_%08x;' % nxt)
                        labels.add(nxt)
                    else:
                        out.append('    %s return;' % self.call_expr(img, nxt))
                prev_fall = None
                continue
            if prev_fall is not None and prev_fall != a:
                # previous instruction falls through to something not next in order
                if prev_fall in aset:
                    out.append('    goto L_%08x;' % prev_fall)
                    labels.add(prev_fall)
                else:
                    out.append('    c->cycles += %d; %s return;' % (cyc, self.call_expr(img, prev_fall)))
                cyc = 0
            if a in labels:
                if cyc:
                    out.append('    c->cycles += %d;' % cyc)
                    cyc = 0
                out.append('L_%08x:' % a)
                if a in back_targets and a != ent:
                    out.append('    CHECK_EVENTS(c);')
            if not i.has_delay:
                # load delay: if this is a load and the next instruction reads its target
                if i.kind == 'load' and i.rt != 0 and i.mn not in ('lwl', 'lwr'):
                    na = a + 4
                    if na in aset and na not in labels and na not in f.delay:
                        ni = img.insn(na)
                        if not ni.has_delay and ni.kind != 'invalid' and i.rt in reads(ni) \
                                and not (ni.mn in ('lwl', 'lwr') and ni.rt == i.rt):
                            # emulate: the next instruction sees the old value
                            tmp = []
                            self.gen_insn(img, i, tmp, ind)
                            ln = tmp[0].strip()
                            m = re.match(r'R\((\d+)\) = (.*);$', ln)
                            out.append('    { uint32_t ld = %s;' % m.group(2))
                            tmp2 = []
                            self.gen_insn(img, ni, tmp2, '      ')
                            out.extend(tmp2)
                            if writes(ni) == i.rt:
                                out.append('      (void)ld; }  // load result cancelled by overwrite')
                            else:
                                out.append('      R(%d) = ld; }' % i.rt)
                            cyc += 2
                            k_iter += 1
                            prev_fall = na + 4
                            continue
                self.gen_insn(img, i, out, ind)
                cyc += 1
                prev_fall = a + 4
                continue
            # ---- control flow with delay slot ----
            d = a + 4
            di = img.insn(d)
            dl = []
            self.gen_insn(img, di, dl, '        ')
            cyc += 2
            k = i.kind
            if k == 'branch':
                rs_ = '0u' if i.rs == 0 else 'R(%d)' % i.rs
                rt_ = '0u' if i.rt == 0 else 'R(%d)' % i.rt
                cond = {'beq': '%s == %s' % (rs_, rt_), 'bne': '%s != %s' % (rs_, rt_),
                        'blez': '(int32_t)%s <= 0' % rs_, 'bgtz': '(int32_t)%s > 0' % rs_,
                        'bltz': '(int32_t)%s < 0' % rs_, 'bgez': '(int32_t)%s >= 0' % rs_,
                        'bltzal': '(int32_t)%s < 0' % rs_, 'bgezal': '(int32_t)%s >= 0' % rs_}[i.mn]
                t = i.target
                out.append('    {')
                if i.rs == i.rt and i.mn == 'beq':
                    cond = 'true'
                out.append('        bool t = %s;' % cond)
                if i.is_link:
                    out.append('        R(31) = 0x%08xu;' % (a + 8))
                out.extend(dl)
                out.append('        c->cycles += %d;' % cyc)
                cyc = 0
                if i.is_link:
                    out.append('        if (t) %s' % self.call_expr(img, t))
                else:
                    jl = emit_jump(t, '')
                    out.append('        if (t) { %s }' % jl[0].strip())
                out.append('    }')
                prev_fall = a + 8
                if d not in labels:  # skip delay slot in linear order
                    k_iter += 1
                continue
            if k == 'j':
                t = i.target
                out.extend(['    {'] + dl + ['        c->cycles += %d;' % cyc])
                cyc = 0
                out.extend(emit_jump(t, '        '))
                out.append('    }')
                prev_fall = None
                if d not in labels:
                    k_iter += 1
                continue
            if k == 'jal':
                out.append('    R(31) = 0x%08xu;' % (a + 8))
                out.extend(dl)
                out.append('    c->cycles += %d;' % cyc)
                cyc = 0
                out.append('    %s' % self.call_expr(img, i.target))
                prev_fall = a + 8
                if d not in labels:
                    k_iter += 1
                continue
            if k == 'jalr':
                out.append('    { uint32_t t = R(%d);' % i.rs)
                if i.rd:
                    out.append('    R(%d) = 0x%08xu;' % (i.rd, a + 8))
                out.extend(dl)
                out.append('    c->cycles += %d;' % cyc)
                cyc = 0
                out.append('    rt_call(c, t); }')
                prev_fall = a + 8
                if d not in labels:
                    k_iter += 1
                continue
            if k == 'jr':
                if i.rs == 31:
                    out.extend(['    {'] + dl + ['        c->cycles += %d;' % cyc, '        return;', '    }'])
                    cyc = 0
                    prev_fall = None
                    if d not in labels:
                        k_iter += 1
                    continue
                out.append('    { uint32_t t = R(%d);' % i.rs)
                out.extend(dl)
                out.append('    c->cycles += %d;' % cyc)
                cyc = 0
                if a in f.tables:
                    tab, tgts = f.tables[a]
                    out.append('    switch (t) {')
                    for t in sorted(set(tgts)):
                        if t in aset:
                            out.append('    case 0x%08xu: goto L_%08x;' % (t, t))
                        else:
                            out.append('    case 0x%08xu: %s return;' % (t, self.call_expr(img, t)))
                    out.append('    default: rt_call(c, t); return;')
                    out.append('    } }')
                else:
                    # dynamic jump: maybe to one of our own labels, else a tail call
                    out.append('    rt_call(c, t); return; }')
                prev_fall = None
                if d not in labels:
                    k_iter += 1
                continue
            raise AssertionError(k)
        if prev_fall is not None:
            if prev_fall in aset:
                out.append('    goto L_%08x;' % prev_fall)
                labels.add(prev_fall)
            else:
                out.append('    c->cycles += %d; %s return;' % (cyc, self.call_expr(img, prev_fall)))
        out.append('}')
        # emit labels that are referenced but never placed (should not happen)
        text = '\n'.join(out)
        placed = set(int(x, 16) for x in re.findall(r'^L_([0-9a-f]{8}):', text, re.M))
        used = set(int(x, 16) for x in re.findall(r'goto L_([0-9a-f]{8});', text))
        missing = used - placed
        if missing:
            assert not (missing & getattr(f, 'extra_labels', set())), '%s: labels %s' % (f.name, missing)
            f.extra_labels = getattr(f, 'extra_labels', set()) | missing
            return self.gen_func(f)
        # drop unused labels (avoid warnings)
        res = []
        for line in out:
            m = re.match(r'^L_([0-9a-f]{8}):$', line)
            if m and int(m.group(1), 16) not in used:
                continue
            res.append(line)
        return res

    def signature(self, im, base_words=8):
        """(addr, word) pairs of instruction words that identify `im` in RAM: spread over its code,
        plus words that differ from every other image covering the same addresses."""
        # functions found only by the gap sweep or through data words may be data that looks like
        # code: not used
        code = sorted(a for (imn, e), f in self.funcs.items() if imn == im.name and not f.bad
                      and (imn, e) not in self.swept for a in f.addrs if im.word(a) != 0)
        if not code:
            return []
        picked = []
        step = max(1, len(code) // base_words)
        for k in range(0, len(code), step):
            picked.append(code[k])
        others = [o for o in self.images if o is not im and o.base < im.end and im.base < o.end]
        for o in others:
            need = 2 - sum(1 for a in picked if o.has(a) and o.word(a) != im.word(a))
            if need <= 0:
                continue
            diff = [a for a in code if o.has(a) and o.word(a) != im.word(a) and a not in picked]
            if not diff:
                # o covers all of our code with the same words: tell them apart where only we have code
                diff = [a for a in code if not o.has(a) and a not in picked]
            picked += diff[:: max(1, len(diff) // need)][:need]
        return [(a, im.word(a)) for a in sorted(set(picked))]

    def write(self, outdir, per_file):
        os.makedirs(outdir, exist_ok=True)
        funcs = [f for k, f in sorted(self.funcs.items(), key=lambda kv: (kv[1].img.prefix, kv[0][1])) if not f.bad]
        hdr = ['// Generated by recomp.py - do not edit', '#pragma once', '#include "../runtime/recomp.h"', '']
        for f in funcs:
            hdr.append('void %s(Cpu* c);' % f.name)
        for h in sorted(set(self.hle.values())):
            hdr.append('void %s(Cpu* c);' % h)
        open(os.path.join(outdir, 'funcs.h'), 'w').write('\n'.join(hdr) + '\n')
        nfile = 0
        for s in range(0, len(funcs), per_file):
            chunk = funcs[s:s + per_file]
            lines = ['// Generated by recomp.py - do not edit', '#include "funcs.h"',
                     '#define R(n) c->r[n]', '']
            for f in chunk:
                lines.extend(self.gen_func(f))
                lines.append('')
            path = os.path.join(outdir, 'funcs_%03d.cpp' % nfile)
            data = '\n'.join(lines) + '\n'
            if not os.path.exists(path) or open(path).read() != data:
                open(path, 'w').write(data)
            nfile += 1
        # remove stale files
        for fn in os.listdir(outdir):
            m = re.match(r'funcs_(\d+)\.cpp$', fn)
            if m and int(m.group(1)) >= nfile:
                os.remove(os.path.join(outdir, fn))
        # lookup tables
        t = ['// Generated by recomp.py - do not edit', '#include "funcs.h"', '#include "../runtime/table.h"', '']
        for k, im in enumerate(self.images):
            fl = [f for f in funcs if f.img is im]
            t.append('static const FuncEntry img%d_funcs[] = {' % k)
            for f in fl:
                t.append('    {0x%08xu, %s},' % (f.entry, self.hle.get((im.name, f.entry), f.name)))
            if not fl:
                t.append('    {0, nullptr},')
            t.append('};')
            sig = self.signature(im)
            t.append('static const uint32_t img%d_sig[] = {%s};' % (k, ', '.join('0x%08xu, 0x%08xu' % aw for aw in sig)))
            im.nfuncs, im.nsig = len(fl), len(sig)
        t.append('const ImageEntry g_images[] = {')
        for k, im in enumerate(self.images):
            t.append('    {"%s", 0x%08xu, 0x%08xu, %s, img%d_funcs, %d, img%d_sig, %d},'
                     % (im.name, im.base, im.end, 'true' if im.host is None else 'false', k, im.nfuncs, k, im.nsig))
        t.append('};')
        t.append('const size_t g_images_count = %d;' % len(self.images))
        open(os.path.join(outdir, 'table.cpp'), 'w').write('\n'.join(t) + '\n')
        return nfile


def main():
    gamedir, cfgdir, outdir = sys.argv[1:4]
    per_file = int(sys.argv[4]) if len(sys.argv) > 4 else 300
    r = Recompiler(gamedir, cfgdir)
    r.analyze()
    bad = [f for f in r.funcs.values() if f.bad]
    dyn = sum(len(f.dyn_jumps) for f in r.funcs.values())
    tabs = sum(len(f.tables) for f in r.funcs.values())
    n = r.write(outdir, per_file)
    by_img = defaultdict(int)
    for f in r.funcs.values():
        by_img[f.img.name] += 1
    print('%d functions (%s) in %d files; %d jump tables, %d dynamic jumps, %d failed analysis'
          % (len(r.funcs), ', '.join('%s %d' % kv for kv in sorted(by_img.items())), n, tabs, dyn, len(bad)),
          file=sys.stderr)
    for f in bad[:40]:
        print('  failed: %s' % f.name, file=sys.stderr)


if __name__ == '__main__':
    main()
