# Build configuration (spec §3.1, corrected for 1.16.3)

Supersedes spec §3.1. **Spec §3.2 (vanilla Gen 3 mechanics) is deleted** — see `mechanics.md`.

Status column: ✅ already correct in stock 1.16.3 · ✏️ config edit needed ·
🔧 source patch needed · ⚙️ runtime/save state, not a config header.

Nothing here is applied yet. Apply only after a green stock `make` + `make check` baseline,
so a build failure is attributable.

| Setting | Constant | Stock value | Target | Status |
|---|---|---|---|---|
| Reusable TMs OFF | `I_REUSABLE_TMS` (`config/item.h:26`) | `FALSE` | `FALSE` | ✅ |
| Fast HP drain | `B_FAST_HP_DRAIN` (`config/battle.h:323`) | `TRUE` | `TRUE` | ✅ |
| Fast intro text | `B_FAST_INTRO_PKMN_TEXT` (`config/battle.h:321`) | `TRUE` | `TRUE` | ✅ |
| Time-of-day encounters OFF | `OW_TIME_OF_DAY_ENCOUNTERS` (`config/overworld.h:95`) | `FALSE` | `FALSE` — **pin, determinism** | ✅ |
| Overworld wild encounters OFF | `WE_OW_ENCOUNTERS` (`config/wild_encounter.h`) | `FALSE` | `FALSE` — **pin**, keeps `wild_encounter.c` authoritative | ✅ |
| Skip intro slide | `B_FAST_INTRO_NO_SLIDE` (`config/battle.h:322`) | `FALSE` | `TRUE` | ✏️ |
| EV gain disabled | `B_EV_CAP_TYPE` (`config/caps.h`) | `EV_CAP_NONE` | `EV_CAP_NO_GAIN` | ✏️ |
| Wild encounters off by default | `WE_FLAG_NO_ENCOUNTER` (`config/wild_encounter.h:6`) | `0` (feature off) | a real unused `FLAG_*` | ✏️ |
| EXP gain disabled at source | — | — | patch `Cmd_getexp` region | 🔧 |
| Battle style = Set | `gBattleScripting.battleStyle` (`battle_main.c:2988`) | reads save option | force unconditionally | 🔧 |
| Instant text | `OPTIONS_TEXT_SPEED_INSTANT` (`constants/global.h:196`) | save option | set at save init | ⚙️ |
| Battle animations off | `optionsBattleSceneOff` (`global.h:602`) | `FALSE` (`new_game.c:106`) | `TRUE` under headless | ⚙️ |

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
