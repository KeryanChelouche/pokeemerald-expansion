# Build configuration (spec §3.1, corrected for 1.16.3)

Supersedes spec §3.1. **Spec §3.2 (vanilla Gen 3 mechanics) is deleted** — see `mechanics.md`.

Status: ✅ correct in stock 1.16.3, no action · **DONE** applied on `harness/m0` ·
⚙️ deferred to harness runtime (save-block state, not a config header).

| Setting | Constant | Target | Status |
|---|---|---|---|
| Reusable TMs OFF | `I_REUSABLE_TMS` (`config/item.h:26`) | `FALSE` | ✅ |
| Fast HP drain | `B_FAST_HP_DRAIN` (`config/battle.h:323`) | `TRUE` | ✅ |
| Fast intro text | `B_FAST_INTRO_PKMN_TEXT` (`config/battle.h:321`) | `TRUE` | ✅ |
| Time-of-day encounters OFF | `OW_TIME_OF_DAY_ENCOUNTERS` (`config/overworld.h:95`) | `FALSE` — **pin, determinism** | ✅ |
| Overworld wild encounters OFF | `WE_OW_ENCOUNTERS` (`config/wild_encounter.h`) | `FALSE` — **pin**, keeps `wild_encounter.c` authoritative | ✅ |
| Skip intro slide | `B_FAST_INTRO_NO_SLIDE` (`config/battle.h:322`) | `TRUE` | **DONE** |
| EV gain disabled | `B_EV_CAP_TYPE` (`config/caps.h:28`) | `EV_CAP_NO_GAIN` | **DONE** |
| EXP gain disabled | `HARNESS_DISABLE_EXP` (`config/harness.h`) | patch `BattleTypeAllowsExp` | **DONE** |
| Battle style = Set | `HARNESS_FORCE_BATTLE_STYLE_SET` (`config/harness.h`) | force, not save option | **DONE** |
| Wild encounters off by default | `WE_FLAG_NO_ENCOUNTER` (`config/wild_encounter.h:6`) | a real unused `FLAG_*` | ⚙️ |
| Instant text | `OPTIONS_TEXT_SPEED_INSTANT` (`constants/global.h:196`) | set at save init | ⚙️ |
| Battle animations off | `optionsBattleSceneOff` (`global.h:602`) | `TRUE` under headless | ⚙️ |

## How the source patches are guarded

All ROM modifications live behind `include/config/harness.h`. Setting `HARNESS_ENABLED`
to `FALSE` yields a stock expansion build, which keeps the fork merge-able with upstream
(spec §4).

`include/config/test.h` sets `HARNESS_ENABLED FALSE`, using upstream's own `#undef` /
`#define` override pattern. **This is load-bearing.** Without it, disabling EXP fails every
upstream test that asserts EXP behaviour and `make check` stops being a usable regression
gate. The suite must measure the engine, not the harness.

### Two kinds of change, and only one is gated

This distinction caused a real regression and will recur:

| Kind | Example | Gated by `HARNESS_ENABLED`? |
|---|---|---|
| Source patch routed through `harness.h` | EXP off, battle style forced | **Yes** — automatic |
| Plain upstream constant | `B_EV_CAP_TYPE`, `B_FAST_INTRO_NO_SLIDE` | **No** — applies to the test build too |

Setting `B_EV_CAP_TYPE = EV_CAP_NO_GAIN` broke two tests that assert on EV yields
(`test/battle/exp.c:153`, `test/battle/move_effect/embargo.c:94` — the former is an EXP test
whose third assertion is on `MON_DATA_HP_EV`, which makes it easy to misread as an EXP
failure). `test.h` now restores `EV_CAP_NONE` for tests.

**Rule for future config edits:** changing an ordinary upstream constant means asking
whether any test asserts on the behaviour it controls. If so, override it back in `test.h`.
Only changes behind `harness.h` are gated for free.

Consequence: the harness build and the tested build are not the same build. `make check`
green does **not** prove the harness patches are correct — only that they did not break the
engine underneath.

### Verifying a harness patch: run upstream's tests against it

The gate above also gives us a way to prove a patch *works*, without needing a smoke test
that plays a whole battle. Temporarily set `HARNESS_ENABLED TRUE` in `test.h`, run the
upstream tests that assert the behaviour being suppressed, and check they now fail. Restore
`test.h` and confirm they pass again — the control matters, or a failure for some unrelated
reason reads as success.

Done for the EXP patch:

```
# HARNESS_ENABLED TRUE  in test.h
make check TESTS="Higher leveled Pokemon give more exp"   ->  FAIL   (exit 2)
# HARNESS_ENABLED FALSE in test.h  (restored)
make check TESTS="Higher leveled Pokemon give more exp"   ->  PASS   (exit 0)
```

That is a stronger result than a scripted battle would give, and far cheaper. Note `TESTS=`
filters on the test's *name prefix*, not its filename — `TESTS="exp"` matches nothing and
reports "No tests found", which is easy to misread as success since it exits 0.

Not yet verified this way: the battle-style patch. There is no upstream test asserting Shift
behaviour to invert.

### EXP: why `BattleTypeAllowsExp`, not the award path

Spec §4.8 says to disable EXP "at source" in the `Cmd_getexp` region. The better hook is
`BattleTypeAllowsExp()` (`src/battle_script_commands.c:3819`) — a predicate whose entire
job is deciding whether EXP is awarded. Returning `FALSE` makes `Cmd_getexp` take its own
existing `getexpState = 6 // goto last case` branch, so no partial award is reachable by
construction.

Zeroing `calculatedExp` (`:3925`) instead would **not** work: the redistribution code floors
each mon's share at 1 (`if (*exp == 0) *exp = 1;`), so every participant would still gain a
point of EXP per faint.

EV gain is a genuinely separate path and needs no patch — `EV_CAP_NO_GAIN` handles it
(`src/caps.c:112`). §4.8's insistence that these are two code paths is confirmed correct.

## Still to do (⚙️)

`WE_FLAG_NO_ENCOUNTER` needs an unused `FLAG_*` assigned and set at run start. Since §4.5
triggers encounters explicitly via `HCMD_ROLL_ENCOUNTER` and the player never walks, this
is defence-in-depth rather than load-bearing.

Instant text and battle animations are save-block option bits, not config constants. They
must be written during save initialisation by the harness — M1 work, not a header edit.

## Notes on the ✏️ / 🔧 / ⚙️ rows

**EV gain.** 1.16.3 added `EV_CAP_NO_GAIN` to `include/config/caps.h`, consumed at
`src/caps.c:112`. This is a supported switch — the spec's "disabled at source" is
unnecessary for EVs. One line, no patch.

**EXP gain.** No equivalent switch exists. `B_EXP_CAP_TYPE = EXP_CAP_HARD` only gates
gain at a level cap (`src/caps.c:67`), which is not the same as disabling it and still
leaves EXP accumulating below the cap. A real patch at the `Cmd_getexp` region
(`src/battle_script_commands.c:3985`–`4073`) is required. Note this is a *separate*
code path from `MonGainEVs` — §4.8's warning is confirmed accurate for 1.16.3.

**Battle style.** Already forced to `OPTIONS_BATTLE_STYLE_SET` at
`src/battle_main.c:2989`, but only under `#if TESTING`. Either build the harness with
`TESTING` defined (drags in the whole test framework — not recommended for the live ROM)
or widen that guard to include a harness define. The spec's instruction to force it
rather than trust the save option is correct; the hook already exists.

**Wild encounter flag.** `WE_FLAG_NO_ENCOUNTER` is `0`, which in this codebase means the
feature is disabled, not that encounters are disabled. It must be assigned an unused
`FLAG_*` and that flag set at run start. Since §4.5 triggers encounters explicitly via
`HCMD_ROLL_ENCOUNTER`, and the player never walks, this is defence-in-depth rather than
load-bearing.

**Instant text / battle animations.** Neither is a `config/` constant — both are save-block
option bits. They must be written during save initialisation by the harness, not by editing
a header. `optionsBattleSceneOff` is read at `src/battle_main.c:3046`.

## Dropped from spec §3.1

**"Fast AI calc"** — no such config exists in 1.16.3. The only related constant is
`DEBUG_AI_DELAY_TIMER` (`config/debug.h:12`), which *displays* AI decision time rather than
reducing it. Row removed; if AI latency proves to matter under fast-forward, it needs
profiling, not a flag.

## Opportunities the spec did not anticipate

`include/config/wild_encounter.h` provides ROM-level enforcement for two referee rules,
as defence-in-depth behind the §8 referee (which remains authoritative per invariant 6):

- `WE_FLAG_NO_CATCHING` — a flag that disables catching entirely. Complements R6 /
  `encounterCatchAllowed`: the referee can clear it only for a valid first encounter.
- `WE_FLAG_NO_RUNNING` — disables fleeing, and makes Roar/Whirlwind/Teleport fail. Relevant
  to R3 (a flee burns the location) — if fleeing is impossible, one branch of R3 is
  unreachable by construction.

Both default to `0` (off). Adopting them is optional and must not move rule enforcement out
of the referee.
