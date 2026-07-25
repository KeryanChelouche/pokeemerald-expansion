#!/usr/bin/env python3
"""Smoke test: drive the ROM mailbox from boot and assert the ROM replies.

This is the precursor to the real bridge (spec §5). It does not use TCP or a
persistent bridge.lua; it generates a one-shot Lua script, runs it under
mgba-headless, and checks the result. That is enough to prove the §4.2-§4.3
loop is alive, and it is cheap enough to run in CI.

What it verifies:
  1. Quickstart (SELECT at the title screen) reaches the overworld unattended,
     skipping the intro, naming and Birch sequence.
  2. Harness_EnsureDispatchTask registers Task_HarnessDispatch from
     OverworldBasic.
  3. The task polls each frame, executes a command, and completes the protocol
     handshake: command cleared, status returned, sequence incremented.

Usage:
    harness/smoke_dispatch.py --mgba PATH_TO_mgba-headless [--rom pokeemerald.gba]

Symbol addresses come from harness_symbols.json; nothing here is hardcoded.
Generate it first:
    tools/harness_syms/extract_symbols.py pokeemerald.map --require gHarnessMailbox
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

# Mirrors enum HarnessCommand / HarnessStatus in include/harness.h. Kept in sync
# by hand for now; when the real bridge lands these should be generated from the
# header rather than retyped.
HCMD_HEAL = 5
HSTAT_IDLE = 0
HSTAT_CMD_PENDING = 1
HSTAT_ERROR = 3

# Field offsets within struct HarnessMailbox. Field order is a wire contract.
OFF_COMMAND, OFF_STATUS, OFF_SEQUENCE = 0, 2, 4
OFF_PAYLOAD_IN_LEN, OFF_PAYLOAD_OUT_LEN = 8, 10

# Frame budget. The title screen needs the intro to finish first; these numbers
# are generous rather than tuned, because a slow boot must not read as a failure.
SELECT_WINDOW = (600, 1200)
SEND_FRAME = 1500
GIVE_UP_FRAME = 2600

LUA = """
local B = {base}
local f = io.open("{out}", "w")
local n, sent, done = 0, false, false

callbacks:add("frame", function()
  n = n + 1

  -- Pulse SELECT so JOY_NEW gets a fresh edge whenever Task_TitleScreenPhase3
  -- becomes live, rather than depending on hitting one exact frame.
  if n >= {sel_lo} and n <= {sel_hi} then
    if (n % 30) < 5 then emu:setKeys(4) else emu:setKeys(0) end
    return
  end
  if n == {sel_hi} + 1 then emu:setKeys(0) end

  if n == {send} and not sent then
    -- Command and payload first, status last: the ROM must never observe a
    -- pending status alongside a stale command.
    emu:write16(B + {off_cmd}, {cmd})
    emu:write16(B + {off_in_len}, 0)
    emu:write16(B + {off_status}, {pending})
    f:write(string.format("SENT %d %d %d\\n",
            emu:read16(B + {off_cmd}), emu:read16(B + {off_status}),
            emu:read32(B + {off_seq})))
    f:flush(); sent = true
  end

  if sent and not done and n > {send} and (n % 10) == 0 then
    local st, sq = emu:read16(B + {off_status}), emu:read32(B + {off_seq})
    if sq > 0 or st ~= {pending} then
      f:write(string.format("REPLY %d %d %d %d\\n",
              emu:read16(B + {off_cmd}), st, sq, emu:read16(B + {off_out_len})))
      f:flush(); done = true
    end
  end

  if n == {giveup} and not done then
    f:write("TIMEOUT\\n"); f:flush()
  end
end)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mgba", required=True, type=pathlib.Path,
                    help="path to mgba-headless (built with -DBUILD_HEADLESS=ON "
                         "-DENABLE_SCRIPTING=ON; no released build has --script)")
    ap.add_argument("--rom", type=pathlib.Path, default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("--symbols", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    for p in (args.mgba, args.rom, args.symbols):
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 1

    doc = json.loads(args.symbols.read_text())
    base = doc["symbols"].get("gHarnessMailbox")
    if base is None:
        print("error: gHarnessMailbox absent from symbols — ROM built without "
              "the harness?", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        out, script = tmp / "result", tmp / "smoke.lua"
        script.write_text(LUA.format(
            base=base, out=out, sel_lo=SELECT_WINDOW[0], sel_hi=SELECT_WINDOW[1],
            send=SEND_FRAME, giveup=GIVE_UP_FRAME, cmd=HCMD_HEAL,
            pending=HSTAT_CMD_PENDING, off_cmd=OFF_COMMAND, off_status=OFF_STATUS,
            off_seq=OFF_SEQUENCE, off_in_len=OFF_PAYLOAD_IN_LEN,
            off_out_len=OFF_PAYLOAD_OUT_LEN))

        env = dict(os.environ)
        libdir = args.mgba.resolve().parent
        env["LD_LIBRARY_PATH"] = f"{libdir}:{env.get('LD_LIBRARY_PATH', '')}"
        try:
            subprocess.run([str(args.mgba), "--script", str(script), str(args.rom)],
                           env=env, capture_output=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            pass  # mgba-headless has no exit condition here; the log is the result

        lines = out.read_text().split("\n") if out.exists() else []

    reply = next((l for l in lines if l.startswith("REPLY")), None)
    if not reply:
        print("FAIL: no reply from ROM — the dispatch task never ran.")
        print("      Check HARNESS_ENABLED, and that quickstart reached the "
              "overworld (ENABLE_QUICKSTART must be TRUE and the build "
              "non-release).")
        for l in lines:
            print(f"      {l}")
        return 1

    _, cmd, status, seq, out_len = reply.split()
    cmd, status, seq, out_len = int(cmd), int(status), int(seq), int(out_len)

    problems = []
    if status == HSTAT_ERROR:
        problems.append(f"ROM reported HSTAT_ERROR (payloadOut[0]={out_len})")
    elif status != HSTAT_IDLE:
        problems.append(f"status {status}, expected HSTAT_IDLE ({HSTAT_IDLE})")
    if cmd != 0:
        problems.append(f"command {cmd} not cleared, expected 0")
    if seq != 1:
        problems.append(f"sequence {seq}, expected 1")

    if problems:
        print("FAIL: handshake completed but was malformed:")
        for p in problems:
            print(f"      - {p}")
        return 1

    print(f"PASS: mailbox handshake at 0x{base:08X} "
          f"(command cleared, status IDLE, sequence {seq})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
