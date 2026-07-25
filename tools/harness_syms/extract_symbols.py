#!/usr/bin/env python3
"""Emit harness_symbols.json from the linker map.

Spec §4.2: bridge.lua and the Python harness resolve every ROM address through
this file. Hardcoded addresses are forbidden — they survive a rebuild that moves
the symbol and then read the wrong memory silently.

Usage:
    tools/harness_syms/extract_symbols.py pokeemerald.map [-o harness_symbols.json]

The output records the ROM build's sha1 alongside the symbols. Consumers MUST
check it against the ROM they are actually running (see §8.3, which applies the
same rule to the documentation pack).
"""

import argparse
import hashlib
import json
import pathlib
import re
import sys

# GNU ld map symbol line: leading whitespace, address, whitespace, identifier.
# Section lines (".sbss 0x... 0x... file.o") carry a size field and are skipped
# by requiring the identifier to be the last token on the line.
SYMBOL_RE = re.compile(r"^\s+0x([0-9a-fA-F]{8,16})\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")

# Symbols the harness needs regardless of what else is exported. Absence of any
# of these means the ROM was built without the harness, or a symbol was renamed
# upstream — either way the harness must not start. Extend as src/harness/ lands.
REQUIRED = [
    "gParties",
    "gPartiesCount",
]

# Memory regions, for sanity-checking that a symbol lives where we expect.
REGIONS = {
    "ewram": (0x02000000, 0x02040000),
    "iwram": (0x03000000, 0x03008000),
    "rom":   (0x08000000, 0x0A000000),
}


def region_of(addr: int) -> str | None:
    for name, (lo, hi) in REGIONS.items():
        if lo <= addr < hi:
            return name
    return None


def parse_map(path: pathlib.Path) -> dict[str, int]:
    symbols: dict[str, int] = {}
    duplicates: set[str] = set()

    for line in path.read_text(errors="replace").splitlines():
        m = SYMBOL_RE.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        name = m.group(2)
        if region_of(addr) is None:
            continue  # discard load addresses and link-time artefacts
        if name in symbols and symbols[name] != addr:
            duplicates.add(name)
        symbols[name] = addr

    # A duplicated symbol means we cannot say which address is live. Dropping it
    # is safer than picking one: a missing key fails loudly at lookup, a wrong
    # address does not.
    for name in duplicates:
        symbols.pop(name, None)
    if duplicates:
        print(
            f"warning: {len(duplicates)} symbol(s) had conflicting addresses and "
            f"were dropped: {', '.join(sorted(duplicates)[:5])}"
            + ("..." if len(duplicates) > 5 else ""),
            file=sys.stderr,
        )

    return symbols


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map", type=pathlib.Path, nargs="?",
                    default=pathlib.Path("pokeemerald.map"))
    ap.add_argument("-r", "--rom", type=pathlib.Path,
                    default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("-o", "--output", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--require", action="append", default=[],
                    help="additional symbol that must be present")
    args = ap.parse_args()

    if not args.map.exists():
        print(f"error: {args.map} not found — build the ROM first", file=sys.stderr)
        return 1

    symbols = parse_map(args.map)
    if not symbols:
        print(f"error: no symbols parsed from {args.map}", file=sys.stderr)
        return 1

    missing = [s for s in REQUIRED + args.require if s not in symbols]
    if missing:
        print(f"error: required symbol(s) absent from map: {', '.join(missing)}",
              file=sys.stderr)
        return 1

    rom_sha1 = None
    if args.rom.exists():
        rom_sha1 = hashlib.sha1(args.rom.read_bytes()).hexdigest()
    else:
        print(f"warning: {args.rom} not found; rom_sha1 will be null",
              file=sys.stderr)

    doc = {
        "rom_sha1": rom_sha1,
        "map": str(args.map),
        "regions": {k: {"start": v[0], "end": v[1]} for k, v in REGIONS.items()},
        "symbols": {n: symbols[n] for n in sorted(symbols)},
    }
    args.output.write_text(json.dumps(doc, indent=2) + "\n")

    by_region: dict[str, int] = {}
    for addr in symbols.values():
        r = region_of(addr)
        if r:
            by_region[r] = by_region.get(r, 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_region.items()))
    print(f"{args.output}: {len(symbols)} symbols ({breakdown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
