#include "global.h"
#include "harness.h"
#include "battle.h"
#include "battle_setup.h"
#include "event_data.h"
#include "field_screen_effect.h"
#include "move.h"
#include "overworld.h"
#include "pokemon.h"
#include "random.h"
#include "script_pokemon_util.h"
#include "task.h"
#include "constants/battle.h"
#include "constants/characters.h"

// Mailbox and dispatch task (spec §4.2-§4.3).
//
// Lives in .sbss so it is zero-initialised at boot: status starts HSTAT_IDLE and
// command starts HCMD_NONE without an explicit initialiser.

#if HARNESS_ENABLED

EWRAM_DATA struct HarnessMailbox gHarnessMailbox = {0};

// A warp does not complete on the frame it is requested, and loading a map
// resets tasks. This state therefore cannot live in the task's data: it lives
// in EWRAM, which survives the map load, and Harness_EnsureDispatchTask
// recreates the task on the far side.
static EWRAM_DATA bool8 sWarpPending = FALSE;
static EWRAM_DATA u8 sWarpTargetGroup = 0;
static EWRAM_DATA u8 sWarpTargetNum = 0;

// Same reasoning as the warp: a battle spans many frames and the field callback
// does not run during it, so completion cannot be detected inline.
static EWRAM_DATA bool8 sBattlePending = FALSE;

#define HARNESS_DISPATCH_TASK_PRIORITY 80

EWRAM_DATA struct HarnessTextLog gHarnessTextLog = {0};

// Appends one battle line, EOS-terminated so a reader can split records.
//
// Called from BattlePutTextOnWindow, which every battle message funnels
// through, so nothing that appears on screen is missed. Deliberately does no
// filtering: deciding which lines matter is the harness's job, and a line
// dropped here is unrecoverable.
void Harness_LogBattleText(const u8 *text)
{
    u32 i;

    if (text == NULL)
        return;

    for (i = 0; i < HARNESS_TEXTLOG_SIZE && text[i] != EOS; i++)
    {
        gHarnessTextLog.buf[gHarnessTextLog.written % HARNESS_TEXTLOG_SIZE] = text[i];
        gHarnessTextLog.written++;
    }

    gHarnessTextLog.buf[gHarnessTextLog.written % HARNESS_TEXTLOG_SIZE] = EOS;
    gHarnessTextLog.written++;
}

static void Harness_Fail(enum HarnessError err)
{
    gHarnessMailbox.payloadOut[0] = err;
    gHarnessMailbox.payloadOutLen = 1;
    gHarnessMailbox.status = HSTAT_ERROR;
}

static void Harness_Ok(u16 outLen)
{
    gHarnessMailbox.payloadOutLen = outLen;
    gHarnessMailbox.status = HSTAT_IDLE;
}

static void Harness_Heal(void)
{
    HealPlayerParty(); // HP, status and PP
    Harness_Ok(0);
}

// Reports completion only once the player is actually standing on the target
// map. Returning HSTAT_IDLE when DoWarp is merely *requested* would let the
// harness roll an encounter against the map it was leaving, which invariant 2
// exists to prevent.
static void Harness_Warp(void)
{
    const struct HarnessWarpArg *arg = (const struct HarnessWarpArg *)gHarnessMailbox.payloadIn;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    sWarpTargetGroup = arg->mapGroup;
    sWarpTargetNum = arg->mapNum;
    sWarpPending = TRUE;

    SetWarpDestination(arg->mapGroup, arg->mapNum, WARP_ID_NONE, arg->x, arg->y);
    DoWarp();
    ResetInitialPlayerAvatarState();
}

static bool8 Harness_WarpArrived(void)
{
    // The task only ticks from OverworldBasic, so reaching here at all means the
    // field is running again; the map identity is the remaining question.
    return gSaveBlock1Ptr->location.mapGroup == sWarpTargetGroup
        && gSaveBlock1Ptr->location.mapNum == sWarpTargetNum;
}

// Bypasses the overworld and script paths entirely (spec §4.4). Both of those
// converge on BattleSetup_StartTrainerBattle, which derives gBattleTypeFlags
// from approaching-trainer state the harness has none of, and which routes the
// ending through post-battle dialogue and reward scripts. The _Debug variant
// skips all of that and returns straight to the field.
//
// Party, levels, movesets, held items, AI flags and single/double all derive
// from trainer data. No roster is duplicated here.
static void Harness_TrainerBattle(void)
{
    const struct HarnessTrainerBattleArg *arg =
        (const struct HarnessTrainerBattleArg *)gHarnessMailbox.payloadIn;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    gBattleTypeFlags = BATTLE_TYPE_TRAINER;
    TRAINER_BATTLE_PARAM.opponentA = arg->trainerId;
    TRAINER_BATTLE_PARAM.opponentB = 0xFFFF;

    switch (arg->kind)
    {
    case HKIND_SINGLE:
        break;
    case HKIND_DOUBLE:
        gBattleTypeFlags |= BATTLE_TYPE_DOUBLE;
        break;
    case HKIND_DOUBLE_TWO_OPPONENTS:
        TRAINER_BATTLE_PARAM.opponentB = arg->trainerIdB;
        gBattleTypeFlags |= BATTLE_TYPE_DOUBLE | BATTLE_TYPE_TWO_OPPONENTS;
        break;
    default:
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    // Cleared so the dispatch task can use it as the battle-finished signal:
    // the field callback does not run during a battle, so the next tick with a
    // non-zero outcome is the first frame back on the overworld.
    gBattleOutcome = 0;
    sBattlePending = TRUE;

    gBattleEnvironment = BattleSetup_GetEnvironmentId();
    CalculateEnemyPartyCount();
    BattleSetup_StartTrainerBattle_Debug();
}

// Required for §12's bit-identical replay. The ROM seeds its RNG from the RTC at
// boot (SeedRngWithRtc, src/main.c), and the emulator takes the RTC from the host
// clock, so two runs of the same ROM with the same inputs diverge. Measured: two
// byte-identical runs of one doubles battle agreed exactly through turn 1 and
// then diverged, finishing in 4 and 10 decisions.
//
// The harness must therefore call this after boot and before anything that
// consumes randomness, and record the seed in the ledger.
static void Harness_SetSeed(void)
{
    if (gHarnessMailbox.payloadInLen < sizeof(u32))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    SeedRng(*(const u32 *)gHarnessMailbox.payloadIn);
    Harness_Ok(0);
}

// Progression flags are the driver's responsibility because battle scripts are
// bypassed (spec §7.3). Badge flags in particular are not bookkeeping: a
// Pokemon above the obedience level for the badges held will ignore orders,
// nap, or hit itself, which looks exactly like a broken decision hook.
static void Harness_SetFlag(void)
{
    if (gHarnessMailbox.payloadInLen < sizeof(u16))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    FlagSet(*(const u16 *)gHarnessMailbox.payloadIn);
    Harness_Ok(0);
}

static void Harness_SetParty(void)
{
    const u8 *p = gHarnessMailbox.payloadIn;
    u32 count = *(const u32 *)p;
    u32 i, j;

    // The count is u32, not u8, purely for alignment: the specs that follow it
    // contain u32 fields, and ARM cannot load those from an odd address. A u8
    // count would put every spec 1 byte out and silently return rotated
    // garbage for `personality`.
    if (count > PARTY_SIZE
     || gHarnessMailbox.payloadInLen < sizeof(u32) + count * sizeof(struct HarnessMonSpec))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    ZeroPlayerPartyMons();

    for (i = 0; i < count; i++)
    {
        const struct HarnessMonSpec *spec =
            (const struct HarnessMonSpec *)(p + sizeof(u32) + i * sizeof(struct HarnessMonSpec));
        struct Pokemon *mon = &gParties[B_TRAINER_PLAYER][i];

        CreateMon(mon, spec->species, spec->level, spec->personality, OTID_STRUCT_PLAYER_ID);

        for (j = 0; j < NUM_STATS; j++)
        {
            SetMonData(mon, MON_DATA_HP_IV + j, &spec->ivs[j]);
            SetMonData(mon, MON_DATA_HP_EV + j, &spec->evs[j]);
        }

        for (j = 0; j < MAX_MON_MOVES; j++)
        {
            u32 pp = GetMovePP(spec->moves[j]);
            SetMonData(mon, MON_DATA_MOVE1 + j, &spec->moves[j]);
            SetMonData(mon, MON_DATA_PP1 + j, &pp);
        }

        SetMonData(mon, MON_DATA_HELD_ITEM, &spec->heldItem);
        SetMonData(mon, MON_DATA_ABILITY_NUM, &spec->abilityNum);
        if (spec->nickname[0] != EOS)
            SetMonData(mon, MON_DATA_NICKNAME, spec->nickname);

        // Must follow the IV and EV writes: stats are derived from them, and
        // CreateMon computed them from the values it generated, not these.
        CalculateMonStats(mon);
    }

    gPartiesCount[B_TRAINER_PLAYER] = count;
    Harness_Ok(0);
}

void Task_HarnessDispatch(u8 taskId)
{
    // An in-flight warp owns the mailbox until it lands. Nothing else may run,
    // or a command would be answered against the wrong map.
    if (sWarpPending)
    {
        if (Harness_WarpArrived())
        {
            sWarpPending = FALSE;
            Harness_Ok(0);
            gHarnessMailbox.command = HCMD_NONE;
            gHarnessMailbox.sequence++;
        }
        return;
    }

    // Likewise an in-flight battle. gBattleOutcome is the signal: it is cleared
    // at request time and the field callback does not run again until the
    // battle has ended, so a non-zero value here means we are back and done.
    if (sBattlePending)
    {
        if (gBattleOutcome != 0)
        {
            sBattlePending = FALSE;
            gHarnessMailbox.payloadOut[0] = gBattleOutcome;
            Harness_Ok(1);
            gHarnessMailbox.command = HCMD_NONE;
            gHarnessMailbox.sequence++;
        }
        return;
    }

    if (gHarnessMailbox.status != HSTAT_CMD_PENDING)
        return;

    // Every command must reach exactly one of Harness_Ok or Harness_Fail. A
    // command that silently left the status at HSTAT_CMD_PENDING would hang the
    // harness; one that silently succeeded without acting would be worse, so
    // unimplemented commands report rather than no-op.
    switch (gHarnessMailbox.command)
    {
    case HCMD_HEAL:
        Harness_Heal();
        break;
    case HCMD_SET_PARTY:
        Harness_SetParty();
        break;
    case HCMD_SET_SEED:
        Harness_SetSeed();
        break;
    case HCMD_SET_FLAG:
        Harness_SetFlag();
        break;
    case HCMD_WARP:
        // Completes asynchronously; the sWarpPending branch above finishes the
        // handshake, so this must not fall through to the sequence increment.
        Harness_Warp();
        return;
    case HCMD_TRAINER_BATTLE:
        // Asynchronous for the same reason; finished by the sBattlePending
        // branch. A malformed payload fails synchronously, though, so only
        // return early if the command was actually accepted.
        Harness_TrainerBattle();
        if (sBattlePending)
            return;
        break;
    case HCMD_ROLL_ENCOUNTER:
    case HCMD_ATTEMPT_CATCH:
    case HCMD_SET_LEVEL:
    case HCMD_TEACH_MOVE:
    case HCMD_EVOLVE:
    case HCMD_GIVE_ITEM:
    case HCMD_PARTY_ARRANGE:
    case HCMD_RELEASE:
    case HCMD_DUMP_STATE:
    case HCMD_DECISION:
        Harness_Fail(HERR_NOT_IMPLEMENTED);
        break;
    default:
        Harness_Fail(HERR_UNKNOWN_COMMAND);
        break;
    }

    gHarnessMailbox.command = HCMD_NONE;
    gHarnessMailbox.sequence++;
}

// Save-block options the harness depends on (spec §3.1). These are not config
// constants, so they must be written at runtime; a fresh save or the options menu
// would otherwise leave them at defaults.
//
// Instant text and disabled battle animations are not cosmetic here. With
// animations on, a battle spends most of its frames in sequences that wait to be
// dismissed, which both slows every attempt and makes progress depend on input
// timing. Turning them off removes the waiting rather than papering over it.
static void Harness_ApplyOptions(void)
{
    gSaveBlock2Ptr->optionsTextSpeed = OPTIONS_TEXT_SPEED_INSTANT;
    gSaveBlock2Ptr->optionsBattleSceneOff = TRUE;
    gSaveBlock2Ptr->optionsBattleStyle = OPTIONS_BATTLE_STYLE_SET;
}

// Idempotent: called every overworld frame so the task exists however the map
// was entered, including after a battle or a savestate load.
void Harness_EnsureDispatchTask(void)
{
    if (!FuncIsActiveTask(Task_HarnessDispatch))
    {
        Harness_ApplyOptions();
        CreateTask(Task_HarnessDispatch, HARNESS_DISPATCH_TASK_PRIORITY);
    }
}

#endif // HARNESS_ENABLED
