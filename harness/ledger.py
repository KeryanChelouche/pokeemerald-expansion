#!/usr/bin/env python3
"""Append-only run ledger (spec §11).

One JSONL file per attempt. Every record carries the emulator frame `f`, and a
turn `t` where one applies, because frame indexing is what lets a replay be
checked against the original and what an overlay track would sync to (§12).

Append-only is the point: records are written as they happen and never revised.
A run that crashes still leaves everything up to the crash, and "what did it know
when it decided that" stays answerable.

The header pins the replay tuple. Determinism was measured to depend on the ROM,
the seed, and the emulator build -- the ROM seeds its RNG from the real-time
clock, so without an explicit seed two runs of the same ROM diverge (see
campaign/docs/spec-deltas.md §12). All three go in the header so a ledger is
self-describing rather than only meaningful next to the checkout that made it.
"""

import json
import pathlib
import subprocess
import time


def _mgba_commit(mgba: pathlib.Path) -> str | None:
    """Best-effort emulator build id. Part of the replay tuple: mgba-headless is
    built from an unreleased commit, so a ledger that omits it cannot be trusted
    to replay identically."""
    src = mgba.resolve().parent.parent
    try:
        out = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Ledger:
    def __init__(self, path: pathlib.Path, *, rom_sha1: str, seed: int,
                 attempt: int, trainer: str, mgba: pathlib.Path | None = None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("a", encoding="utf-8")
        self.write(0, "run_start",
                   rom_sha1=rom_sha1,
                   mgba_commit=_mgba_commit(mgba) if mgba else None,
                   seed=f"0x{seed:08X}",
                   attempt=attempt,
                   trainer=trainer,
                   started=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def write(self, frame: int, kind: str, *, turn: int | None = None, **fields):
        rec = {"f": frame, "type": kind}
        if turn is not None:
            rec["t"] = turn
        rec.update(fields)
        # Flushed per record: a run that dies mid-battle should still have
        # everything up to the death on disk.
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()

    def close(self, frame: int, cause: str, **fields):
        self.write(frame, "run_end", cause=cause, **fields)
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self.fh.closed:
            self.fh.close()


def summarise(path: pathlib.Path) -> dict:
    """Reduce a ledger to the numbers the evaluation actually reports.

    Post-run evaluation was streamlined to badges obtained and deaths with
    locations (campaign/docs/spec-deltas.md §13.3), so this deliberately computes
    those and not a wider set that nothing consumes.
    """
    fights_won, deaths, caught = 0, [], []
    end = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        kind = rec["type"]
        if kind == "fight_end" and rec.get("result") == "won":
            fights_won += 1
        elif kind == "death":
            deaths.append(rec)
        elif kind == "catch":
            caught.append(rec)
        elif kind == "run_end":
            end = rec
    return {"fights_won": fights_won, "deaths": deaths,
            "caught": caught, "run_end": end}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Summarise a run ledger.")
    ap.add_argument("ledger", type=pathlib.Path)
    args = ap.parse_args()

    s = summarise(args.ledger)
    print(f"fights won : {s['fights_won']}")
    print(f"caught     : {len(s['caught'])}")
    for c in s["caught"]:
        print(f"   {c['nickname']:<12} {c['species']:<12} at {c['location']}")
    print(f"deaths     : {len(s['deaths'])}")
    for d in s["deaths"]:
        print(f"   {d['nickname']:<12} {d['species']:<12} "
              f"Lv{d['level']:<3} caught {d['caught_at']}, died at {d['died_at']}"
              f"  (f{d['f']})")
    if s["run_end"]:
        print(f"run end    : {s['run_end']['cause']} (f{s['run_end']['f']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
