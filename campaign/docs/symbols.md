# Symbol verification (spec §16)

Verified against this checkout: **pokeemerald-expansion 1.16.3**
(`include/constants/expansion.h`, `EXPANSION_TAGGED_RELEASE FALSE` — untagged, contains
unreleased changes).

The spec was written against ≥1.14. Four symbols it names no longer exist. Do not reintroduce
them; use the replacements below.

## Renamed / restructured

| Spec §16 name | Replacement in 1.16.3 | Location |
|---|---|---|
| `gTrainerBattleOpponent_A` | `TRAINER_BATTLE_PARAM.opponentA` | `include/battle_setup.h:51` |
| `gTrainerBattleOpponent_B` | `TRAINER_BATTLE_PARAM.opponentB` | `include/battle_setup.h:51` |
| `GenerateWildMon` | `TryGenerateWildMon(info, area, flags)` → `CreateWildMon(species, level)` | `src/wild_encounter.c:480`, `:466` |
| `ChooseWildMonIndex_WaterRock` | `ChooseWildMonIndex_Water()` / `ChooseWildMonIndex_Rocks()` | `src/wild_encounter.c:220`, `:247` |

`TRAINER_BATTLE_PARAM` is a macro for `gTrainerBattleParameter.params`, not a bare global.
§4.4's assignment snippet must be rewritten accordingly.

## Confirmed present

| Symbol | Location | Note |
|---|---|---|
| `BattleSetup_StartTrainerBattle` | `src/battle_setup.c:1306` | Sets `gBattleTypeFlags` itself from `gNoOfApproachingTrainers` and follower-partner state. §4.4 must account for this — it does not simply consume flags the caller set. |
| `BattleSetup_StartWildBattle` | called `src/wild_encounter_ow.c:409` | |
| `CheckForTrainersWantingBattle` | `src/trainer_see.c:438` | Overworld path, bypassed |
| `ScrCmd_trainerbattle` | `src/scrcmd.c:2452` | Script path, bypassed |
| `gWildMonHeaders` | indexed `[headerId].encounterTypes[timeOfDay].<area>MonsInfo` | **Shape changed:** now time-of-day keyed. See `mechanics.md`. |
| `ChooseWildMonIndex_Land` | `src/wild_encounter.c:179` | |
| `ChooseWildMonIndex_Fishing(rod)` | `src/wild_encounter.c:274` | static; fishing entry is `TryGenerateWildMon`-adjacent at `:540` |
| `HealPlayerParty` | used `src/union_room.c:1580` | HP + status + PP, satisfies `HCMD_HEAL` |
| `CalculateMonStats` | used `src/daycare.c:343` | |
| `GetEvolutionTargetSpecies` | used `src/party_menu.c:5851` | Signature is now `(mon, mode, item, NULL, &canStopEvo, CHECK_EVO)` — six args, not one. |
| `SetWarpDestination` | used `src/field_specials.c:985` | |
| `WarpIntoMap` | used `src/cable_car.c:390` | |
| `gMapHeader.regionMapSectionId` | `src/overworld.c:670` | |
| `MON_DATA_MET_LOCATION` | `src/pokemon.c:2243` (get), `:2758` (set) | Compared against `GetCurrentRegionMapSectionId()` at `src/pokemon.c:5043` — that accessor is what smoke test B should assert against |

## Resolved ambiguities

**Level-up learnset accessor** (§4.8) — `GetLevelUpMovesBySpecies(species, u16 *moves)`
at `src/pokemon.c:5204`, declared `include/pokemon.h:880`. Returns a count and fills the
caller's buffer. This is what `HCMD_SET_LEVEL` returns from. Related:
`MonTryLearningNewMoveAtLevel(mon, firstMove, level)` at `src/pokemon.c:1654` is the
normal-path variant that `SetMonData(MON_DATA_LEVEL)` bypasses.

**Player battle controller action selection** (§4.7) — three hook points, all in
`src/battle_controller_player.c`:

| Function | Line | Role |
|---|---|---|
| `HandleInputChooseAction` | 234 | Fight / Bag / Pokémon / Run |
| `HandleInputChooseMove` | 692 | Move slot |
| `HandleInputChooseTarget` | 413 | Doubles targeting |

Dispatch is table-driven via `gBattlerControllerFuncs[battler]` and
`[CONTROLLER_CHOOSEACTION] = PlayerHandleChooseAction` (line 118). The cleanest hook is
replacing the table entries rather than patching input reads inside each function.

**EXP award path** (§4.8) — `src/battle_script_commands.c` around `:3985`–`:4073`
(`Cmd_getexp` region), writing via `SetMonData(MON_DATA_EXP)`. Player-side level-up display
lives separately at `src/battle_controller_player.c:1431`–`1539`.

**EV award path** (§4.8, "separate paths" — confirmed separate) — `MonGainEVs(mon,
defeatedSpecies)` at `src/pokemon.c:5049`, called from `src/battle_script_commands.c:3986`
and `:4073`. Unlike EXP, this one has a supported config switch; see `config.md`.

**Damage calculation entry point** (§10.4 Method A) — `CalculateMoveDamage(struct
DamageContext *ctx)` at `src/battle_util.c:8031`, declared `include/battle_util.h:245`.
Takes a context struct, not loose arguments, so any test-only command must construct a
`DamageContext`. Currently unused: the calc service is dropped from v1.1a.

**Debug menu warp** (reference implementation) — `DebugAction_Util_Warp_Warp` at
`src/debug.c:1422`, with map-group / map / warp selection at `:1452` onward. Note
`DEBUG_OVERWORLD_MENU` is `DISABLED_ON_RELEASE`, so this is not present in `make release`
builds.

## New in 1.16 — not in the spec

**Runtime config system.** `include/config_changes.h:23` defines
`GetConfig(name)` → `GetConfigInternal(CONFIG_##name)`, with a matching
`SetConfig(tag, value)`. Battle, Pokémon and AI configs are mutable at runtime and stored
in a `struct ConfigChanges` bitfield. Battle code reads through it — e.g.
`GetConfig(B_BADGE_BOOST)` at `src/battle_util.c:8917`.

Consequence for §12 replay determinism: **runtime config state is part of the replay tuple.**
`(rom_hash, seed, decision_log)` is no longer sufficient unless the harness guarantees it
never calls `SetConfig`. Either forbid `SetConfig` on the live run path, or add a config
snapshot to the ledger header.

**Wild encounter code is forked.** `src/wild_encounter.c` (43 KB) and
`src/wild_encounter_ow.c` (70 KB, overworld visible encounters) both reach
`TryGenerateWildMon`. §4.5 must name which one `Harness_RollEncounter` drives.
`WE_OW_ENCOUNTERS` is `FALSE` by default, which keeps the `wild_encounter.c` path
authoritative — pin it.
