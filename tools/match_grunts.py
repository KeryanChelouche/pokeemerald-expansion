#!/usr/bin/env python3
"""Identify which specific grunts are mandatory, by matching teams.

The reference sheet names every mandatory fight but calls them all "Team Aqua
Grunt", so a hideout with sixteen grunts and seven mandatory ones cannot be
resolved by name. It does give each team, and the decomp gives every trainer's
party -- so the team is the key.

Prints the id whose party matches each sheet entry, and says so plainly when a
team matches more than one trainer or none.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (label, [(SPECIES, level), ...]) exactly as the sheet lists them.
SHEET = {
    "Weather Institute": [
        ("1F  grunt",        [("CARVANHA", 28)], "WEATHER_INSTITUTE_1F"),
        ("2F  grunt [2]",    [("POOCHYENA", 27), ("CARVANHA", 27)], "WEATHER_INSTITUTE_2F"),
        ("2F  grunt [2]",    [("ZUBAT", 27), ("POOCHYENA", 27)], "WEATHER_INSTITUTE_2F"),
    ],
    "Mt. Pyre": [
        ("outside grunt",    [("CARVANHA", 32)], "MT_PYRE"),
        ("outside grunt",    [("ZUBAT", 32)], "MT_PYRE"),
        ("outside grunt [2]", [("POOCHYENA", 30), ("CARVANHA", 30)], "MT_PYRE"),
        ("outside grunt [2]", [("WAILMER", 30), ("ZUBAT", 30)], "MT_PYRE"),
    ],
    "Magma Hideout": [
        ("1F grunt",         [("POOCHYENA", 29)], "MAGMA_HIDEOUT_1F"),
        ("2F grunt",         [("NUMEL", 29)], "MAGMA_HIDEOUT_2F"),
        ("3F grunt",         [("ZUBAT", 29)], "MAGMA_HIDEOUT_3F"),
        ("3F grunt",         [("BALTOY", 29)], "MAGMA_HIDEOUT_3F"),
        ("4F grunt",         [("BALTOY", 29)], "MAGMA_HIDEOUT_4F"),
        ("4F grunt [2]",     [("NUMEL", 29)], "MAGMA_HIDEOUT_4F"),
        ("4F grunt [2]",     [("ZUBAT", 29)], "MAGMA_HIDEOUT_4F"),
    ],
    "Aqua Hideout": [
        ("B1F grunt [2]",    [("ZUBAT", 31), ("CARVANHA", 31)], "AQUA_HIDEOUT_B1F"),
        ("B1F grunt [2]",    [("POOCHYENA", 31), ("ZUBAT", 31)], "AQUA_HIDEOUT_B1F"),
        ("B2F grunt [2]",    [("ZUBAT", 32)], "AQUA_HIDEOUT_B2F"),
        ("B2F grunt [2]",    [("CARVANHA", 32)], "AQUA_HIDEOUT_B2F"),
    ],
    "Space Center": [
        ("1F grunt",         [("BALTOY", 32)], "SPACE_CENTER_1F"),
        ("1F grunt",         [("MIGHTYENA", 26), ("MIGHTYENA", 28), ("NUMEL", 30)], "SPACE_CENTER_1F"),
        ("2F grunt",         [("ZUBAT", 32)], "SPACE_CENTER_2F"),
        ("2F grunt",         [("MIGHTYENA", 32)], "SPACE_CENTER_2F"),
        ("2F grunt",         [("BALTOY", 32)], "SPACE_CENTER_2F"),
    ],
}

# Only these id families are candidates for each sheet section.
CANDIDATES = {
    "Weather Institute": "GRUNT_WEATHER_INST_",
    "Mt. Pyre":          "GRUNT_MT_PYRE_",
    "Magma Hideout":     "GRUNT_MAGMA_HIDEOUT_",
    "Aqua Hideout":      "GRUNT_AQUA_HIDEOUT_",
    "Space Center":      "GRUNT_SPACE_CENTER_",
}


def parties() -> dict[str, list[tuple[str, int]]]:
    """TRAINER_X -> [(SPECIES, level), ...] for the normal difficulty table."""
    txt = (ROOT / "src/data/trainers.h").read_text(errors="replace")
    out = {}
    blocks = re.split(r"\[DIFFICULTY_NORMAL\]\[(TRAINER_[A-Z0-9_]+)\]\s*=", txt)
    for i in range(1, len(blocks), 2):
        tid, body = blocks[i], blocks[i + 1]
        mons = []
        for m in re.finditer(r"\.species\s*=\s*SPECIES_([A-Z0-9_]+).*?\.lvl\s*=\s*(\d+)",
                             body, re.S):
            mons.append((m.group(1), int(m.group(2))))
        out[tid] = mons
    return out


def where() -> dict[str, list[str]]:
    """TRAINER_X -> the maps that start its battle."""
    import json
    data = json.loads((ROOT / "campaign/extracted.json").read_text())
    out: dict[str, list[str]] = {}
    for mp, rec in data.items():
        for t in rec["trainers"]:
            out.setdefault(t["id"], []).append(mp)
    return out


def main() -> int:
    party = parties()
    loc = where()
    print(f"parsed {len(party)} trainer parties\n")

    for section, rows in SHEET.items():
        prefix = "TRAINER_" + CANDIDATES[section]
        pool = {tid: mons for tid, mons in party.items() if tid.startswith(prefix)}
        print("=" * 70)
        print(f"{section}   ({len(rows)} mandatory of {len(pool)} present)")
        print("=" * 70)

        used = set()
        for label, team, floor in rows:
            # Team AND floor. Grunts with identical teams are common, so the
            # team alone leaves several candidates; the floor separates them.
            hits = [tid for tid, mons in pool.items()
                    if mons == team and tid not in used
                    and any(floor in m for m in loc.get(tid, []))]
            if len(hits) == 1:
                used.add(hits[0])
                print(f"  {label:18} -> {hits[0].replace('TRAINER_', '')}")
            elif not hits:
                # Either the sheet's team is wrong, or every match is taken --
                # both worth seeing rather than silently picking something.
                allhits = [t for t, m in pool.items() if m == team]
                print(f"  {label:18} -> NO MATCH  {team}"
                      + (f"  (all {len(allhits)} already used)" if allhits else ""))
            else:
                used.add(hits[0])
                print(f"  {label:18} -> {hits[0].replace('TRAINER_', '')}  "
                      f"(AMBIGUOUS, {len(hits)} identical teams: "
                      f"{', '.join(h.replace('TRAINER_', '') for h in hits)})")

        left = sorted(set(pool) - used)
        if left:
            print(f"  not mandatory: {', '.join(t.replace('TRAINER_', '') for t in left)}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
