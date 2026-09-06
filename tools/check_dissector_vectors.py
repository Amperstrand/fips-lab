#!/usr/bin/env python3
"""Wireshark dissector vs canonical cross-vectors check (P5 cross-vectors).

Canonical source: Amperstrand/fips-protocol-defs-mvp
vectors/fips-v0-cross-vectors.json (embedded below with provenance). The
dissector's link_msg_names table and FMP constants are parsed from the Lua
source and compared — drift between the dissector and the canonical wire
contract fails here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DISSECTOR = Path(__file__).resolve().parents[1] / 'tools' / 'fips_dissector.lua'

CANONICAL_LINK_TYPES = {
    0x00: 'SessionDatagram',
    0x01: 'SenderReport',
    0x02: 'ReceiverReport',
    0x10: 'TreeAnnounce',
    0x20: 'FilterAnnounce',
    0x30: 'LookupRequest',
    0x31: 'LookupResponse',
    0x50: 'Disconnect',
    0x51: 'Heartbeat',
}
CANONICAL_PHASES = {0x0: 'ESTABLISHED', 0x1: 'MSG1', 0x2: 'MSG2'}
CANONICAL_CONSTS = {'COMMON_PREFIX_SIZE': 4, 'ESTABLISHED_HEADER_SIZE': 16, 'FMP_VERSION': 0}


def parse_lua_table(source: str, name: str) -> dict[int, str]:
    m = re.search(rf'{name}\s*=\s*\{{(.*?)\}}', source, re.DOTALL)
    if not m:
        raise SystemExit(f'dissector: table {name} not found')
    out: dict[int, str] = {}
    for key, val in re.findall(r'\[(0x[0-9A-Fa-f]+)\]\s*=\s*"([^"]+)"', m.group(1)):
        out[int(key, 16)] = val.split(' (')[0]
    return out


def main() -> int:
    src = DISSECTOR.read_text()
    failures = []

    link = parse_lua_table(src, 'link_msg_names')
    if link != CANONICAL_LINK_TYPES:
        failures.append(f'link_msg_names drifted: dissector={link} canonical={CANONICAL_LINK_TYPES}')

    phases = parse_lua_table(src, 'phase_names')
    if phases != CANONICAL_PHASES:
        failures.append(f'phase_names drifted: dissector={phases} canonical={CANONICAL_PHASES}')

    for const, expected in CANONICAL_CONSTS.items():
        m = re.search(rf'local\s+{const}\s*=\s*(\d+)', src)
        if not m:
            failures.append(f'constant {const} not found in dissector')
        elif int(m.group(1)) != expected:
            failures.append(f'{const}: dissector={m.group(1)} canonical={expected}')

    if failures:
        print('DISSECTOR DRIFT:')
        for f in failures:
            print(f'  - {f}')
        print('Regenerate the dissector from the profile (defs-mvp) and update')
        print('vectors/fips-v0-cross-vectors.json in the same change.')
        return 1
    print('dissector agrees with canonical cross-vectors '
          f'({len(CANONICAL_LINK_TYPES)} link types, {len(CANONICAL_CONSTS)} constants)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
