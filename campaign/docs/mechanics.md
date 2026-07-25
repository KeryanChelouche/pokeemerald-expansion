# Mechanics — deviations from vanilla Emerald

Spec §3.2 required the ROM be pinned to vanilla Gen 3 mechanics. **That requirement is
deleted.** Its sole justification was parity with the external `@smogon/calc` service
(invariant 1), and the calc service is removed from v1.1a. With no second calculator to
agree with, there is nothing for Gen 3 to be correct *against*.

The run therefore uses **pokeemerald-expansion 1.16.3 stock mechanics**: `GEN_LATEST = GEN_9`
(`include/config/general.h:73`), which is the default for all 216 `GEN_LATEST` flags in
`include/config/battle.h` and 27 in `include/config/pokemon.h`.

## What this means

The battle system is modern, not Gen 3. Non-exhaustively: physical/special split is on,
the Fairy type is live with Gen 6+ matchups, crits are 1.5× rather than 2×, updated move
power/accuracy/type/flag data, Gen 5+ status and binding durations, dynamic speed,
Gen 4+ multi-target damage reduction, abilities and items through Gen 9.

This is a deviation from vanilla Emerald in aggregate rather than a list of rows. Trainer
rosters, movesets and AI remain vanilla Emerald (spec §3); only the mechanics resolving them
are modern. **Consequence for §7:** a walkthrough-authored campaign remains valid for node
*order* and trainer *identity*, but not for difficulty. Matchups that were safe in vanilla
may not be, and vice versa.

## Deviations that remain deliberate

| Deviation | Setting | Reason |
|---|---|---|
| EV gain disabled | `B_EV_CAP_TYPE = EV_CAP_NO_GAIN` | Determinism (spec §3.1). Removes an uncontrolled stat input. |
| EXP gain disabled | source patch, `Cmd_getexp` | Levels are set explicitly by the harness (§4.8). Makes over-cap Pokémon impossible. |
| Badge stat boosts | none needed | Already off: `B_BADGE_BOOST` defaults to `GEN_LATEST`, and `src/battle_util.c:8917` applies boosts only when `<= GEN_3`. The spec listed this as a deliberate deviation for calc parity; it is now simply the stock default, requiring no action. |
| Battle style forced to Set | source patch, `battle_main.c:2988` | Rule surface (§4.7): switch actions must be absent after an opponent faint. |

Badge boosts are worth restating because the spec's reasoning inverted under this change.
Had §3.2 been implemented as written — flipping `GEN_LATEST` to `GEN_3` — it would have
*re-enabled* badge boosts, and the "badge boosts off" deviation would then have required an
explicit override back to `GEN_4`. Dropping §3.2 removes that trap.

## Resolved: the calc economy is dropped

Spec §9.4 priced `calc(target_set)` at one charge and built an information economy around
it. With the calc service removed there is no oracle to price, and the economy is **dropped
from v1 entirely** rather than rebuilt on the ROM's own `CalculateMoveDamage`.

All agent tools are now free. §9.4's principle reduces from *"information is free,
computation is charged"* to *"information is free"*. The agent plays on documented data
(rosters, encounter tables, learnsets, computed stats) plus `empirical()` — everything it
has actually observed.

See `spec-deltas.md` for the sections this removes or rewrites.
