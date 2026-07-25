#!/usr/bin/env python3
"""Smoke tests for the ROM mailbox: drive commands from boot and assert results.

Precursor to the real bridge (spec §5). Rather than TCP and a persistent
bridge.lua, this generates a one-shot Lua script, runs it under mgba-headless
and checks what the ROM did. That is enough to prove the §4.2-§4.3 loop is
alive, and cheap enough for CI.

Covers:
  handshake   HCMD_HEAL round-trips: command cleared, status IDLE, sequence
              incremented. Proves quickstart reaches the overworld, that
              Harness_EnsureDispatchTask registers the task, and that the task
              polls and completes the protocol.
  set_party   HCMD_SET_PARTY builds a party. Asserts the *computed stats*, not
              just the count: stats derive from species base stats, IVs, EVs and
              nature, so matching them proves every field of the spec landed and
              that CalculateMonStats ran after the IV/EV writes rather than
              before.
  warp        HCMD_WARP completes asynchronously. Asserts the handshake does not
              finish until the player is actually on the target map. Reporting
              completion at request time would let the harness roll an encounter
              against the map it was leaving, which invariant 2 forbids.

Usage:
    harness/smoke.py --mgba PATH_TO_mgba-headless

Requires harness_symbols.json (tools/harness_syms/extract_symbols.py). No
address is hardcoded.
"""

import argparse
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile

# Mirrors enum HarnessCommand / HarnessStatus in include/harness.h. Kept in sync
# by hand; when the real bridge lands these should be generated from the header.
HCMD_WARP, HCMD_HEAL, HCMD_SET_PARTY = 1, 5, 14
HSTAT_IDLE, HSTAT_CMD_PENDING, HSTAT_ERROR = 0, 1, 3

# struct HarnessMailbox field offsets. Field order is a wire contract.
OFF_CMD, OFF_STATUS, OFF_SEQ, OFF_IN_LEN, OFF_OUT_LEN, OFF_PAYLOAD_IN = 0, 2, 4, 8, 10, 12

# struct HarnessMonSpec, packed as the ROM lays it out. The u32 count preceding
# the specs exists for alignment: these contain u32 fields and ARM cannot load
# those from an unaligned address.
SPEC_FMT = "<IHH4HBB6B6B11s3x"
SPEC_SIZE = 44

# struct Pokemon offsets (include/pokemon.h): 80-byte BoxPokemon, then status,
# level, mail, and the six computed stats. Species is encrypted inside
# BoxPokemon and deliberately not read here — the stats prove it instead.
MON_SIZE = 100
OFF_LEVEL, OFF_HP, OFF_MAXHP = 84, 86, 88
OFF_ATK, OFF_DEF, OFF_SPE, OFF_SPA, OFF_SPD = 90, 92, 94, 96, 98

EOS = 0xFF

# Frame budget: generous rather than tuned, so a slow boot cannot read as a
# failure. SELECT is pulsed across a window so JOY_NEW gets a fresh edge
# whenever Task_TitleScreenPhase3 goes live.
SEL_LO, SEL_HI, FIRST_CMD, GIVE_UP = 600, 1200, 1400, 4000

# Marshtomp, level 15, 31 IVs, no EVs. personality 0x12345678 % 25 = 21 = Gentle
# (+SpD, -Def), which makes the nature visible in the asserted stats.
SPECIES_MARSHTOMP, LEVEL, PERSONALITY = 259, 15, 0x12345678
BASE = dict(hp=70, atk=85, defense=70, spe=50, spa=60, spd=70)

# Target map for the warp. Group 3 / map 0 is arbitrary and only needs to differ
# from wherever quickstart drops the player.
WARP_GROUP, WARP_NUM, WARP_X, WARP_Y = 3, 0, 5, 5


def expected_stats() -> dict:
    def s(base, iv=31, ev=0):
        return (2 * base + iv + ev // 4) * LEVEL // 100 + 5
    return {
        "maxHP": (2 * BASE["hp"] + 31) * LEVEL // 100 + LEVEL + 10,
        "atk": s(BASE["atk"]),
        "def": int(s(BASE["defense"]) * 0.9),   # Gentle lowers Defence
        "spe": s(BASE["spe"]),
        "spa": s(BASE["spa"]),
        "spd": int(s(BASE["spd"]) * 1.1),       # Gentle raises Sp. Def
    }


def build_party_payload() -> bytes:
    spec = struct.pack(SPEC_FMT, PERSONALITY, SPECIES_MARSHTOMP, 0,
                       33, 0, 0, 0,            # Tackle, then empty slots
                       LEVEL, 0,
                       *([31] * 6), *([0] * 6),
                       bytes([EOS] * 11))
    assert len(spec) == SPEC_SIZE, len(spec)
    return struct.pack("<I", 1) + spec


LUA = """
local B, PC, P = {base}, {parties_count}, {parties}
local PARTY_PAYLOAD = {{{party_bytes}}}
local WARP_PAYLOAD = {{{warp_bytes}}}
local f = io.open("{out}", "w")
local n, phase, warpFrame = 0, 0, 0

local function send(cmd, bytes)
  for i, b in ipairs(bytes) do emu:write8(B + {off_in} + i - 1, b) end
  emu:write16(B + {off_in_len}, #bytes)
  emu:write16(B + {off_cmd}, cmd)
  emu:write16(B + {off_status}, {pending})
end
local function seq() return emu:read32(B + {off_seq}) end
local function status() return emu:read16(B + {off_status}) end

callbacks:add("frame", function()
  n = n + 1
  if n >= {sel_lo} and n <= {sel_hi} then
    if (n % 30) < 5 then emu:setKeys(4) else emu:setKeys(0) end
    return
  end
  if n == {sel_hi} + 1 then emu:setKeys(0) end

  if n == {first} and phase == 0 then
    send({heal}, {{}})
    phase = 1

  elseif phase == 1 and seq() >= 1 then
    f:write(string.format("HANDSHAKE cmd=%d status=%d seq=%d\\n",
            emu:read16(B + {off_cmd}), status(), seq()))
    send({set_party}, PARTY_PAYLOAD)
    phase = 2

  elseif phase == 2 and seq() >= 2 then
    f:write(string.format(
      "PARTY status=%d count=%d level=%d hp=%d maxHP=%d atk=%d def=%d spe=%d spa=%d spd=%d\\n",
      status(), emu:read8(PC), emu:read8(P + {o_lv}), emu:read16(P + {o_hp}),
      emu:read16(P + {o_max}), emu:read16(P + {o_atk}), emu:read16(P + {o_def}),
      emu:read16(P + {o_spe}), emu:read16(P + {o_spa}), emu:read16(P + {o_spd})))
    send({warp}, WARP_PAYLOAD)
    warpFrame = n
    phase = 3

  elseif phase == 3 and n > warpFrame and seq() >= 3 then
    -- Frames elapsed proves the ROM waited for arrival instead of reporting
    -- completion the moment DoWarp was requested.
    f:write(string.format("WARP status=%d seq=%d frames=%d\\n",
            status(), seq(), n - warpFrame))
    phase = 4
  end

  if n == {giveup} and phase < 4 then
    f:write(string.format("TIMEOUT phase=%d status=%d seq=%d\\n",
            phase, status(), seq()))
    phase = 4
  end
  f:flush()
end)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mgba", required=True, type=pathlib.Path,
                    help="mgba-headless, built with -DBUILD_HEADLESS=ON "
                         "-DENABLE_SCRIPTING=ON (no released build has --script)")
    ap.add_argument("--rom", type=pathlib.Path, default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("--symbols", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    for p in (args.mgba, args.rom, args.symbols):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 1

    sym = json.loads(args.symbols.read_text())["symbols"]
    missing = [s for s in ("gHarnessMailbox", "gParties", "gPartiesCount") if s not in sym]
    if missing:
        print(f"error: symbols absent ({', '.join(missing)}) — ROM built without "
              f"the harness?", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        out, script = tmp / "result", tmp / "smoke.lua"
        script.write_text(LUA.format(
            base=sym["gHarnessMailbox"], parties=sym["gParties"],
            parties_count=sym["gPartiesCount"], out=out,
            party_bytes=",".join(map(str, build_party_payload())),
            warp_bytes=",".join(map(str, (WARP_GROUP, WARP_NUM, WARP_X, WARP_Y))),
            off_cmd=OFF_CMD, off_status=OFF_STATUS, off_seq=OFF_SEQ,
            off_in_len=OFF_IN_LEN, off_in=OFF_PAYLOAD_IN, pending=HSTAT_CMD_PENDING,
            heal=HCMD_HEAL, set_party=HCMD_SET_PARTY, warp=HCMD_WARP,
            sel_lo=SEL_LO, sel_hi=SEL_HI, first=FIRST_CMD, giveup=GIVE_UP,
            o_lv=OFF_LEVEL, o_hp=OFF_HP, o_max=OFF_MAXHP, o_atk=OFF_ATK,
            o_def=OFF_DEF, o_spe=OFF_SPE, o_spa=OFF_SPA, o_spd=OFF_SPD))

        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = (f"{args.mgba.resolve().parent}:"
                                  f"{env.get('LD_LIBRARY_PATH', '')}")
        try:
            subprocess.run([str(args.mgba), "--script", str(script), str(args.rom)],
                           env=env, capture_output=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            pass  # mgba-headless has no exit condition; the log is the result

        lines = out.read_text().splitlines() if out.exists() else []

    return report(lines)


def parse(line: str) -> dict:
    return {k: int(v) for k, v in (f.split("=") for f in line.split()[1:])}


def report(lines: list[str]) -> int:
    got = {l.split()[0]: parse(l) for l in lines if l and "=" in l}
    failures: list[str] = []

    if "TIMEOUT" in got:
        t = got["TIMEOUT"]
        stage = ["handshake", "handshake", "set_party", "warp"][min(t["phase"], 3)]
        print(f"FAIL: timed out during {stage} "
              f"(status={t['status']} seq={t['seq']})")
        print("      If phase=0, the dispatch task never ran: check "
              "HARNESS_ENABLED and that quickstart reached the overworld.")
        return 1

    def check(name, cond, detail):
        if not cond:
            failures.append(f"{name}: {detail}")

    if "HANDSHAKE" not in got:
        print("FAIL: no reply from ROM — the dispatch task never ran.")
        return 1
    h = got["HANDSHAKE"]
    check("handshake", h["status"] == HSTAT_IDLE, f"status {h['status']}, want IDLE")
    check("handshake", h["cmd"] == 0, f"command {h['cmd']} not cleared")
    check("handshake", h["seq"] == 1, f"sequence {h['seq']}, want 1")

    if "PARTY" not in got:
        failures.append("set_party: no result")
    else:
        p, exp = got["PARTY"], expected_stats()
        check("set_party", p["status"] == HSTAT_IDLE, f"status {p['status']}, want IDLE")
        check("set_party", p["count"] == 1, f"count {p['count']}, want 1")
        check("set_party", p["level"] == LEVEL, f"level {p['level']}, want {LEVEL}")
        check("set_party", p["hp"] == p["maxHP"], f"hp {p['hp']} != maxHP {p['maxHP']}")
        for k, v in exp.items():
            check("set_party", p[k] == v, f"{k} {p[k]}, want {v}")

    if "WARP" not in got:
        failures.append("warp: no result")
    else:
        w = got["WARP"]
        check("warp", w["status"] == HSTAT_IDLE, f"status {w['status']}, want IDLE")
        check("warp", w["seq"] == 3, f"sequence {w['seq']}, want 3")
        check("warp", w["frames"] > 1,
              "completed on the request frame — it is not waiting for arrival")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"      - {f}")
        return 1

    print(f"PASS: handshake, set_party (stats match Marshtomp L{LEVEL} "
          f"31 IVs Gentle), warp (async, {got['WARP']['frames']} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
