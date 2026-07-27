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
import json
import pathlib
import shutil
import subprocess
import time
import re
import struct
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import session as S                                    # noqa: E402
from ledger import Ledger                              # noqa: E402
from play import (BADGE_FLAGS, HACT_BALL, HACT_MOVE, HACT_SWITCH,  # noqa: E402
                  HTARGET_DEFAULT, ITEM_POKE_BALL, METHODS, SPEC_FMT,
                  decode, decode_text, load_charmap, load_names, render)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Capabilities held at the start of this slice. Route 102/103/104 list surf and
# rod draws, and §7.2 requires them filtered out until the capability is granted.
START_CAPABILITIES: set[str] = set()

HERR_NOT_ELIGIBLE = 8      # enum HarnessError in include/harness.h

OUTCOMES = {1: "won", 2: "lost", 3: "drew", 4: "ran", 6: "it fled", 7: "caught"}

# The rival's team counters yours, so the starter chosen selects which rival
# variant the Route 103 node resolves to.
STARTERS = {
    "TREECKO":  ("SPECIES_TREECKO", 277, "TREECKO"),
    "TORCHIC":  ("SPECIES_TORCHIC", 255, "TORCHIC"),
    "MUDKIP":   ("SPECIES_MUDKIP", 258, "MUDKIP"),
}
STARTER_MOVES = {277: [33], 255: [33], 258: [33]}   # Tackle/Scratch stand-in
NAME_LEN = 12         # POKEMON_NAME_LENGTH
PLAYER_NAME_LEN = 7   # PLAYER_NAME_LENGTH
MALE, FEMALE = 0, 1


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


def load_families(root: pathlib.Path, species_names) -> dict[int, int]:
    """species id -> family id, from the decomp evolution table (R4).

    R4 counts the whole evolution family as a dupe, including members that are
    dead, so families are built by unioning every evolution edge rather than by
    looking only one step ahead.
    """
    ids = {name: sid for sid, name in
           ((s, "SPECIES_" + n.upper().replace(" ", "_")) for s, n in species_names.items())}
    parent: dict[int, int] = {}

    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    pat_species = re.compile(r"^\s*\[(SPECIES_[A-Z0-9_]+)\]\s*=", re.M)
    for path in sorted((root / "src/data/pokemon/species_info").glob("*.h")):
        txt = path.read_text(errors="replace")
        marks = [(m.start(), m.group(1)) for m in pat_species.finditer(txt)]
        for i, (pos, name) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(txt)
            body = txt[pos:end]
            m = re.search(r"\.evolutions\s*=\s*EVOLUTION\((.*?)\),?\s*\n", body, re.S)
            if not m or name not in ids:
                continue
            for tgt in re.findall(r"(SPECIES_[A-Z0-9_]+)", m.group(1)):
                if tgt in ids:
                    union(ids[name], ids[tgt])

    return {sid: find(sid) for sid in ids.values()}


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
PARTY_VIEW = 24
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
        self.families = load_families(ROOT, self.species)
        self.owned_families: set[int] = set()   # R4: includes dead members
        self.capabilities = set(START_CAPABILITIES)
        self.starter = None
        self.gender, self.player_name, self.rival = MALE, "?", "MAY"
        self.last_was_dupe = False
        self.last_caught_species = 0
        self.team = []                       # [(nickname, species_name)]
        self.beaten = set()
        self.spent = {}                      # mapsec -> outcome (R2/R3)
        self.graveyard = []                  # R1: append-only, never revived
        self.led = None                      # §11 ledger, opened in main()
        self.turn = 0                        # decisions within the current battle
        self.party_slots = 1                 # starter occupies slot 0

    # -- table --------------------------------------------------------------
    def load_nodes(self, path):
        nodes = yaml.safe_load(path.read_text())
        problems, ids = [], {n["id"] for n in nodes}
        for nd in nodes:
            if nd["kind"] == "trainer_battle":
                key = (nd["trainer"].replace("{starter}", self.starter)
                                    .replace("{rival}", self.rival))
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

    def level_cap(self, nodes):
        """R8: the cap is the next fight's highest level.

        With free ordering the agent chooses when to take a fight, so the cap
        tracks the pending fight rather than a fixed schedule position.
        """
        fight = self.next_fight(nodes)
        if fight is None:
            return None
        return max(m["level"] for m in self.rosters[fight["trainer_resolved"]]["mons"])

    def level_to_cap(self, sess, nodes):
        cap = self.level_cap(nodes)
        if cap is None:
            print("  no pending fight, so no cap to level to")
            return
        out, _ = sess.run(S.HCMD_DUMP_STATE)
        count = struct.unpack_from("<I", out, 0)[0]
        print(f"\n  Level cap is {cap} (next fight's strongest)")
        for i in range(count):
            off = 4 + i * PARTY_VIEW
            sp, hp, mx, lv, alive = struct.unpack_from("<3HBB", out, off)
            nick = decode_text(out[off + NICK_OFF:off + NICK_OFF + NICK_LEN] + b"\xff",
                               self.charmap)
            who = (nick[0] if nick else "?")
            if lv >= cap:
                print(f"    {who} already Lv{lv}")
                continue
            reply, _ = sess.run(S.HCMD_SET_LEVEL, struct.pack("<BB2x", i, cap))
            nl, np = struct.unpack_from("<2I", reply, 0)
            learned = [struct.unpack_from("<H", reply, 8 + k * 2)[0] for k in range(nl)]
            pending = [struct.unpack_from("<H", reply, 8 + 2 * 24 + k * 2)[0]
                       for k in range(np)]
            print(f"    {who} Lv{lv} -> Lv{cap}")
            if learned:
                # Already applied: the engine puts a new move in a free slot, so
                # there is nothing to decide.
                print(f"       learned: "
                      f"{', '.join(self.moves.get(m, f'#{m}') for m in learned)}")
            if pending:
                self.resolve_pending(sess, i, who, pending)

    def read_party(self, sess):
        """Live party from the ROM, rather than what Python believes it to be."""
        out, _ = sess.run(S.HCMD_DUMP_STATE)
        count = struct.unpack_from("<I", out, 0)[0]
        party = []
        for i in range(count):
            off = 4 + i * PARTY_VIEW
            sp, hp, mx, lv, alive = struct.unpack_from("<3HBB", out, off)
            nick = decode_text(out[off + NICK_OFF:off + NICK_OFF + NICK_LEN] + b"\xff",
                               self.charmap)
            party.append({"slot": i, "species": sp, "hp": hp, "maxhp": mx, "level": lv,
                          "nick": nick[0] if nick else "?",
                          "name": self.species.get(sp, f"#{sp}")})
        return party

    def reap(self, sess, where):
        """R1: anything at 0 HP after a battle is dead, permanently.

        Removal is what makes the rule real. Left in the party, a fainted Pokemon
        is revived by the next heal, and R10 grants unlimited healing -- so the
        defining rule of a nuzlocke would quietly not apply.

        Read back from the ROM rather than inferred from the battle transcript:
        the party is the authority on who is standing.
        """
        for mon in reversed(self.read_party(sess)):      # high slots first
            if mon["hp"] != 0:
                continue
            caught_at = next((sec for nick, sec in self.team if nick == mon["nick"]),
                             "unknown")
            self.graveyard.append({
                "nick": mon["nick"], "species": mon["name"],
                "level": mon["level"], "caught_at": caught_at, "died_at": where,
            })
            self.log("death", nickname=mon["nick"], species=mon["name"],
                     level=mon["level"], caught_at=caught_at, died_at=where)
            sess.run(S.HCMD_RELEASE, struct.pack("<I", mon["slot"]))
            self.team = [(n, s) for n, s in self.team if n != mon["nick"]]
            print(f"   † {mon['nick']} ({mon['name']} Lv{mon['level']}) died at "
                  f"{where} — gone for good (R1)")

    def resolve_pending(self, sess, slot, who, pending):
        """A move only needs a decision when all four slots are full (§4.8)."""
        out, _ = sess.run(S.HCMD_DUMP_STATE)
        for mv in pending:
            name = self.moves.get(mv, f"#{mv}")
            if self.args.auto:
                print(f"       declined {name} (auto)")
                continue
            print(f"       {who} can learn {name}, but knows four moves already.")
            for j in range(4):
                print(f"         {j + 1}) forget move slot {j + 1}")
            print(f"         0) decline {name}")
            while True:
                pk = input(f"       Choose [0-4]: ").strip()
                if pk.isdigit() and 0 <= int(pk) <= 4:
                    break
            if int(pk) == 0:
                print(f"       declined {name}")
                continue
            sess.run(S.HCMD_TEACH_MOVE,
                     struct.pack("<HBB", mv, slot, int(pk) - 1))
            print(f"       {who} learned {name}")

    def evolve_all(self, sess):
        """§4.8: evolution never fires on its own, because levels are set directly."""
        for mon in self.read_party(sess):
            try:
                out, _ = sess.run(S.HCMD_EVOLVE, struct.pack("<I", mon["slot"]))
            except S.SessionError as e:
                # Only "not eligible" is expected here. Swallowing every error
                # would hide a hang or a bad slot as a Pokemon that simply is not
                # ready, which is indistinguishable from working.
                if f"error code {HERR_NOT_ELIGIBLE}" in str(e):
                    continue
                raise
            new = struct.unpack_from("<H", out, 0)[0]
            print(f"    {mon['nick']} evolved into "
                  f"{self.species.get(new, f'#{new}')}!")

    def arrange(self, sess):
        party = self.read_party(sess)
        if len(party) < 2:
            print("  nothing to reorder")
            return
        print("\n  Current order:")
        for m in party:
            print(f"    {m['slot'] + 1}) {m['nick']} ({m['name']}) Lv{m['level']}")
        raw = input("  New order as slot numbers, e.g. 2 1 3: ").split()
        try:
            order = [int(x) - 1 for x in raw]
        except ValueError:
            print("  not a list of slot numbers")
            return
        if sorted(order) != list(range(len(party))):
            # Checked here as well as in the ROM: with R1 in force, dropping a
            # Pokemon through a bad index looks exactly like a death.
            print("  that is not a rearrangement of the current party")
            return
        payload = struct.pack("<B6Bx", len(order), *(order + [0] * (6 - len(order))))
        sess.run(S.HCMD_PARTY_ARRANGE, payload)
        print("  party rearranged")

    def summary(self, sess):
        """Full party detail between battles (§4.9 via HCMD_DUMP_STATE)."""
        out, _ = sess.run(S.HCMD_DUMP_STATE)
        count = struct.unpack_from("<I", out, 0)[0]
        print(f"\n  Party ({count}):")
        for i in range(count):
            off = 4 + i * PARTY_VIEW
            sp, hp, mx, lv, alive = struct.unpack_from("<3HBB", out, off)
            nick = decode_text(out[off + NICK_OFF:off + NICK_OFF + NICK_LEN] + b"\xff",
                               self.charmap)
            who = nick[0] if nick else "?"
            fam = self.family(sp)
            print(f"    {i + 1}. {who:<12} {self.species.get(sp, f'#{sp}'):<12} "
                  f"Lv{lv:<3} HP {hp:>3}/{mx:<3} "
                  f"{'' if alive else '(fainted)'}  family={fam}")

    def show_graveyard(self):
        if not self.graveyard:
            return
        print("\n  Graveyard:")
        for g in self.graveyard:
            print(f"    † {g['nick']:<12} {g['species']:<12} Lv{g['level']:<3} "
                  f"caught {g['caught_at']}, died at {g['died_at']}")

    def show_state(self):
        print("\n  Your team:")
        for nick, sp in self.team:
            print(f"    {nick:<12} ({sp})")
        self.show_graveyard()
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
    def family(self, species_id):
        return self.families.get(species_id, species_id)

    def log(self, kind, **fields):
        if self.led is not None:
            self.led.write(self.sess.frame, kind, **fields)

    def choose(self, raw, txt):
        self.show_text(txt)
        r = decode(raw, self.species, self.moves, self.charmap)
        self.turn += 1

        # R4/R3, enforced by the referee before the agent sees the list (§8.2):
        # a dupe cannot be caught, and the location is not spent by meeting one.
        foe = r["battlers"].get(1)
        if foe is not None:
            self.last_caught_species = foe.species
        if r.get("is_wild") and foe is not None:
            if self.family(foe.species) in self.owned_families:
                r["ball_allowed"] = 0
                if not self.last_was_dupe:      # announce once per encounter
                    print(f"   ! {foe.name} is a dupe of a family you already own — "
                          f"no ball, and this location is not spent (R3/R4)")
                self.last_was_dupe = True

        menu = render(r)
        me = r["battlers"].get(r["battler"])
        foe = r["battlers"].get(1)
        if self.led is not None:
            # The enumerated list is recorded, not just the pick: §4.7's rule is
            # that the agent chooses from legal actions, and a replay cannot
            # check that without knowing what was on offer.
            self.led.write(self.sess.frame, "decision_request", turn=self.turn,
                           battler=r["battler"],
                           forced_switch=bool(r.get("forced_switch")),
                           wild=bool(r.get("is_wild")),
                           me=(f"{me.name} Lv{me.level} {me.hp}/{me.maxhp}"
                               if me else None),
                           foe=(f"{foe.name} Lv{foe.level} {foe.hp}/{foe.maxhp}"
                                if foe else None),
                           legal_actions=[label for label, _ in menu],
                           text=decode_text(txt, self.charmap))

        if self.args.auto:
            print(f" auto-> {menu[0][0]}")
            chosen = 0
        else:
            while True:
                pick = input(f" Choose [1-{len(menu)}] (q to quit): ").strip()
                if pick.lower() in ("q", "quit"):
                    self.log("run_end_early", reason="operator quit")
                    raise SystemExit("stopped")
                if pick.isdigit() and 1 <= int(pick) <= len(menu):
                    chosen = int(pick) - 1
                    break
            print(f"  -> {menu[chosen][0]}")
        act_kind, slot, target = menu[chosen][1]
        self.log("decision", turn=self.turn, action=menu[chosen][0],
                 index=chosen, act=act_kind, slot=slot, target=target)
        return menu[chosen][1]

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
        self.last_was_dupe = False
        self.turn = 0
        self.log("fight_start", node=nd["id"], trainer=nd["trainer_resolved"],
                 trainer_id=self.trainers[nd["trainer_resolved"]],
                 battle_kind=nd.get("battle_kind", "single"))
        kind = 0 if nd.get("battle_kind", "single") == "single" else 1
        out, txt = sess.run(S.HCMD_TRAINER_BATTLE,
                            struct.pack("<HHB3x", self.trainers[nd["trainer_resolved"]],
                                        0, kind),
                            on_decision=self.choose)
        self.show_text(txt)
        code = out[0] if out else 0
        print(f"\n  == {nd['id']}: {OUTCOMES.get(code, code)} ==")
        self.log("fight_end", node=nd["id"], result=OUTCOMES.get(code, str(code)))
        self.reap(sess, nd["id"])
        if code == 2:
            # R9 as specified: losing the battle ends the run, regardless of what
            # is left elsewhere.
            self.show_graveyard()
            self.log("run_end", cause="whiteout")
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

        self.last_was_dupe = False
        self.turn = 0
        self.log("method_choice", node=nd["id"], location=nd["mapsec"],
                 available=[f"{d['method']} on {d['map']}" for d in avail],
                 chosen=f"{draw['method']} on {draw['map']}",
                 map_group=self.maps[draw["map"]][0],
                 map_num=self.maps[draw["map"]][1],
                 method_id=METHODS[draw["method"]])
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
        self.log("encounter_end", node=nd["id"], location=nd["mapsec"],
                 outcome=OUTCOMES.get(code, str(code)), dupe=self.last_was_dupe)
        self.reap(sess, nd["id"])
        if code == 2:
            self.show_graveyard()
            self.log("run_end", cause="whiteout")
            raise SystemExit("  whiteout — the run ends here (R9)")

        if self.last_was_dupe:
            # R3: only a dupe permits a reroll, so the location stays open.
            self.spent.pop(nd["mapsec"], None)
            print("  location remains open (dupe)")

        if code == 7:
            slot = self.party_slots
            self.party_slots += 1
            self.owned_families.add(self.family(self.last_caught_species))
            nick = self.ask_nickname("new catch", f"CAUGHT{slot}")
            sess.run(S.HCMD_SET_NICKNAME, nickname_payload(slot, nick, self.charmap))
            self.team.append((nick, nd["mapsec"]))
            self.log("catch", nickname=nick, location=nd["mapsec"],
                     species=self.species.get(self.last_caught_species,
                                              str(self.last_caught_species)))
            print(f"  {nick} joins the team in slot {slot + 1}")

    # -- the loop -----------------------------------------------------------
    def pick_trainer(self):
        """Name and gender belong to the attempt. Gender is not decoration: it
        decides which rival the campaign resolves."""
        if self.args.auto:
            self.gender, self.player_name = MALE, "AUTO"
        else:
            print("\nWho is attempting this run?")
            name = ""
            while not name:
                name = input(f"  Trainer name (max {PLAYER_NAME_LEN}): ").strip().upper()
            self.player_name = name[:PLAYER_NAME_LEN]
            while True:
                g = input("  Gender — (m)ale or (f)emale: ").strip().lower()
                if g.startswith("m"):
                    self.gender = MALE
                    break
                if g.startswith("f"):
                    self.gender = FEMALE
                    break
        self.rival = "MAY" if self.gender == MALE else "BRENDAN"
        return self.player_name, self.gender

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
        # The starter counts as owned for R4, so its whole line is a dupe.
        self.owned_families.add(self.family(sp_id))
        return sp_id, nick

    def main(self):
        name, gender = self.pick_trainer()
        sp_id, starter_nick = self.pick_starter()
        nodes = self.load_nodes(ROOT / "campaign/nodes.yaml")
        print(f"\nCampaign: {name} ({'male' if gender == MALE else 'female'}), "
              f"starter {self.starter}, rival {self.rival}, "
              f"{len(nodes)} nodes, seed 0x{self.args.seed:08X}")

        party = build_party([(0x12345678, sp_id, 5, STARTER_MOVES[sp_id])])
        rom_sha1 = json.loads(self.args.symbols.read_text()).get("rom_sha1", "?")

        shots = None
        if self.args.video:
            shots = self.args.ledger.parent / "frames"
            if shots.exists():
                shutil.rmtree(shots)

        # try/finally, because a run that ends in a whiteout is exactly the run
        # worth having footage of. Encoding only on the success path would drop
        # every failed attempt, which is most of them in a nuzlocke.
        try:
          with S.Session(self.args.mgba, self.args.rom, self.args.symbols,
                         shots=shots, every=self.args.every) as sess:
              self.sess = sess
              self.led = Ledger(self.args.ledger, rom_sha1=rom_sha1,
                                seed=self.args.seed, attempt=self.args.attempt,
                                trainer=name, mgba=self.args.mgba)
              # Every command is recorded, not only the semantic events. A replay
              # that reconstructs the command sequence instead of reissuing it runs
              # a different number of commands and drifts out of frame alignment
              # even when every decision matches.
              sess.on_command = lambda cmd, payload, at: self.led.write(
                  at, "command", cmd=cmd, payload=payload.hex().upper())
              sess.bootstrap(self.args.seed, party)
              # Identity is applied as a command so the choice is recorded and
              # replayable, rather than baked into the build.
              sess.run(S.HCMD_SET_PLAYER,
                       struct.pack("<B3x", gender)
                       + encode_name(name, self.charmap)[:PLAYER_NAME_LEN + 1]
                         .ljust(PLAYER_NAME_LEN + 1, b"\xff"))
              self.log("trainer", name=name,
                       gender="male" if gender == MALE else "female",
                       rival=self.rival)
              self.log("starter", species=self.species.get(sp_id, str(sp_id)),
                       species_id=sp_id, level=5, moves=STARTER_MOVES[sp_id],
                       nickname=starter_nick)
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
                          pk = input(f"\n  Choose [1-{len(options)}], (s)ummary, "
                                     f"(l)evel, (e)volve, (a)rrange, (h)eal, "
                                     f"(q)uit: ").strip().lower()
                          if pk in ("s", "summary"):
                              self.summary(sess)
                              continue
                          if pk in ("l", "level"):
                              self.level_to_cap(sess, nodes)
                              continue
                          if pk in ("e", "evolve"):
                              self.evolve_all(sess)
                              continue
                          if pk in ("a", "arrange"):
                              self.arrange(sess)
                              continue
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
              self.led.close(sess.frame, "slice complete")
              print(f"\n Ledger written to {self.args.ledger}")

        finally:
            if self.args.video:
                self.encode(shots)
        return 0

    def encode(self, shots):
        frames = sorted(shots.glob("*.png")) if shots and shots.exists() else []
        if not frames:
            print(" no frames captured", file=sys.stderr)
            return
        fps = max(1, round(60 / self.args.every))
        # Held frames -- fades, transitions, a message box waiting to be
        # dismissed -- are most of a run's wall time and none of its content.
        # mpdecimate drops near-identical frames and setpts re-times what is
        # left, which roughly halves a recording without touching the run.
        vf = "scale=480:320:flags=neighbor"
        if not self.args.full_video:
            vf += ",mpdecimate=hi=768:lo=320:frac=0.33,setpts=N/FRAME_RATE/TB"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
                        "-i", str(shots / "%06d.png"), "-vf", vf,
                        "-pix_fmt", "yuv420p", str(self.args.video)], check=True)
        kept = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of", "csv=p=0",
             str(self.args.video)], capture_output=True, text=True).stdout.strip()
        print(f" Video written to {self.args.video} "
              f"({kept or '?'} frames kept of {len(frames)} at {fps}fps)")
        # ~11 MB per recorded minute otherwise, and useless once encoded.
        shutil.rmtree(shots, ignore_errors=True)


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
    ap.add_argument("--attempt", type=int, default=1,
                    help="attempt number, recorded in the ledger header")
    ap.add_argument("--video", type=pathlib.Path,
                    help="record the run to video as it is played")
    ap.add_argument("--full-video", action="store_true",
                    help="keep every captured frame; by default held frames "
                         "(fades, waiting message boxes) are dropped")
    ap.add_argument("--every", type=int, default=2,
                    help="capture one frame in N when recording (default 2, ~30fps)")
    ap.add_argument("--ledger", type=pathlib.Path, default=None,
                    help="append-only run ledger (§11); defaults to a new "
                         "timestamped file per attempt")
    args = ap.parse_args()
    if args.ledger is None:
        # One file per attempt (§11). A fixed default silently merged separate
        # runs into one file, and the summariser then read two attempts as one.
        args.ledger = pathlib.Path(
            f"runs/{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    elif args.ledger.exists() and '"run_start"' in args.ledger.read_text():
        raise SystemExit(f"{args.ledger} already holds a run. Ledgers are "
                         f"append-only and one file per attempt; pass a "
                         f"different --ledger.")
    return Runner(args).main()


if __name__ == "__main__":
    sys.exit(main())
