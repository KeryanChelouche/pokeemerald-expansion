#!/usr/bin/env python3
"""Play a battle by hand through the agent's own interface.

Stand in for the LLM: the harness shows you exactly what an agent would see -- a
serialised battle state and an enumerated list of legal actions -- and you pick
one. Nothing else is available to you, which is the point. If a choice cannot be
made from this screen, the agent could not make it either.

Per spec §4.7 you choose "use Tackle", never "press A, press right, press A".
Menu navigation and message advancement are the harness's problem.

Usage:
    harness/play.py --mgba /root/mgba-src/build/mgba-headless

    # pick the fight and your team
    harness/play.py --trainer 483 --kind double --seed 12345

Requires harness_symbols.json (tools/harness_syms/extract_symbols.py).

How it works: mgba-headless runs a generated Lua script that owns the mailbox.
On each decision request the script writes the raw request bytes to a file and
waits; this process decodes them, prompts you, and writes a reply file. Request
and reply files are numbered, so there is no overwrite race. The ROM blocks in
HSTAT_DECISION_PENDING while you think, so taking your time costs nothing.
"""

import argparse
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import time

# --- mailbox / request layout: mirrors include/harness.h ---------------------
HCMD_WARP, HCMD_TRAINER_BATTLE, HCMD_ROLL_ENCOUNTER = 1, 2, 3
HCMD_SET_FLAG, HCMD_SET_PARTY = 12, 14
HCMD_SET_SEED, HCMD_DECISION = 15, 16

# SYSTEM_FLAGS (TRAINER_FLAGS_END + 1) + 0x7 .. +0xE, per include/constants/flags.h.
# Granted so the party actually obeys: a Pokemon above the obedience level for
# the badges held will refuse orders, nap, or hit itself, which is easy to
# mistake for a broken decision hook.
SYSTEM_FLAGS = 0x860
BADGE_FLAGS = [SYSTEM_FLAGS + 0x7 + i for i in range(8)]
HSTAT_DECISION_PENDING = 2
HACT_MOVE, HACT_SWITCH, HACT_BALL = 0, 1, 2
ITEM_POKE_BALL = 1
HTARGET_DEFAULT = 0xFF
PAYLOAD_SIZE = 2048
OFF_PAYLOAD_OUT = 12 + PAYLOAD_SIZE

VIEW_SIZE, MAX_BATTLERS, MAX_MOVES, PARTY_SIZE = 36, 4, 4, 6
OFF_BATTLERS = 8
OFF_MOVES = OFF_BATTLERS + MAX_BATTLERS * VIEW_SIZE      # 152
OFF_LEGAL_MOVES = OFF_MOVES + MAX_MOVES * 4              # 168
OFF_LEGAL_SWITCH = OFF_LEGAL_MOVES + MAX_MOVES           # 172
OFF_PARTY = OFF_LEGAL_SWITCH + PARTY_SIZE + 2            # 180 (explicit padding2)
OFF_IS_WILD, OFF_BALL_ALLOWED = 6, 7

# Encounter methods, mirroring enum HarnessEncounterMethod.
METHODS = {"grass": 0, "surf": 1, "rocksmash": 2,
           "rod_old": 3, "rod_good": 4, "rod_super": 5}
# MAP_ROUTE101 = (16 | (0 << 8)) in include/constants/map_groups.h.
DEFAULT_MAP = (0, 16)
REQUEST_SIZE = OFF_PARTY + PARTY_SIZE * 8                # 226

SPEC_FMT = "<IHH4HBB6B6B11s3x"

# Battler id -> human label. Doubles layout per §4.9.
SLOT_NAMES = {0: "your left", 1: "foe left", 2: "your right", 3: "foe right"}

# Mirrors STATUS1_* in include/constants/battle.h. Sleep is not a flag: the low
# three bits are a turn counter, so it must be masked, not tested bitwise.
STATUS1_SLEEP_MASK = 0x7
STATUS_BITS = [(1 << 3, "PSN"), (1 << 4, "BRN"), (1 << 5, "FRZ"),
               (1 << 6, "PAR"), (1 << 7, "TOX"), (1 << 12, "FRB")]

TEXTLOG_SIZE = 4096
EOS = 0xFF

# Control bytes that structure a message rather than print a glyph. Line and
# paragraph breaks become spaces so each battle line reads as one sentence.
TEXT_CONTROL = {0xFA: " ", 0xFB: " ", 0xFE: " "}


# Multi-byte ligatures and control sequences, longest-match first. Without
# these, `PKMN` (53 54) decodes as two unrelated single-byte glyphs and reads as
# mojibake. FD xx are text-buffer placeholders the battle engine substitutes.
# Argument counts for FC control codes; see charmap.txt.
FC_ARGS = {0x00: 0, 0x04: 3, 0x09: 0, 0x0A: 0, 0x0B: 1, 0x10: 0, 0x11: 0, 0x12: 1}

MULTI = {
    (0x53, 0x54): "POKéMON",
    (0xFD, 0x01): "{PLAYER}",
    (0xFD, 0x02): "{VAR1}",
    (0xFD, 0x03): "{VAR2}",
    (0xFD, 0x04): "{VAR3}",
}


def load_charmap(path: pathlib.Path) -> dict[int, str]:
    """Byte -> character from charmap.txt, single-byte quoted entries.

    charmap.txt assigns several bytes twice (a Latin glyph and a Japanese kana
    share 0x53), so later entries must not overwrite earlier ones -- hence
    setdefault. Multi-byte sequences are handled by MULTI above.
    """
    out = {}
    pat = re.compile(r"^'(.)'\s*=\s*([0-9A-Fa-f]{2})\s*$")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = pat.match(line.strip())
        if m:
            out.setdefault(int(m.group(2), 16), m.group(1))
    out[0x53] = "PK"      # only reached when not part of the PKMN ligature
    out[0x39] = " "       # NBSP; charmap also spells it '~', which prints as one
    out[0x3A] = ""        # ZWS
    return out


def decode_text(raw: bytes, charmap: dict[int, str]) -> list[str]:
    """Split the EOS-terminated records and decode each to a readable line."""
    lines, cur, i = [], [], 0
    while i < len(raw):
        b = raw[i]
        pair = (b, raw[i + 1]) if i + 1 < len(raw) else None
        if pair in MULTI:
            cur.append(MULTI[pair])
            i += 2
            continue
        if b == 0xFC:
            # Control-code prefix. Argument count varies by code (charmap.txt:
            # FC 00 takes none, FC 04 takes three, most take one), so a fixed
            # skip leaves stray argument bytes to be decoded as glyphs -- the
            # source of the kana in "Gotcha! ... was caught!".
            code = raw[i + 1] if i + 1 < len(raw) else 0
            i += 2 + FC_ARGS.get(code, 1)
            continue
        i += 1
        if b == EOS:
            s = "".join(cur).strip()
            if s:
                lines.append(s)
            cur = []
        elif b in TEXT_CONTROL:
            cur.append(TEXT_CONTROL[b])
        else:
            cur.append(charmap.get(b, ""))
    return lines


def load_names(path: pathlib.Path, prefix: str) -> dict[int, str]:
    """Parse `NAME = 123,` and bare `NAME,` enum entries out of a constants header."""
    names, counter = {}, 0
    pat = re.compile(rf"^\s*{prefix}([A-Z0-9_]+)\s*(?:=\s*(0x[0-9a-fA-F]+|\d+))?\s*,")
    for line in path.read_text(errors="replace").splitlines():
        m = pat.match(line)
        if not m:
            continue
        if m.group(2) is not None:
            counter = int(m.group(2), 0)
        names.setdefault(counter, m.group(1).replace("_", " ").title())
        counter += 1
    return names


class Battler:
    def __init__(self, raw: bytes, species_names):
        (self.species, self.hp, self.maxhp, self.atk, self.dfn, self.spe,
         self.spa, self.spd, self.status1, self.level) = struct.unpack_from("<8HIB", raw)
        self.name = species_names.get(self.species, f"#{self.species}")

    def status(self) -> str:
        s = []
        turns = self.status1 & STATUS1_SLEEP_MASK
        if turns:
            s.append(f"SLP({turns})")   # turns left matters for the decision
        s += [tag for bit, tag in STATUS_BITS if self.status1 & bit]
        return " ".join(s)


def decode(raw: bytes, species_names, move_names) -> dict:
    r = {}
    (r["battler"], r["n_moves"], r["n_switch"], r["is_double"],
     r["n_battlers"], r["alive"], r["is_wild"], r["ball_allowed"]) = \
        struct.unpack_from("<8B", raw, 0)
    r["battlers"] = {i: Battler(raw[OFF_BATTLERS + i * VIEW_SIZE:], species_names)
                     for i in range(MAX_BATTLERS) if r["alive"] >> i & 1}
    r["moves"] = []
    for i in range(MAX_MOVES):
        mv, pp, maxpp = struct.unpack_from("<HBB", raw, OFF_MOVES + i * 4)
        r["moves"].append({"id": mv, "pp": pp, "maxpp": maxpp,
                           "name": move_names.get(mv, f"#{mv}")})
    r["legal_moves"] = list(struct.unpack_from("<4B", raw, OFF_LEGAL_MOVES))[:r["n_moves"]]
    r["legal_switch"] = list(struct.unpack_from("<6B", raw, OFF_LEGAL_SWITCH))[:r["n_switch"]]
    r["party"] = []
    for i in range(PARTY_SIZE):
        sp, hp, mx, lv, legal = struct.unpack_from("<3HBB", raw, OFF_PARTY + i * 8)
        r["party"].append({"species": sp, "hp": hp, "maxhp": mx, "level": lv,
                           "legal": bool(legal),
                           "name": species_names.get(sp, f"#{sp}")})
    return r


def render(r: dict) -> list[tuple]:
    """Print the state and return the action menu as (label, decision-bytes)."""
    me = r["battlers"].get(r["battler"])
    kind = ("WILD " if r.get("is_wild") else "") + ("DOUBLE" if r["is_double"] else "SINGLE")
    print(f"\n{'=' * 66}")
    print(f" {kind} battle — deciding for battler {r['battler']} ({SLOT_NAMES.get(r['battler'], '?')})")
    print(f"{'=' * 66}")

    for bid in sorted(r["battlers"]):
        b = r["battlers"][bid]
        side = "YOU " if bid % 2 == 0 else "FOE "
        mark = " <-- deciding" if bid == r["battler"] else ""
        st = b.status()
        print(f" {side}[{bid}] {b.name:<12} Lv{b.level:<3} HP {b.hp:>3}/{b.maxhp:<3}"
              f"{'  ' + st if st else ''}{mark}")
    if me:
        print(f"      atk {me.atk}  def {me.dfn}  spe {me.spe}  spa {me.spa}  spd {me.spd}")

    print("\n Legal actions:")
    menu = []
    for slot in r["legal_moves"]:
        mv = r["moves"][slot]
        menu.append((f"Move   {mv['name']:<14} PP {mv['pp']}/{mv['maxpp']}",
                     (HACT_MOVE, slot, HTARGET_DEFAULT)))
    # In doubles a move may be aimed at either foe, so offer the targeted forms too.
    if r["is_double"]:
        for slot in r["legal_moves"]:
            mv = r["moves"][slot]
            for tid in sorted(b for b in r["battlers"] if b % 2 == 1):
                menu.append((f"Move   {mv['name']:<14} -> [{tid}] "
                             f"{r['battlers'][tid].name} ({SLOT_NAMES.get(tid)})",
                             (HACT_MOVE, slot, tid)))
    for slot in r["legal_switch"]:
        p = r["party"][slot]
        menu.append((f"Switch to {p['name']:<12} Lv{p['level']} HP {p['hp']}/{p['maxhp']}",
                     (HACT_SWITCH, slot, HTARGET_DEFAULT)))
    # §4.6: only offered when the referee authorised a catch for this encounter.
    if r.get("ball_allowed"):
        menu.append(("Throw  POKé BALL", (HACT_BALL, ITEM_POKE_BALL & 0xFF,
                                          ITEM_POKE_BALL >> 8)))

    for i, (label, _) in enumerate(menu, 1):
        print(f"   {i:>2}) {label}")
    return menu


def build_party(species_names) -> bytes:
    """Default team: two level 20 mons with Tackle. Deterministic personalities."""
    mons = [(0x12345678, 259, 20), (0x11112222, 255, 20)]
    out = struct.pack("<I", len(mons))
    for pers, sp, lv in mons:
        out += struct.pack(SPEC_FMT, pers, sp, 0, 33, 0, 0, 0, lv, 0,
                           *([31] * 6), *([0] * 6), bytes([0xFF] * 11))
    return out


LUA = """
local B = {base}
local OUT = B + {off_out}
local TLOG = {tlog}
local TSIZE = {tsize}
local DIR = "{dir}"
-- Ordered setup commands; each is sent only after the previous one completed,
-- which the sequence counter reports. Warp and encounter finish asynchronously
-- and still bump it, so no special-casing is needed here.
local QUEUE = {{{queue}}}
local n, qi, sent, booted, ended = 0, 1, 0, false, false
local reqN, awaiting, lastW = 0, false, 0

local function send(cmd, bytes)
  for i, v in ipairs(bytes) do emu:write8(B + 12 + i - 1, v) end
  emu:write16(B + 8, #bytes); emu:write16(B + 0, cmd); emu:write16(B + 2, 1)
end
local function seq() return emu:read32(B + 4) end

callbacks:add("frame", function()
  n = n + 1
  if n >= 600 and n <= 1200 then
    if (n % 30) < 5 then emu:setKeys(4) else emu:setKeys(0) end
    return
  end
  if n == 1201 then emu:setKeys(0); booted = true end
  if not booted then return end

  -- Advance menus and messages. The agent never sees this; it only ever
  -- chooses actions (spec §4.7).
  if (n % 16) < 5 then emu:setKeys(1) else emu:setKeys(0) end

  if qi <= #QUEUE then
    if seq() == sent then
      send(QUEUE[qi][1], QUEUE[qi][2])
      sent = sent + 1
      qi = qi + 1
    end
    return
  end

  if emu:read16(B + 2) == {pending} and not awaiting then
    reqN = reqN + 1
    local len = emu:read16(B + 10)
    local parts = {{}}
    for i = 0, len - 1 do parts[#parts + 1] = string.format("%02X", emu:read8(OUT + i)) end
    local w = io.open(DIR .. "/req_" .. reqN, "w")
    w:write(table.concat(parts)); w:close()

    -- Only bytes appended since the last request, so the driver shows the
    -- narration for exactly the turn that just resolved.
    local nowW = emu:read32(TLOG)
    local tp = {{}}
    local i = lastW
    while i < nowW do
      tp[#tp + 1] = string.format("%02X", emu:read8(TLOG + 4 + (i % TSIZE)))
      i = i + 1
    end
    lastW = nowW
    local tw = io.open(DIR .. "/txt_" .. reqN, "w")
    tw:write(table.concat(tp)); tw:close()
    awaiting = true
  end

  if awaiting then
    local rf = io.open(DIR .. "/reply_" .. reqN, "r")
    if rf then
      local a, b2, c = rf:read("n"), rf:read("n"), rf:read("n")
      rf:close(); os.remove(DIR .. "/reply_" .. reqN)
      send({c_dec}, {{a, b2, c, 0}})
      awaiting = false
    end
  end

  -- The last queued command is the battle itself. Answering a decision also
  -- bumps the sequence counter, so that alone is not an end condition; the
  -- battle's completion is the one that leaves an outcome byte in payloadOut.
  if not ended and seq() >= sent and emu:read16(B + 10) == 1
     and emu:read16(B + 2) == 0 and not awaiting then
    ended = true
    emu:setKeys(0)
    local nowW = emu:read32(TLOG)
    local tp = {{}}
    local i = lastW
    while i < nowW do
      tp[#tp + 1] = string.format("%02X", emu:read8(TLOG + 4 + (i % TSIZE)))
      i = i + 1
    end
    lastW = nowW
    local tw = io.open(DIR .. "/txt_end", "w")
    tw:write(table.concat(tp)); tw:close()
    -- Written once, and via rename, so the reader cannot observe a partial
    -- file. Rewriting it every frame let the driver read a truncated value and
    -- report "outcome 0" for a battle that was won.
    local w = io.open(DIR .. "/END.tmp", "w")
    w:write(string.format("%d %d", emu:read8(OUT), emu:read16(B + 2))); w:close()
    os.rename(DIR .. "/END.tmp", DIR .. "/END")
  end
end)
"""

OUTCOMES = {1: "YOU WON", 2: "YOU LOST", 3: "DREW", 4: "RAN",
            6: "IT FLED", 7: "CAUGHT"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mgba", type=pathlib.Path,
                    default=pathlib.Path("/root/mgba-src/build/mgba-headless"))
    ap.add_argument("--rom", type=pathlib.Path, default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("--symbols", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--trainer", type=int, default=520,
                    help="trainer id (default 520 = Brendan/Route103, one level 5 Treecko)")
    ap.add_argument("--kind", choices=("single", "double"), default="single")
    ap.add_argument("--encounter", choices=sorted(METHODS),
                    help="roll a wild encounter instead of a trainer battle")
    ap.add_argument("--map", type=lambda s: tuple(int(x) for x in s.split(",")),
                    default=DEFAULT_MAP, metavar="GROUP,NUM",
                    help="map to warp to before rolling (default 0,16 = Route 101)")
    ap.add_argument("--no-catch", action="store_true",
                    help="withhold catch authorisation, as the referee would for "
                         "a spent or dupe location")
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=0xC0FFEE01,
                    help="RNG seed; the ROM otherwise seeds from the host clock")
    args = ap.parse_args()

    for p in (args.mgba, args.rom, args.symbols):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 1

    root = pathlib.Path(__file__).resolve().parent.parent
    species_names = load_names(root / "include/constants/species.h", "SPECIES_")
    move_names = load_names(root / "include/constants/moves.h", "MOVE_")

    syms = json.loads(args.symbols.read_text())["symbols"]
    base, tlog = syms["gHarnessMailbox"], syms["gHarnessTextLog"]
    charmap = load_charmap(root / "charmap.txt")
    kind = 0 if args.kind == "single" else 1

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        # Ordered setup, then the fight itself as the final command. Badges come
        # before the party is built: without them a Pokemon above the obedience
        # level refuses orders, which reads as a broken decision hook.
        queue: list[tuple[int, bytes]] = [(HCMD_SET_SEED, struct.pack("<I", args.seed))]
        queue += [(HCMD_SET_FLAG, struct.pack("<H", f)) for f in BADGE_FLAGS]
        queue.append((HCMD_SET_PARTY, build_party(species_names)))
        if args.encounter:
            # Warp first: the roll uses the current map's tables and stamps
            # met-location from it (invariant 2).
            queue.append((HCMD_WARP, struct.pack("<4B", args.map[0], args.map[1], 5, 5)))
            queue.append((HCMD_ROLL_ENCOUNTER,
                          struct.pack("<BB2x", METHODS[args.encounter],
                                      0 if args.no_catch else 1)))
        else:
            queue.append((HCMD_TRAINER_BATTLE,
                          struct.pack("<HHB3x", args.trainer, 0, kind)))

        lua_queue = ",".join(
            "{%d,{%s}}" % (cmd, ",".join(map(str, payload))) for cmd, payload in queue)

        (d / "play.lua").write_text(LUA.format(
            base=base, off_out=OFF_PAYLOAD_OUT, dir=d,
            tlog=tlog, tsize=TEXTLOG_SIZE, queue=lua_queue,
            c_dec=HCMD_DECISION, pending=HSTAT_DECISION_PENDING))

        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = (f"{args.mgba.resolve().parent}:"
                                  f"{env.get('LD_LIBRARY_PATH', '')}")
        proc = subprocess.Popen([str(args.mgba), "--script", str(d / "play.lua"),
                                 str(args.rom), "-l", "0"],
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

        what = (f"{args.encounter} encounter on map {args.map[0]},{args.map[1]}"
                if args.encounter else f"trainer {args.trainer} ({args.kind})")
        print(f"Booting (seed 0x{args.seed:08X}, {what})...")
        print("The ROM waits while you think, so there is no time pressure.")
        served = 0
        try:
            while True:
                if (d / "END").exists():
                    tail = d / "txt_end"
                    if tail.exists():
                        for line in decode_text(bytes.fromhex(tail.read_text()), charmap):
                            print(f"   | {line}")
                        tail.unlink()
                    code = int(((d / "END").read_text().split() or ["0"])[0])
                    print(f"\n{'=' * 66}\n {OUTCOMES.get(code, f'outcome {code}')}"
                          f"  after {served} decision(s)\n{'=' * 66}")
                    return 0
                if proc.poll() is not None:
                    print("\nEmulator exited before the battle finished.", file=sys.stderr)
                    return 1

                nxt = d / f"req_{served + 1}"
                if not nxt.exists():
                    time.sleep(0.05)
                    continue

                # The Lua writer is not atomic; wait for the whole record.
                raw = bytes.fromhex(nxt.read_text())
                if len(raw) < REQUEST_SIZE:
                    time.sleep(0.02)
                    continue
                served += 1

                tfile = d / f"txt_{served}"
                if tfile.exists():
                    for line in decode_text(bytes.fromhex(tfile.read_text()), charmap):
                        print(f"   | {line}")

                menu = render(decode(raw, species_names, move_names))
                if not menu:
                    print(" No legal actions — this should be impossible.", file=sys.stderr)
                    return 1

                while True:
                    try:
                        pick = input(f" Choose [1-{len(menu)}] (q to quit): ").strip()
                    except EOFError:
                        return 1
                    if pick.lower() in ("q", "quit"):
                        return 1
                    if pick.isdigit() and 1 <= int(pick) <= len(menu):
                        break
                    print("  not one of the listed actions")

                label, (typ, slot, tgt) = menu[int(pick) - 1]
                print(f"  -> {label}")
                tmp = d / f"reply_{served}.tmp"
                tmp.write_text(f"{typ} {slot} {tgt}\n")
                tmp.rename(d / f"reply_{served}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
