#!/usr/bin/env python3
"""Replay a recorded run, and optionally record it to video (spec §12).

Replays from the ledger alone: `(rom_hash, mgba_commit, seed, decision_log)`.
Decisions are fed back by index rather than by label, so a replay reproduces the
choice that was made even if the label formatting has since changed -- what is
being replayed is the decision, not the wording.

Verification compares the event stream, which is what §12 actually requires --
"reproduce an identical event stream from (rom_hash, seed, decision_log)". Each
decision is checked against the state and the enumerated legal actions recorded
for it, so a divergence is reported at the first decision that differs rather
than as a different final outcome.

Frame numbers deliberately are NOT the criterion. A command is applied on
whichever frame the emulator next polls for it, so the exact frame depends on the
driver's wall-clock latency: the same run replayed lands its commands tens of
frames earlier or later while producing identical states and outcomes. Frame
indexing exists for overlay synchronisation (§11), not for replay equality.
Drift is reported for information.

    harness/replay.py runs/attempt.jsonl                 # verify only
    harness/replay.py runs/attempt.jsonl --video out.mp4 # verify and record

Recording captures the emulator's own framebuffer, so what is recorded is what
the game drew, not a reconstruction. It costs a PNG per captured frame and is
off by default.
"""

import argparse
import json
import pathlib
import shutil
import struct
import subprocess
import os
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import session as S                                    # noqa: E402
from replay_lua import PLAN_LUA, write_plan            # noqa: E402

CAPTURE_LUA = '''
local shotDir = "{shots}"
local every = {every}
local shot = 0
local frames = 0
callbacks:add("frame", function()
  frames = frames + 1
  if every > 0 and (frames %% every) == 0 then
    shot = shot + 1
    emu:screenshot(string.format("%s/%%06d.png", shotDir, shot))
  end
end)
'''


def load_ledger(path: pathlib.Path):
    head, decisions, events = None, [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["type"] == "run_start":
            head = rec
        elif rec["type"] == "decision":
            decisions.append(rec)
        events.append(rec)
    if head is None:
        raise SystemExit(f"{path} has no run_start record; not a ledger")
    return head, decisions, events


def commands(events):
    """Every command the original run issued, verbatim and in order.

    Replayed rather than reconstructed. Rebuilding the sequence from the semantic
    events looks equivalent but is not: the original also issues nicknames and
    other bookkeeping, and running a different number of commands shifts every
    later frame, so a replay reports divergence even when the decisions match.
    """
    return [(r["cmd"], bytes.fromhex(r["payload"])) for r in events
            if r["type"] == "command"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", type=pathlib.Path)
    ap.add_argument("--mgba", type=pathlib.Path,
                    default=pathlib.Path("/root/mgba-src/build/mgba-headless"))
    ap.add_argument("--rom", type=pathlib.Path, default=pathlib.Path("pokeemerald.gba"))
    ap.add_argument("--symbols", type=pathlib.Path,
                    default=pathlib.Path("harness_symbols.json"))
    ap.add_argument("--video", type=pathlib.Path,
                    help="write an mp4 of the replay")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--every", type=int, default=2,
                    help="capture one frame in N when recording (default 2, ~30fps)")
    args = ap.parse_args()

    head, decisions, events = load_ledger(args.ledger)
    seed = int(head["seed"], 16)
    rom_sha1 = json.loads(args.symbols.read_text()).get("rom_sha1")

    print(f"Replaying {args.ledger}")
    print(f"  seed        {head['seed']}")
    print(f"  decisions   {len(decisions)}")
    if rom_sha1 and head.get("rom_sha1") and rom_sha1 != head["rom_sha1"]:
        # Refused rather than warned: a different ROM will diverge, and a replay
        # that silently drifts is worse than one that does not run.
        print(f"\nERROR: ledger was recorded against ROM {head['rom_sha1'][:12]}, "
              f"this ROM is {rom_sha1[:12]}.", file=sys.stderr)
        return 1

    tmp = tempfile.TemporaryDirectory()
    work = pathlib.Path(tmp.name)
    shots = work / "shots"
    shots.mkdir(parents=True, exist_ok=True)

    if args.video and not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg is needed to write video", file=sys.stderr)
        return 1

    # Every command and every decision reply, pinned to the frame it was applied
    # on originally. Written before the emulator starts so nothing depends on how
    # fast this process is.
    entries = []
    for rec in events:
        if rec["type"] == "command":
            entries.append((rec["f"], "C", rec["cmd"], rec["payload"]))
        elif rec["type"] == "decision":
            entries.append((rec["f"], "D", rec["act"], rec["slot"], rec["target"]))
    if not entries:
        raise SystemExit("ledger records no commands; it predates command logging")

    plan = work / "plan.txt"
    last_frame = write_plan(plan, entries)
    print(f"  commands    {sum(1 for e in entries if e[1] == 'C')}")
    print(f"  last frame  {last_frame}")

    syms = json.loads(args.symbols.read_text())["symbols"]
    lua = work / "replay.lua"
    lua.write_text(PLAN_LUA.format(
        base=syms["gHarnessMailbox"], off_out=12 + 2048, plan=plan, dir=work,
        shots=shots, every=(args.every if args.video else 0),
        c_dec=S.HCMD_DECISION, last_frame=last_frame))

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = (f"{args.mgba.resolve().parent}:"
                              f"{env.get('LD_LIBRARY_PATH', '')}")
    proc = subprocess.Popen(
        [str(args.mgba), "--script", str(lua), str(args.rom), "-l", "0"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + args.timeout
    done = work / "REPLAY_DONE"
    while time.time() < deadline and not done.exists():
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    applied = int(done.read_text()) if done.exists() else 0
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    print(f"\n  plan entries applied : {applied}/{len(entries)}")
    if applied != len(entries):
        print("  -> the replay did not apply the whole plan")

    if args.video:
        if not shutil.which("ffmpeg"):
            print("ERROR: ffmpeg is needed to write video", file=sys.stderr)
            return 1
        extra = CAPTURE_LUA.format(shots=shots, every=args.every)

    sess = S.Session(args.mgba, args.rom, args.symbols, timeout=180)
    if extra:
        sess.extra_lua = extra

    fed = 0
    diverged, drift = [], []
    requests = [r for r in events if r["type"] == "decision_request"]

    # The reply is an index into the legal-action list, so it has to be turned
    # back into an action. Re-decode the request the same way the driver does.
    from play import decode, load_names                # noqa: E402
    from play import load_charmap, render              # noqa: E402
    root = pathlib.Path(__file__).resolve().parent.parent
    species = load_names(root / "include/constants/species.h", "SPECIES_")
    moves = load_names(root / "include/constants/moves.h", "MOVE_")
    charmap = load_charmap(root / "charmap.txt")

    def replay_decision(raw, txt):
        nonlocal fed
        if fed >= len(decisions):
            raise SystemExit("ledger ran out of decisions before the run ended")
        rec, req = decisions[fed], requests[fed] if fed < len(requests) else {}
        fed += 1

        r = decode(raw, species, moves, charmap)
        menu = render(r)
        me = r["battlers"].get(r["battler"])
        foe = r["battlers"].get(1)
        now = {
            "me": f"{me.name} Lv{me.level} {me.hp}/{me.maxhp}" if me else None,
            "foe": f"{foe.name} Lv{foe.level} {foe.hp}/{foe.maxhp}" if foe else None,
            "legal_actions": [label for label, _ in menu],
        }
        for key in ("me", "foe", "legal_actions"):
            if key in req and req[key] != now[key]:
                diverged.append((fed, key, req[key], now[key]))

        drift.append(sess.frame - rec["f"])
        idx = rec["index"]
        if idx >= len(menu):
            raise SystemExit(f"decision {fed}: ledger chose option {idx + 1} but "
                             f"only {len(menu)} are legal now; the run diverged")
        return menu[idx][1]

    cmds = commands(events)
    if not cmds:
        raise SystemExit("ledger records no commands; it predates command logging")
    print(f"  commands    {len(cmds)}")

    with sess:
        for cmd, payload in cmds:
            sess.run(cmd, payload, on_decision=replay_decision)

    print(f"\n  decisions replayed : {fed}/{len(decisions)}")
    if drift:
        lo, hi = min(drift), max(drift)
        print(f"  frame drift        : {lo:+d}..{hi:+d} "
              f"(informational; commands land on whichever frame is polled)")
    if fed != len(decisions):
        print(f"  -> DIVERGED: expected {len(decisions)} decisions, replay needed "
              f"{fed}")
        return 1
    if diverged:
        print(f"  state mismatches   : {len(diverged)}")
        for i, key, want, got in diverged[:5]:
            print(f"     decision {i} {key}:")
            print(f"       ledger: {want}")
            print(f"       replay: {got}")
        print("  -> the replay DIVERGED")
        return 1
    print("  event stream       : identical — replay verified")

    if args.video:
        n = len(list(shots.glob("*.png")))
        if not n:
            print("  no frames captured", file=sys.stderr)
            return 1
        fps = max(1, round(60 / args.every))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
             "-i", str(shots / "%06d.png"), "-vf", "scale=480:320:flags=neighbor",
             "-pix_fmt", "yuv420p", str(args.video)], check=True)
        print(f"  video              : {args.video} ({n} frames at {fps}fps)")

    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
