#!/usr/bin/env python3
"""Pull the campaign's raw material out of the decomp.

Every fact here comes from the game's own data rather than from a guide: trainers
from the map scripts that start them, encounters from wild_encounters.json, items
from the object and bg events that hold them. Guides disagree with each other and
with the ROM; this cannot.

What it deliberately does NOT produce is the *order*. Progression gating lives in
story flags and script logic, so route order and which fight opens which route is
supplied by hand in campaign/ROUTE.txt and only checked against this.

    tools/campaign_extract.py > campaign/extracted.json
"""

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAPS = ROOT / "data/maps"

# trainerbattle_single TRAINER_X, ... / _double / _rematch etc.
TRAINER_RE = re.compile(r"^\s*trainerbattle\w*\s+(TRAINER_[A-Z0-9_]+)", re.M)


def trainers_for(map_dir: pathlib.Path) -> list[str]:
    """Trainers whose battle is started by this map's scripts.

    Rematches are dropped: they reuse the same trainer id, and a nuzlocke route
    wants each mandatory fight once.
    """
    script = map_dir / "scripts.inc"
    if not script.exists():
        return []
    txt = script.read_text(errors="replace")
    seen, out = set(), []
    # Multi battles are started by their own command, not trainerbattle, so they
    # are invisible to the loop below. There is exactly one in the game -- Maxie
    # and Tabitha at the Space Center, fought 2-vs-2 with Steven as partner --
    # and it is mandatory, so missing it would leave a hole in the route.
    for m in re.finditer(r"^\s*multi_(\w+)\s+(TRAINER_[A-Z0-9_]+)\s*,[^,]+,\s*"
                         r"(TRAINER_[A-Z0-9_]+)\s*,[^,]+,\s*(\w+)", txt, re.M):
        for tid in (m.group(2), m.group(3)):
            if tid not in seen:
                seen.add(tid)
                out.append({"id": tid, "kind": f"multi_{m.group(1)}",
                            "partner": m.group(4)})
    for m in re.finditer(r"^\s*trainerbattle(\w*)\s+(TRAINER_[A-Z0-9_]+)", txt, re.M):
        kind, tid = m.group(1), m.group(2)
        if "rematch" in kind:
            continue
        if tid not in seen:
            seen.add(tid)
            # Doubles are recorded because the harness needs the battle kind to
            # set the battle up, and it cannot be inferred from the roster.
            out.append({"id": tid,
                        "kind": "double" if "double" in kind else "single"})
    return out


def items_for(mj: dict, map_dir: pathlib.Path) -> dict:
    balls = [o["trainer_sight_or_berry_tree_id"] for o in mj.get("object_events", [])
             if o.get("script") == "Common_EventScript_FindItem"]
    hidden = [b["item"] for b in mj.get("bg_events", [])
              if b.get("type") == "hidden_item"]

    # Gifts come from dialogue, not from an object with an item on it, so they
    # are invisible to the two lists above. This is where every HM, every gym TM
    # and the rods live -- the items that actually decide what a run can do.
    gift, script = [], map_dir / "scripts.inc"
    if script.exists():
        seen = set()
        for m in re.finditer(r"^\s*giveitem\s+(ITEM_[A-Z0-9_]+)",
                             script.read_text(errors="replace"), re.M):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                gift.append(m.group(1))
    return {"ball": balls, "hidden": hidden, "gift": gift}


def encounters() -> dict:
    """map -> {method: [species...]}, deduped and in table order."""
    data = json.loads((ROOT / "src/data/wild_encounters.json").read_text())
    out = {}
    for group in data.get("wild_encounter_groups", []):
        if not group.get("for_maps"):
            continue
        for enc in group.get("encounters", []):
            m = out.setdefault(enc["map"], {})
            for field in ("land_mons", "water_mons", "rock_smash_mons", "fishing_mons"):
                blk = enc.get(field)
                if not blk:
                    continue
                mons, seen = [], set()
                for e in blk["mons"]:
                    if e["species"] not in seen:
                        seen.add(e["species"])
                        mons.append({"species": e["species"],
                                     "min": e["min_level"], "max": e["max_level"]})
                m[field] = mons
    return out


def main() -> int:
    enc = encounters()
    out = {}
    for map_dir in sorted(MAPS.iterdir()):
        mj_path = map_dir / "map.json"
        if not mj_path.is_dir() and mj_path.exists():
            mj = json.loads(mj_path.read_text())
        else:
            continue
        name = mj.get("id", "").replace("MAP_", "")
        rec = {
            "map": mj.get("id"),
            "region_section": mj.get("region_map_section"),
            "trainers": trainers_for(map_dir),
            "items": items_for(mj, map_dir),
            "encounters": enc.get(mj.get("id"), {}),
            "connections": [c.get("map") for c in (mj.get("connections") or [])],
        }
        if rec["trainers"] or rec["encounters"] or any(rec["items"].values()):
            out[name] = rec

    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
