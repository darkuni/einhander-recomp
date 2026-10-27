#!/usr/bin/env python3
"""dis.py <file> <start> <end|+count> [base]  - disassemble a PS-X EXE (or a raw image loaded at base)"""
import sys, struct, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from mips import decode, disasm
data = open(sys.argv[1], 'rb').read()
if len(sys.argv) > 4:
    base = int(sys.argv[4], 16)
else:
    base = struct.unpack_from('<I', data, 0x18)[0]; data = data[0x800:]
s = int(sys.argv[2], 16)
e = s + int(sys.argv[3][1:]) * 4 if sys.argv[3].startswith('+') else int(sys.argv[3], 16)
for pc in range(s & ~3, e, 4):
    off = pc - base
    if off < 0 or off + 4 > len(data): continue
    w = struct.unpack('<I', data[off:off + 4])[0]
    print('%08x  %08x  %s' % (pc, w, disasm(decode(w, pc))))
