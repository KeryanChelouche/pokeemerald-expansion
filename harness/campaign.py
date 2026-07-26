#!/usr/bin/env python3
"""Play a campaign slice by hand, standing in for the agent.

Walks campaign/nodes.yaml in order. Between nodes it stops at a checkpoint: your
team, what is coming next, and -- for a battle -- the opponent's documented
roster. Those are the §9.8 "between" decisions, and they are where a nuzlocke is
actually decided; the battles mostly follow from them.

Everything shown is what the agent would receive. Nothing is available here that
would not be in its context.

Usage:
    harness/campaign.py                       # walk the whole slice
    harness/campaign.py --seed 0xC0FFEE01     # pin the RNG
    harness/campaign.py --auto                # first legal action, no prompts

Requires harness_symbols.json.
"""

import argparse
import pathlib
import re
import struct
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import session as S                                    # noqa: E402
from play import (BADGE_FLAGS, HACT_BALL, HACT_MOVE, HACT_SWITCH,  # noqa: E402
                  HTARGET_DEFAULT, ITEM_POKE_BALL, METHODS, SPEC_FMT,
                  decode, decode_text, load_charmap, load_names, render)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Capabilities held at the start of this slice. Route 102/103/104 list surf and
# rod draws, and §7.2 requires them filtered out until the capability is granted.
START_CAPABILITIES: set[str] = set()

OUTCOMES = {1: "won", 2: "lost", 3: "drew", 4: "ran", 6: "it fled", 7: "caught"}


def load_constants(path: pathlib.Path, prefix: str) -> dict[str, int]:
    """NAME -> value for `#define NAME v` and `NAME = v,` enum styles."""
    out, counter = {}, 0
    txt = path.read_text(errors="replace")
    for m in re.finditer(rf"^\s*#define\s+({prefix}[A-Z0-9_]+)\s+(\d+)", txt, re.M):
        out[m.group(1)] = int(m.group(2))
    for m in re.finditer(rf"^\s*({prefix}[A-Z0-9_]+)\s*(?:=\s*(\d+))?\s*,", txt, re.M):
        if m.group(2) is not None:
            counter = int(m.group(2))
        out.setdefault(m.group(1), counter)
        counter += 1
    return out


def load_maps(path: pathlib.Path) -> dict[str, tuple[int, int]]:
    """MAP_X -> (group, num), from `MAP_X = (num | (group << 8))`."""
    out = {}
    for m in re.finditer(r"^\s*(MAP_[A-Z0-9_]+)\s*=\s*\((\d+)\s*\|\s*\((\d+)\s*<<\s*8\)\)",
                         path.read_text(errors="replace"), re.M):
        out[m.group(1)] = (int(m.group(3)), int(m.group(2)))
    return out


def parse_rosters(path: pathlib.Path) -> dict[str, dict]:
    """TRAINER_X -> {name, class, mons:[{species, level, item, moves}]}.

    Movesets are only present for about a quarter of entries; the rest are
    filled at runtime from the level-up learnset (GiveMonInitialMoveset). Those
    are reported as implicit rather than guessed at, so a plan is never built on
    a moveset this file does not actually state.
    """
    out, blocks = {}, re.split(r"^=== (TRAINER_[A-Z0-9_]+) ===$",
                               path.read_text(errors="replace"), flags=re.M)[1:]
    for i in range(0, len(blocks), 2):
        key, body = blocks[i], blocks[i + 1]
        rec = {"name": "", "class": "", "mons": []}
        for line in body.splitlines():
            if line.startswith("Name: "):
                rec["name"] = line[6:].strip()
            elif line.startswith("Class: "):
                rec["class"] = line[7:].strip()
        for chunk in re.split(r"\n\s*\n", body):
            lvl = re.search(r"^Level: (\d+)", chunk, re.M)
            if not lvl:
                continue
            head = chunk.strip().splitlines()[0]
            species, _, item = head.partition(" @ ")
            rec["mons"].append({
                "species": species.strip(), "level": int(lvl.group(1)),
                "item": item.strip() or None,
                "moves": re.findall(r"^- (.+)$", chunk, re.M),
            })
        out[key] = rec
    return out


def build_party(mons) -> bytes:
    out = struct.pack("<I", len(mons))
    for pers, sp, lv, moves in mons:
        mv = (list(moves) + [0, 0, 0, 0])[:4]
        out += struct.pack(SPEC_FMT, pers, sp, 0, *mv, lv, 0,
                           *([31] * 6), *([0] * 6), bytes([0xFF] * 11))
    return out


class Runner:
    def __init__(self, args):
        self.args = args
        self.charmap = load_charmap(ROOT / "charmap.txt")
        self.species = load_names(ROOT / "include/constants/species.h", "SPECIES_")
        self.moves = load_names(ROOT / "include/constants/moves.h", "MOVE_")
        self.trainers = load_constants(ROOT / "include/constants/opponents.h", "TRAINER_")
        self.mapsecs = load_constants(ROOT / "include/constants/region_map_sections.h",
                                      "MAPSEC_")
        self.maps = load_maps(ROOT / "include/constants/map_groups.h")
        self.rosters = parse_rosters(ROOT / "src/data/trainers.party")
        self.capabilities = set(START_CAPABILITIES)
        self.caught: list[str] = []
        self.spent: dict[str, str] = {}     # mapsec -> outcome (R2/R3)

    # -- validation ---------------------------------------------------------
    def load_nodes(self, path: pathlib.Path) -> list[dict]:
        nodes = yaml.safe_load(path.read_text())
        problems = []
        for nd in nodes:
            if nd["kind"] == "trainer_battle":
                if nd["trainer"] not in self.trainers:
                    problems.append(f"{nd['id']}: unknown trainer {nd['trainer']}")
                if nd["trainer"] not in self.rosters:
                    problems.append(f"{nd['id']}: {nd['trainer']} has no roster")
            else:
                if nd["mapsec"] not in self.mapsecs:
                    problems.append(f"{nd['id']}: unknown mapsec {nd['mapsec']}")
                for d in nd["draws"]:
                    if d["map"] not in self.maps:
                        problems.append(f"{nd['id']}: unknown map {d['map']}")
                    if d["method"] not in METHODS:
                        problems.append(f"{nd['id']}: unknown method {d['method']}")
        if problems:
            # §7.6: fail loudly on an unknown constant rather than skipping the
            # row, which would silently drop a mandatory fight from the run.
            raise SystemExit("campaign table rejected:\n  " + "\n  ".join(problems))
        return nodes

    # -- presentation -------------------------------------------------------
    def show_text(self, raw: bytes) -> None:
        for line in decode_text(raw, self.charmap):
            print(f"   | {line}")

    def preview(self, nd: dict) -> None:
        print(f"\n{'#' * 68}")
        print(f"# NEXT: {nd['id']}")
        print(f"{'#' * 68}")
        if nd["kind"] == "trainer_battle":
            r = self.rosters[nd["trainer"]]
            print(f"  {r['class']} {r['name']}  ({nd['trainer']}, "
                  f"{nd.get('battle_kind', 'single')})")
            for m in r["mons"]:
                item = f" @ {m['item']}" if m["item"] else ""
                if m["moves"]:
                    mv = ", ".join(m["moves"])
                else:
                    # Stated, not guessed: see parse_rosters.
                    mv = "(moveset not in roster data; filled from Gen 3 learnset)"
                print(f"    - {m['species']} Lv{m['level']}{item}")
                print(f"        {mv}")
        else:
            print(f"  Location {nd['mapsec']}")
            avail, blocked = self.draws(nd)
            for d in avail:
                print(f"    available : {d['method']:<10} on {d['map']}")
            for d, need in blocked:
                print(f"    locked    : {d['method']:<10} needs {'+'.join(need)}")
            if not avail:
                print("    -> no draw available here; this location yields nothing")

    def draws(self, nd: dict):
        avail, blocked = [], []
        for d in nd["draws"]:
            need = [c for c in d.get("requires", []) if c not in self.capabilities]
            (blocked.append((d, need)) if need else avail.append(d))
        return avail, blocked

    def show_party(self, sess) -> None:
        print("\n  Your team:")
        if not self.caught:
            print("    (starter only)")
        for i, nm in enumerate(self.caught):
            print(f"    {i + 1}. {nm}")

    # -- decisions ----------------------------------------------------------
    def choose(self, raw: bytes, txt: bytes):
        self.show_text(txt)
        r = decode(raw, self.species, self.moves)
        menu = render(r)
        if self.args.auto:
            label, act = menu[0]
            print(f" auto-> {label}")
            return act
        while True:
            pick = input(f" Choose [1-{len(menu)}] (q to quit): ").strip()
            if pick.lower() in ("q", "quit"):
                raise SystemExit("stopped")
            if pick.isdigit() and 1 <= int(pick) <= len(menu):
                label, act = menu[int(pick) - 1]
                print(f"  -> {label}")
                return act

    def checkpoint(self, sess, nd: dict) -> None:
        self.preview(nd)
        self.show_party(sess)
        if self.args.auto:
            return
        while True:
            pick = input("\n  [enter] continue, (h)eal, (q)uit: ").strip().lower()
            if pick in ("", "c", "continue"):
                return
            if pick in ("h", "heal"):
                sess.run(S.HCMD_HEAL)
                print("  party healed (HP, status and PP)")
            elif pick in ("q", "quit"):
                raise SystemExit("stopped")

    # -- the walk -----------------------------------------------------------
    def run_node(self, sess, nd: dict) -> None:
        if nd["kind"] == "trainer_battle":
            kind = 0 if nd.get("battle_kind", "single") == "single" else 1
            out, txt = sess.run(
                S.HCMD_TRAINER_BATTLE,
                struct.pack("<HHB3x", self.trainers[nd["trainer"]], 0, kind),
                on_decision=self.choose)
            self.show_text(txt)
            code = out[0] if out else 0
            print(f"\n  == {nd['id']}: {OUTCOMES.get(code, code)} ==")
            if code == 2:
                # R9: no legal Pokemon left is the end of the run, not a retry.
                raise SystemExit("  whiteout — the run ends here (R9)")
            return

        avail, _ = self.draws(nd)
        if not avail:
            print(f"\n  == {nd['id']}: skipped, no draw available ==")
            return

        if len(avail) == 1 or self.args.auto:
            draw = avail[0]
        else:
            print("\n  Which draw spends this location?")
            for i, d in enumerate(avail, 1):
                print(f"    {i}) {d['method']} on {d['map']}")
            while True:
                p = input(f"  Choose [1-{len(avail)}]: ").strip()
                if p.isdigit() and 1 <= int(p) <= len(avail):
                    draw = avail[int(p) - 1]
                    break

        group, num = self.maps[draw["map"]]
        sess.run(S.HCMD_WARP, struct.pack("<4B", group, num, 5, 5))
        out, txt = sess.run(
            S.HCMD_ROLL_ENCOUNTER,
            struct.pack("<BB2x", METHODS[draw["method"]], 1),
            on_decision=self.choose)
        self.show_text(txt)
        code = out[0] if out else 0
        self.spent[nd["mapsec"]] = OUTCOMES.get(code, str(code))
        print(f"\n  == {nd['id']}: {OUTCOMES.get(code, code)} ==")
        if code == 7:
            self.caught.append(nd["mapsec"])

    def main(self) -> int:
        nodes = self.load_nodes(ROOT / "campaign/nodes.yaml")
        print(f"Campaign: {len(nodes)} nodes, seed 0x{self.args.seed:08X}")

        party = build_party([(0x12345678, 259, 10, [33])])   # Marshtomp, Tackle
        with S.Session(self.args.mgba, self.args.rom, self.args.symbols) as sess:
            sess.bootstrap(self.args.seed, party)
            for nd in nodes:
                self.checkpoint(sess, nd)
                self.run_node(sess, nd)

            print(f"\n{'=' * 68}")
            print(f" Slice complete. Caught at: "
                  f"{', '.join(self.caught) if self.caught else 'nothing'}")
            for sec, outcome in self.spent.items():
                print(f"   {sec}: {outcome}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mgba", type=pathlib.Path,
                    default=pathlib.Path("/root/mgba-src/build/mgba-headless"))
    ap.add_argument("--rom", type=pathlib.Path, default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("--symbols", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=0xC0FFEE01)
    ap.add_argument("--auto", action="store_true",
                    help="take the first legal action everywhere, for smoke testing")
    return Runner(ap.parse_args()).main()


if __name__ == "__main__":
    sys.exit(main())
