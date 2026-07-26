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

# The rival's team counters yours, so the starter chosen selects which rival
# variant the Route 103 node resolves to.
STARTERS = {
    "TREECKO":  ("SPECIES_TREECKO", 277, "TREECKO"),
    "TORCHIC":  ("SPECIES_TORCHIC", 255, "TORCHIC"),
    "MUDKIP":   ("SPECIES_MUDKIP", 258, "MUDKIP"),
}
STARTER_MOVES = {277: [33], 255: [33], 258: [33]}   # Tackle/Scratch stand-in
NAME_LEN = 12      # POKEMON_NAME_LENGTH in constants/global.h


def encode_name(text: str, charmap: dict[int, str]) -> bytes:
    """ASCII -> ROM character encoding, EOS terminated.

    Built by inverting the charmap actually used for decoding, so the two can
    never disagree about what a byte means.
    """
    rev = {}
    for byte, ch in charmap.items():
        rev.setdefault(ch, byte)
    out = bytearray()
    for ch in text[:NAME_LEN]:
        if ch not in rev:
            raise SystemExit(f"character {ch!r} cannot be written to a nickname")
        out.append(rev[ch])
    out.append(0xFF)
    # Padded to the full field so the payload always matches the ROM struct.
    return bytes(out.ljust(NAME_LEN + 1, b"\xff"))


# Padded well past the ROM struct on purpose. The ROM rejects a payload that is
# too short and ignores extra bytes, so over-sending is safe while a miscounted
# struct size is not -- and it was miscounted twice.
NICK_PAYLOAD = 32


def nickname_payload(slot: int, name: str, charmap) -> bytes:
    return (struct.pack("<B3x", slot) + encode_name(name, charmap)).ljust(
        NICK_PAYLOAD, b"\xff")


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


# struct HarnessPartyView: 2+2+2+1+1+13+1. The name is 13 bytes because
# POKEMON_NAME_LENGTH is 12, not 10.
PARTY_VIEW = 22
NICK_OFF, NICK_LEN = 8, 13
OFF_PARTY_V = 180    # after padding2


def decode_party(raw: bytes, species_names, charmap):
    """The party slice of a decision request, including nicknames (R5)."""
    out = []
    for i in range(6):
        off = OFF_PARTY_V + i * PARTY_VIEW
        sp, hp, mx, lv, legal = struct.unpack_from("<3HBB", raw, off)
        nick = decode_text(raw[off + NICK_OFF:off + NICK_OFF + NICK_LEN] + b"\xff",
                           charmap)
        out.append({"species": sp, "hp": hp, "maxhp": mx, "level": lv,
                    "legal": bool(legal),
                    "name": species_names.get(sp, f"#{sp}"),
                    "nick": nick[0] if nick else ""})
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
        self.starter = None
        self.team = []                       # [(nickname, species_name)]
        self.beaten = set()
        self.spent = {}                      # mapsec -> outcome (R2/R3)
        self.party_slots = 1                 # starter occupies slot 0

    # -- table --------------------------------------------------------------
    def load_nodes(self, path):
        nodes = yaml.safe_load(path.read_text())
        problems, ids = [], {n["id"] for n in nodes}
        for nd in nodes:
            if nd["kind"] == "trainer_battle":
                key = nd["trainer"].replace("{starter}", self.starter)
                nd["trainer_resolved"] = key
                if key not in self.trainers:
                    problems.append(f"{nd['id']}: unknown trainer {key}")
                elif key not in self.rosters:
                    problems.append(f"{nd['id']}: {key} has no roster")
            else:
                if nd["mapsec"] not in self.mapsecs:
                    problems.append(f"{nd['id']}: unknown mapsec {nd['mapsec']}")
                if nd.get("unlocked_by") and nd["unlocked_by"] not in ids:
                    problems.append(f"{nd['id']}: unlocked_by names no node "
                                    f"({nd['unlocked_by']})")
                for d in nd["draws"]:
                    if d["map"] not in self.maps:
                        problems.append(f"{nd['id']}: unknown map {d['map']}")
                    if d["method"] not in METHODS:
                        problems.append(f"{nd['id']}: unknown method {d['method']}")
        if problems:
            # §7.6: reject the table rather than skip a row. A silently dropped
            # mandatory fight would not surface until much later.
            raise SystemExit("campaign table rejected:\n  " + "\n  ".join(problems))
        return nodes

    # -- availability -------------------------------------------------------
    def next_fight(self, nodes):
        """Exactly one fight is ever pending, by design (see nodes.yaml)."""
        chain = sorted((n for n in nodes if n["kind"] == "trainer_battle"),
                       key=lambda n: n["order"])
        for nd in chain:
            if nd["id"] not in self.beaten:
                return nd
        return None

    def open_locations(self, nodes):
        """Unlocked, unspent locations. Locations are gated only by fights, so
        crossing a route without spending its encounter stays legal (R2/R3)."""
        out = []
        for nd in nodes:
            if nd["kind"] != "location" or nd["mapsec"] in self.spent:
                continue
            need = nd.get("unlocked_by")
            if need is None or need in self.beaten:
                out.append(nd)
        return out

    def draws(self, nd):
        avail, blocked = [], []
        for d in nd["draws"]:
            miss = [c for c in d.get("requires", []) if c not in self.capabilities]
            (blocked.append((d, miss)) if miss else avail.append(d))
        return avail, blocked

    # -- presentation -------------------------------------------------------
    def show_text(self, raw):
        for line in decode_text(raw, self.charmap):
            print(f"   | {line}")

    def show_state(self):
        print("\n  Your team:")
        for nick, sp in self.team:
            print(f"    {nick:<12} ({sp})")
        if self.spent:
            print("  Locations spent:")
            for sec, res in self.spent.items():
                print(f"    {sec:<26} {res}")

    def describe_fight(self, nd):
        r = self.rosters[nd["trainer_resolved"]]
        lines = [f"FIGHT  {r['class']} {r['name']}  "
                 f"({nd.get('battle_kind', 'single')})"]
        for m in r["mons"]:
            item = f" @ {m['item']}" if m["item"] else ""
            mv = (", ".join(m["moves"]) if m["moves"]
                  else "(moveset not in roster data; from Gen 3 learnset)")
            lines.append(f"           {m['species']} Lv{m['level']}{item} — {mv}")
        return lines

    def describe_location(self, nd):
        avail, blocked = self.draws(nd)
        lines = [f"CATCH  {nd['mapsec']}"]
        for d in avail:
            lines.append(f"           {d['method']} on {d['map']}")
        for d, miss in blocked:
            lines.append(f"           {d['method']} locked (needs {'+'.join(miss)})")
        if not avail:
            lines.append("           no draw available — would yield nothing")
        return lines

    # -- decisions ----------------------------------------------------------
    def choose(self, raw, txt):
        self.show_text(txt)
        r = decode(raw, self.species, self.moves)
        menu = render(r)
        if self.args.auto:
            print(f" auto-> {menu[0][0]}")
            return menu[0][1]
        while True:
            pick = input(f" Choose [1-{len(menu)}] (q to quit): ").strip()
            if pick.lower() in ("q", "quit"):
                raise SystemExit("stopped")
            if pick.isdigit() and 1 <= int(pick) <= len(menu):
                print(f"  -> {menu[int(pick) - 1][0]}")
                return menu[int(pick) - 1][1]

    def ask_nickname(self, what, default):
        if self.args.auto:
            return default
        while True:
            name = input(f"  Name your {what} (R5 requires one): ").strip().upper()
            if name:
                return name[:NAME_LEN]
            print("    a nickname is required")

    # -- actions ------------------------------------------------------------
    def do_fight(self, sess, nd):
        kind = 0 if nd.get("battle_kind", "single") == "single" else 1
        out, txt = sess.run(S.HCMD_TRAINER_BATTLE,
                            struct.pack("<HHB3x", self.trainers[nd["trainer_resolved"]],
                                        0, kind),
                            on_decision=self.choose)
        self.show_text(txt)
        code = out[0] if out else 0
        print(f"\n  == {nd['id']}: {OUTCOMES.get(code, code)} ==")
        if code == 2:
            raise SystemExit("  whiteout — the run ends here (R9)")
        self.beaten.add(nd["id"])

    def do_location(self, sess, nd):
        avail, _ = self.draws(nd)
        if not avail:
            print(f"  {nd['mapsec']} has no available draw; nothing to spend.")
            return
        draw = avail[0]
        if len(avail) > 1 and not self.args.auto:
            print("\n  Which draw spends this location?")
            for i, d in enumerate(avail, 1):
                print(f"    {i}) {d['method']} on {d['map']}")
            while True:
                pk = input(f"  Choose [1-{len(avail)}]: ").strip()
                if pk.isdigit() and 1 <= int(pk) <= len(avail):
                    draw = avail[int(pk) - 1]
                    break

        group, num = self.maps[draw["map"]]
        sess.run(S.HCMD_WARP, struct.pack("<4B", group, num, 5, 5))
        out, txt = sess.run(S.HCMD_ROLL_ENCOUNTER,
                            struct.pack("<BB2x", METHODS[draw["method"]], 1),
                            on_decision=self.choose)
        self.show_text(txt)
        code = out[0] if out else 0
        # R3: faint, flee or a failed ball all spend the location just as a catch
        # does. Only a dupe would permit a reroll, and dupes are not modelled yet.
        self.spent[nd["mapsec"]] = OUTCOMES.get(code, str(code))
        print(f"\n  == {nd['id']}: {OUTCOMES.get(code, code)} ==")

        if code == 7:
            slot = self.party_slots
            self.party_slots += 1
            nick = self.ask_nickname("new catch", f"CAUGHT{slot}")
            sess.run(S.HCMD_SET_NICKNAME, nickname_payload(slot, nick, self.charmap))
            self.team.append((nick, nd["mapsec"]))
            print(f"  {nick} joins the team in slot {slot + 1}")

    # -- the loop -----------------------------------------------------------
    def pick_starter(self):
        keys = list(STARTERS)
        if self.args.auto:
            self.starter = "MUDKIP"
        else:
            print("\nChoose your starter — this also decides which team the rival "
                  "brings,\nsince their starter is picked to counter yours.")
            for i, k in enumerate(keys, 1):
                print(f"  {i}) {k}")
            while True:
                pk = input(f"Choose [1-{len(keys)}]: ").strip()
                if pk.isdigit() and 1 <= int(pk) <= len(keys):
                    self.starter = keys[int(pk) - 1]
                    break
        _, sp_id, label = STARTERS[self.starter]
        nick = self.ask_nickname(f"{label}", label)
        self.team.append((nick, label))
        return sp_id, nick

    def main(self):
        sp_id, starter_nick = self.pick_starter()
        nodes = self.load_nodes(ROOT / "campaign/nodes.yaml")
        print(f"\nCampaign: {len(nodes)} nodes, starter {self.starter}, "
              f"seed 0x{self.args.seed:08X}")

        party = build_party([(0x12345678, sp_id, 5, STARTER_MOVES[sp_id])])
        with S.Session(self.args.mgba, self.args.rom, self.args.symbols) as sess:
            sess.bootstrap(self.args.seed, party)
            sess.run(S.HCMD_SET_NICKNAME,
                     nickname_payload(0, starter_nick, self.charmap))

            while True:
                fight = self.next_fight(nodes)
                locs = self.open_locations(nodes)
                if fight is None and not locs:
                    break

                options = []
                print(f"\n{'#' * 68}")
                print("# What now?")
                print(f"{'#' * 68}")
                if fight is not None:
                    options.append(("fight", fight))
                    for i, line in enumerate(self.describe_fight(fight)):
                        print(f"  {len(options)}) {line}" if i == 0 else f"     {line}")
                for nd in locs:
                    options.append(("loc", nd))
                    for i, line in enumerate(self.describe_location(nd)):
                        print(f"  {len(options)}) {line}" if i == 0 else f"     {line}")
                self.show_state()

                if self.args.auto:
                    kind, nd = options[0]
                else:
                    while True:
                        pk = input(f"\n  Choose [1-{len(options)}], (h)eal, (q)uit: ")\
                            .strip().lower()
                        if pk in ("h", "heal"):
                            sess.run(S.HCMD_HEAL)
                            print("  party healed (HP, status and PP)")
                            continue
                        if pk in ("q", "quit"):
                            raise SystemExit("stopped")
                        if pk.isdigit() and 1 <= int(pk) <= len(options):
                            kind, nd = options[int(pk) - 1]
                            break

                (self.do_fight if kind == "fight" else self.do_location)(sess, nd)

            print(f"\n{'=' * 68}")
            print(" Slice complete.")
            self.show_state()
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mgba", type=pathlib.Path,
                    default=pathlib.Path("/root/mgba-src/build/mgba-headless"))
    ap.add_argument("--rom", type=pathlib.Path, default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("--symbols", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=0xC0FFEE01)
    ap.add_argument("--auto", action="store_true",
                    help="take the first option everywhere, for smoke testing")
    return Runner(ap.parse_args()).main()


if __name__ == "__main__":
    sys.exit(main())
