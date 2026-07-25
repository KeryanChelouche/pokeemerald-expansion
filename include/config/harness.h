#ifndef GUARD_CONFIG_HARNESS_H
#define GUARD_CONFIG_HARNESS_H

// LLM nuzlocke harness configuration.
//
// Every modification this project makes to upstream battle/overworld code is
// guarded by a constant declared here, so that turning HARNESS_ENABLED off
// yields a stock expansion build. This keeps the fork merge-able with upstream
// (see campaign/docs/spec-deltas.md).
//
// Settings that are plain upstream config values live in their own headers and
// are NOT duplicated here; campaign/docs/config.md is the index of what was
// changed and why.

#define HARNESS_ENABLED                 TRUE

// EXP must not be gained: the harness sets levels explicitly, and uncontrolled
// EXP would cause mid-battle level-ups, which in turn offer moves and trigger
// evolutions outside any decision point the agent is asked about.
// Enforced in BattleTypeAllowsExp (src/battle_script_commands.c).
//
// EV gain is disabled separately, and does have an upstream switch:
// B_EV_CAP_TYPE = EV_CAP_NO_GAIN in config/caps.h. The two are distinct code
// paths; disabling one does not disable the other.
#define HARNESS_DISABLE_EXP             TRUE

// Battle style must be Set, not Shift: under Shift the player is offered a free
// switch after each opponent faint, which would appear as a legal action the
// ruleset does not intend to exist. Upstream forces this under `#if TESTING`
// only, so the harness widens that guard rather than relying on the save
// option, which a fresh save or an options-menu write could change.
// Enforced in BattleBeginFirstTurn (src/battle_main.c).
#define HARNESS_FORCE_BATTLE_STYLE_SET  TRUE

#endif // GUARD_CONFIG_HARNESS_H
