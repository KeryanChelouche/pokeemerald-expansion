# Spec deltas — v1.1 → v1.1a

Two decisions, taken in order:

1. **The `@smogon/calc` service is removed.** (Reverses v1.0 → v1.1's central change.)
2. **The calc charge economy is dropped entirely** — not rebuilt on the ROM's own
   calculator.

Consequence of (1) alone: no second calculator, so no parity requirement, so §3.2's
Gen 3 pinning is unnecessary. See `mechanics.md`.

Consequence of (2): all agent tools are free, and every section that priced, budgeted,
cached, logged or measured charges is void.

This file is the authoritative list of what that touches. The spec document itself is not
edited; read it alongside this.

---

## Deleted outright

| Section | Content |
|---|---|
| §3.2 | Vanilla Gen 3 mechanics table |
| §10 | Calc service — library, inputs, outputs |
| §10.4 | Differential test, methods A and B |
| §15 #1 | Calc charge starting pool and per-badge increment |
| §15 #2 | `calc_scope` and `cache_invalidation` final values |

## Invariants (§1.2)

| # | Was | Now |
|---|---|---|
| 1 | ROM pinned to Gen 3; calc verified equivalent by §10.4 | **Void.** No second calculator exists. |
| 3 | Calc service MUST be fed the ROM's computed stats, not IVs/EVs/nature | **Reframed, not void.** Nothing is fed to a calculator, but §4.9's serializer and `team_state()` MUST still emit all six computed stat values — that is what the agent reasons from. The requirement survives; its justification changes. |
| 4 | One emulator instance during an attempt | Unchanged, and now *easier*: the differential test was one of the three offline consumers §3.4 had to keep out of the live path. |

Invariants 2, 5, 6, 7, 8 unaffected.

## §3.3 Toolchain — substituted

The spec calls for the standard pret build (devkitARM/agbcc). Two corrections:

- **agbcc is gone.** Deprecated since expansion 1.9 (`Makefile:341`); the build is
  modern-only. `make agbcc` prints a deprecation notice and does nothing.
- **devkitARM was unreachable** — `apt.devkitpro.org` returns Cloudflare 403 from this
  environment, so the official installer cannot run. Ubuntu's `gcc-arm-none-eabi` 13.2.1
  is used instead via `TOOLCHAIN=/usr`, which is the override `INSTALL.md:102` documents.

**Verified equivalent for our purposes:** `make check` passes on this toolchain — 4887
passed, 13 known-failing, exit 0. The 13 are upstream-acknowledged, not toolchain damage.

`make compare` against `rom.sha1` will never match, but that is true of any `MODERN=1`
expansion build and is unrelated to the substitution. Our ROM identity is its own sha1,
recorded in `harness_symbols.json` and the ledger.

## §3.4 Emulator — rearchitected

The spec requires "mGBA ≥ 0.10 with Lua scripting, headless under `xvfb`". **The premise is
wrong: no released mGBA can do this.**

Lua scripting exists in 0.10, but only through the Qt frontend's GUI scripting window.
There is no `--script` flag in the SDL frontend in 0.10.2 (Ubuntu), 0.10.5 (latest release),
or current master. Verified empirically rather than from documentation: mGBA 0.10.2 ran the
built ROM headless under xvfb for 25 s without ever executing a probe script passed via
`-C script=`.

**Resolution: `mgba-headless`.** mGBA master ships a dedicated headless frontend
(`src/platform/headless-main.c`) taking `--script FILE`, repeatable. It is gated behind
`BUILD_HEADLESS=OFF`, which is why no distro package contains it. Built from source with:

```
cmake .. -DBUILD_HEADLESS=ON -DENABLE_SCRIPTING=ON -DBUILD_QT=OFF -DBUILD_SDL=OFF \
         -DUSE_LUA=ON -DUSE_LIBZIP=OFF -DUSE_MINIZIP=OFF -DUSE_FFMPEG=OFF
make mgba-headless
```

`USE_LIBZIP=OFF` works around a broken `libzip-targets.cmake` in Ubuntu's `libzip-dev`;
zip support is irrelevant for raw `.gba` files.

This is **better** than what §3.4 specified. The frontend is headless by construction, not
by suppression — no X server, no `xvfb`, no video pipeline to disable. Drop `xvfb` from the
live run path; `render/` still needs a real frontend for §12's 1× capture.

**Cost: it is unreleased code.** Pinned to mGBA master
`c034660f007c543233f1cadeb0ca13c71afd8f41`. That hash is part of the reproducibility tuple
and MUST be recorded in the ledger header alongside `rom_hash` — §12's replay guarantee is
meaningless if the emulator drifts.

Record the **full** SHA, not the abbreviated form. `git fetch --depth 1 origin <sha>` will
not accept an abbreviated hash, so a short pin cannot be restored directly and has to be
recovered by fetching history and searching for it.

Build it outside any ephemeral scratch directory (this project uses `/root/mgba-src`). A
pruned scratch directory silently removes the emulator, and every test then reports "no
output" — which looks exactly like a ROM hang rather than a missing binary.

Verified working end to end: script loads, `callbacks:add("frame", ...)` fires, and
`emu:read8/read32` return correct data from the running ROM.

## §4.7 / §14.1 Input injection — requirement relaxed

§14.1 requires the battle path to run with "no dialogue, no overworld interaction, no input
injection". **The intent is narrower than the wording:** what must not happen is the *agent*
making micro-navigation decisions. The agent chooses "use Tackle", never "press A, press
right, press A". How the harness realises that choice internally is unconstrained.

Synthetic keypresses are therefore permitted, and are used for two things:

- advancing battle messages and menus;
- confirming a choice the agent has already made.

This is not a workaround — it is materially **more correct** than the alternative. The first
implementation bypassed the menus and emitted `B_ACTION_EXEC_SCRIPT` directly, which meant
re-deriving move targeting outside the engine. It got it wrong: moves resolved against the
user instead of the opponent, and the ROM reported a perfectly well-formed battle while the
opponent took no damage for five consecutive turns. Target selection depends on move target
type, gimmick state, doubles layout and ally liveness — all of which
`HandleInputChooseMove` already handles.

The hook now preselects `gMoveSelectionCursor` to the agent's chosen slot and lets the
game's own selection code run. Shorter, and correct by construction.

**The invariant that actually matters** is the one §4.7 states: the agent MUST receive an
enumerated list of legal actions and choose from it. That is unchanged, and it is what the
decision request enforces. Keypresses are an implementation detail beneath it.

### The keypress driver is NOT the determinism problem

An earlier version of this section blamed the A-pulse driver for run-to-run divergence. That
was wrong. The pulse is keyed off the emulator's frame counter (`n % 16`), so it fires on
identical frames every run; there is no jitter for RNG to amplify.

The real cause is below. Retained here because the wrong diagnosis is the tempting one.

## §12 Determinism — the RNG is seeded from the real-time clock

**Runs of the same ROM with the same inputs diverge.** Measured: two byte-identical runs of
one doubles battle agreed exactly through turn 1 — same decision frames, same state — then
diverged, finishing in 4 and 10 decisions.

The cause is `SeedRngWithRtc()` (`src/main.c:242`), called at boot from `main.c:109`:

```c
seconds = ((HOURS_PER_DAY * RtcGetDayCount(&rtc) + BCD8(rtc.hour))
        * MINUTES_PER_HOUR + BCD8(rtc.minute)) * SECONDS_PER_MINUTE + BCD8(rtc.second);
SeedRng(seconds);
```

The emulator takes the RTC from the host clock, so the seed differs on every run. Note the
upstream comment: *"FRLG commented this out to remove RTC, however Emerald didn't undo
this!"* — it is live because `BUGFIX` is defined in `config/general.h`.

**Consequences:**

- `HCMD_SET_SEED` (§5.1) is not optional convenience — it is what makes §12 possible at all.
  The harness MUST issue it after boot and before anything consumes randomness, and MUST
  record the seed in the ledger. Implemented via `SeedRng`.
- The RTC remains a latent input beyond the RNG: `GetTimeOfDay()` reads it. Encounter tables
  are insulated only because `OW_TIME_OF_DAY_ENCOUNTERS` is `FALSE` (see `config.md`, where
  that row is pinned). If that ever flips, wall-clock time re-enters the replay tuple.
- The replay tuple in §12 is therefore `(rom_hash, mgba_commit, seed, decision_log)`. The
  emulator commit is listed for the reason given under §3.4; the seed for this one.

**Verified.** With `set_seed` issued after boot and before the party is built, two runs of
the same doubles battle are frame-exact:

```
run 1: f1693 f1731 f2515 f2548 f3073 f3108 f3659  outcome=1 dec=6
run 2: f1693 f1731 f2515 f2548 f3073 f3108 f3659  outcome=1 dec=6
```

Same decision frames, same state at each, same outcome. Without `set_seed` the same battle
diverged into 4 and 10 decisions. Frame-exact reproducibility is the precondition for §12's
bit-identical replay, so M2 can now be built on it.

A caution on testing this: a comparison run is only meaningful if the ROM is unchanged for
its whole duration. An earlier attempt overlapped a rebuild, so the two halves ran different
ROMs and its "identical" verdict was worthless. Rebuild first, then compare.

## §2 Components

- `calc-service/` — deleted.
- `harness/economy/` — was "calc charges, matchup cache, empirical log". Charges and the
  matchup cache are gone; only the empirical log survives. Fold it into `harness/agent/`
  or rename to `harness/empirical/`. There is no economy left to name a package after.

## §4 ROM modifications

- `HCMD_CALC_TESTONLY` / `Harness_CalcTestOnly` — removed from the §4.3 dispatch table.
  It existed only for §10.4.
- §4.9 serializer — **unchanged**, including all six computed stats (see invariant 3 above).
- §4.7 battle event emission — unchanged. The events fed the ledger, the empirical log and
  the differential test; the first two remain.

## §5 Wire protocol

- `calc_testonly` — removed from the §5.1 command table.

## §8.1 Referee state

- `charges: int` — removed.

Nothing else in referee state was calc-derived. §8.2 duties are unchanged: no duty
mentioned charges.

## §9.4 Tools

The table collapses to its free rows. Removed: the `calc` row, the cache paragraph, the
budget paragraph, the `calc_scope` config, the `cache_invalidation` config, and the
"one charge = one target" definition.

Retained, and now more load-bearing than before:

> The agent MAY assert unpaid numeric estimates; the harness MUST log every numeric claim
> for post-hoc comparison and MUST NOT block it.

With nothing purchasable, **every** numeric claim the agent makes is unpaid and unverified
at decision time. §13.3's "accuracy of unpaid numeric assertions" stops being a side metric
and becomes the primary read on whether the agent's damage reasoning is any good.

## §9.3 Context assembly

Item 3 lists "charges remaining" among the always-present context. Remove it. Items 1, 2,
4, 5, 6 unchanged — note item 4's "empirical log entries relevant to the present matchup"
is now the agent's only quantitative input beyond raw stats.

## §11 Ledger

- `calc_spend` event type — removed.
- `charges_left` field — removed from `decision_request` and from `calc_spend`'s siblings.
- `avoidable` field — removed from the `death` event, along with the rule that it MUST be
  null at write time (see §13.3).
- `node` and `location` — **added** to the `death` event (see §13.3).

All other event types unchanged. The ledger is not otherwise reduced: it still drives
replay verification (§12) and the footage overlay, which serve objective A and are
independent of what the evaluation reports.

## §12 Replay, footage, offline analysis

- Overlay track (`render/`) listed "charges remaining" as an overlay element. Remove it.
- **Post-hoc avoidability — removed.** Dropping the calc initially made this *more*
  expensive: with no cheap one-turn path, every death would have needed an offline emulator
  rollout. The §13.3 decision below removes `avoidable-death rate`, its only consumer, so
  the whole avoidability analysis is cut rather than paid for. See "This resolves the
  avoidability problem" under §13.3.

## §13.3 Metrics — replaced

Third decision: **post-run evaluation is streamlined to two numbers.**

> Badges obtained. Deaths — how many, and where.

The entire §13.3 table is replaced by that. Removed from **Mechanical**: attempts to first
completion, deaths per badge, avoidable-death rate, illegal actions proposed, cost and
latency per badge, standing-order coverage. Removed entirely: the **Economy** block (already
half-gone with the charge economy) and the **Narrative** block — dossier reference rate,
plan adherence, stated-intent consistency.

"Deaths per badge" is not retained as a metric but is trivially recoverable from the two
numbers if wanted; there is no need to compute it during a run.

Human review of rendered footage remains the instrument for objective A, as §13.3 already
said. It is a judgement, not a metric, and is unaffected by this.

### This resolves the avoidability problem

`avoidable-death rate` was the sole consumer of post-hoc avoidability. With it gone:

- §12's *"For each death, determine whether a legal alternative existed that survived with
  p > 0.9"* — **removed**. This was going to require an offline emulator rollout per death
  once the calc service was dropped, which was the most expensive open consequence in this
  document. It is now moot.
- §11's `avoidable` field, and the rule that it *"MUST be null at write time and filled only
  by the post-hoc analyser"* — **removed** from the death event.
- `harness/eval/`'s post-hoc analyser — reduced to tallying the ledger. The offline emulator
  rollout path it needed is no longer required, which also removes the last routine consumer
  of a second emulator instance (§3.4, invariant 4).

### One gap: "where" is not currently in the death record

§11's `death` event carries `caught_at` (where the Pokémon was *caught*) but no field for
where it *died*. The node is available on the surrounding `decision_request` events and so
is recoverable by scanning backwards, but that is fragile for something now half of the
entire evaluation.

**Add `node` and `location` to the `death` event** so it is self-contained:

```json
{"f": 186112, "t": 1419, "type": "death", "mon_uid": "a3f1", "nickname": "Pylon",
 "species": "SPECIES_MANECTRIC", "caught_at": "MAPSEC_ROUTE_110",
 "node": "gym03_wattson", "location": "MAP_MAUVILLE_CITY_GYM",
 "turns_alive": 4021, "cause": "MOVE_SHOCK_WAVE",
 "circumstance": "designated_sacrifice", "held_tms": ["TM24_THUNDERBOLT"]}
```

`cause` and `circumstance` are retained — not for metrics, but because §9.5 requires event
notifications to vary by circumstance, and §9.2's graveyard store records both.

### §13.1 / §13.2 unchanged for now

The fixed suite and the baselines measure agent quality *before* a run; this decision was
about what a completed run reports. They are untouched, and M5 still stands. If the
intent was to cut those too, say so — the fixed suite is ~30 hand-built positions and is
the single largest piece of work in the evaluation harness.

## §14 Milestones

**M3** was: *Calc service, differential test methods A and B, documentation pack, calc
economy, empirical log.* Its acceptance test was *differential test green; charge accounting
exact.*

What remains is the **documentation pack (§8.3)** and the **empirical log**. Neither is
large, and both are prerequisites for M4 rather than a milestone in their own right. M3
should be dissolved into M2 (documentation pack — the campaign loader already needs it for
constant resolution) and M4 (empirical log — an agent memory store per §9.2).

The spec's closing line *"M3 now gates the agent on differential-test success. Do not build
M4 against an unverified calculator."* is void. **M4 is no longer gated.** With M0–M2 done,
the agent can be built directly.

M0, M1, M2, M5, M6, M7 unaffected.

---

## What this does not change

The removals are all downstream of the oracle. The parts of the design that produce the
run — the referee and ruleset (§6, §8), the campaign table (§7), the agent's identity,
memory and prompt constraints (§9.1–9.3, §9.5–9.9), replay determinism (§12), and the
narrative objective A — are untouched.

The one design claim that is now untested is §9.4's argument that scarcity produces
interesting play. Nothing in v1.1a is scarce except locations, TMs, and lives. That may be
enough; it is no longer a question the harness can answer, because there is no charge budget
to instrument.
