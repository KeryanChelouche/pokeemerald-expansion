#include "global.h"
#include "harness.h"
#include "battle.h"
#include "battle_setup.h"
#include "event_data.h"
#include "item.h"
#include "field_screen_effect.h"
#include "move.h"
#include "overworld.h"
#include "palette.h"
#include "script.h"
#include "pokemon.h"
#include "pokemon_storage_system.h"
#include "pokedex.h"
#include "random.h"
#include "wild_encounter.h"
#include "pokemon_storage_system.h"
#include "item.h"
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
static EWRAM_DATA u8 sWarpTargetX = 0;
static EWRAM_DATA u8 sWarpTargetY = 0;
static EWRAM_DATA u16 sWarpWaitFrames = 0;
static EWRAM_DATA u8 sWarpRetries = 0;

// A warp issued while the field is still settling is silently dropped: the
// destination is overwritten and the player simply stays put. Measured on the
// map quickstart leaves the player on, where the first warp of a run failed
// roughly one time in three.
//
// Retrying is the fix rather than a longer wait, because nothing arrives later
// -- the request is gone. After a few attempts the command fails instead of
// waiting forever; a harness that hangs is far harder to diagnose than one that
// reports what went wrong.
#define HARNESS_WARP_RETRY_FRAMES 90
#define HARNESS_WARP_MAX_RETRIES  4

static void Harness_IssueWarp(void);

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

    // Already there: complete without warping. Calling DoWarp to the current map
    // starts a transition, but Harness_WarpArrived is true on the very first
    // tick -- the destination already matches -- so the command reports success
    // while the screen is still fading, and whatever is issued next lands
    // mid-transition and hangs. Reproduced by rolling the same location twice in
    // a row, which is exactly what R3 invites after a dupe forces a run.
    if (gSaveBlock1Ptr->location.mapGroup == arg->mapGroup
     && gSaveBlock1Ptr->location.mapNum == arg->mapNum)
    {
        Harness_Ok(0);
        return;
    }

    sWarpTargetGroup = arg->mapGroup;
    sWarpTargetNum = arg->mapNum;
    sWarpTargetX = arg->x;
    sWarpTargetY = arg->y;
    sWarpWaitFrames = 0;
    sWarpRetries = 0;
    sWarpPending = TRUE;

    Harness_IssueWarp();
}

static void Harness_IssueWarp(void)
{
    SetWarpDestination(sWarpTargetGroup, sWarpTargetNum, WARP_ID_NONE,
                       sWarpTargetX, sWarpTargetY);
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

// Set by roll_encounter, read by the decision hook. §4.6: a ball throw is legal
// only when the referee has authorised it for a valid first encounter, so this
// is never something the agent turns on for itself.
EWRAM_DATA bool8 gHarnessCatchAllowed = FALSE;

// Consecutive quiet overworld frames required before the harness may act. The
// field can look idle for a frame mid-sequence, so a single sample is not
// enough.
#define HARNESS_FIELD_SETTLE_FRAMES 60

EWRAM_DATA bool8 gHarnessFieldReady = FALSE;
static EWRAM_DATA u16 sQuietFrames = 0;

// Rolls on the CURRENT map's tables (spec §4.5), which is why the harness must
// warp first: met-location is stamped from the map the player is standing on and
// R2's location registry depends on it (invariant 2).
static void Harness_RollEncounter(void)
{
    const struct HarnessEncounterArg *arg =
        (const struct HarnessEncounterArg *)gHarnessMailbox.payloadIn;
    const struct WildPokemonInfo *info = NULL;
    enum WildPokemonArea area = WILD_AREA_LAND;
    u16 headerId;
    enum TimeOfDay timeOfDay;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    headerId = GetCurrentMapWildMonHeaderId();
    if (headerId == HEADER_NONE)
    {
        // This map has no wild table at all. Failing loudly matters: silently
        // rolling nothing would look like an unlucky empty encounter and would
        // quietly spend a location.
        Harness_Fail(HERR_NO_ENCOUNTER_TABLE);
        return;
    }

    gHarnessCatchAllowed = arg->ballAllowed;

    // §4.6 locks the bag, so the ball the agent may throw has to be put there by
    // the harness. Without stock the throw script has nothing to consume and the
    // battle stalls rather than reporting a failure.
    if (arg->ballAllowed && !CheckBagHasItem(ITEM_POKE_BALL, 1))
        AddBagItem(ITEM_POKE_BALL, HARNESS_BALL_STOCK);
    gBattleOutcome = 0;
    sBattlePending = TRUE;

    // Rods run through the game's own fishing path, which picks the species and
    // starts the battle itself.
    switch (arg->method)
    {
    case HENC_ROD_OLD:
        FishingWildEncounter(OLD_ROD);
        return;
    case HENC_ROD_GOOD:
        FishingWildEncounter(GOOD_ROD);
        return;
    case HENC_ROD_SUPER:
        FishingWildEncounter(SUPER_ROD);
        return;
    case HENC_GRASS:
        area = WILD_AREA_LAND;
        break;
    case HENC_SURF:
        area = WILD_AREA_WATER;
        break;
    case HENC_ROCKSMASH:
        area = WILD_AREA_ROCKS;
        break;
    default:
        sBattlePending = FALSE;
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    timeOfDay = GetTimeOfDayForEncounters(headerId, area);
    switch (area)
    {
    case WILD_AREA_LAND:
        info = gWildMonHeaders[headerId].encounterTypes[timeOfDay].landMonsInfo;
        break;
    case WILD_AREA_WATER:
        info = gWildMonHeaders[headerId].encounterTypes[timeOfDay].waterMonsInfo;
        break;
    case WILD_AREA_ROCKS:
        info = gWildMonHeaders[headerId].encounterTypes[timeOfDay].rockSmashMonsInfo;
        break;
    default:
        break;
    }

    // A map can have grass but no surf or rock smash table. Distinguish that
    // from "no tables at all" so the campaign loader can tell which draw options
    // a location actually supports (§7.2).
    if (info == NULL)
    {
        sBattlePending = FALSE;
        Harness_Fail(HERR_NO_ENCOUNTER_TABLE);
        return;
    }

    // No WILD_CHECK_REPEL or KEEN_EYE: the harness wants the table's own
    // distribution, not one filtered by held items or party state.
    TryGenerateWildMon(info, area, 0);
    BattleSetup_StartWildBattle();
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

// R5: every caught Pokemon must be nicknamed, and an empty name is rejected.
// Enforced here rather than trusted to the caller, because a blank nickname is
// invisible in play and would only surface in the graveyard at the end of a run.
// Party state outside battle (spec §4.9, partial). The decision request already
// carries this during a battle; the agent needs it between battles too, to decide
// what to level, evolve or bring to the next fight.
static void Harness_DumpState(void)
{
    struct HarnessStateDump *out = (struct HarnessStateDump *)gHarnessMailbox.payloadOut;
    struct Pokemon *party = gParties[B_TRAINER_PLAYER];
    u32 i;

    memset(out, 0, sizeof(*out));
    out->count = gPartiesCount[B_TRAINER_PLAYER];

    for (i = 0; i < PARTY_SIZE; i++)
    {
        u32 species = GetMonData(&party[i], MON_DATA_SPECIES_OR_EGG);

        out->party[i].species = species;
        out->party[i].hp = GetMonData(&party[i], MON_DATA_HP);
        out->party[i].maxHP = GetMonData(&party[i], MON_DATA_MAX_HP);
        out->party[i].level = GetMonData(&party[i], MON_DATA_LEVEL);
        out->party[i].isLegalSwitch = species != SPECIES_NONE
                                   && GetMonData(&party[i], MON_DATA_HP) != 0;
        GetMonData(&party[i], MON_DATA_NICKNAME, out->party[i].nickname);
    }

    Harness_Ok(sizeof(*out));
}

// §4.8: levels are set explicitly because EXP is disabled.
//
// Moves are handed to the engine's own level-up path rather than derived here.
// MonTryLearningNewMoveAtLevel already knows which moves a level grants, learns
// them into free slots, tracks multiple moves at one level, and handles
// form-change cases such as Zacian and Zamazenta declining Iron Head. An earlier
// version of this walked the learnset by hand and got it wrong -- it offered
// every move at or below the level, including ones the Pokemon had not reached.
//
// Levels are stepped one at a time because a move granted at level 3 is missed
// entirely by jumping straight from 2 to 5.
static void Harness_SetLevel(void)
{
    const struct HarnessSetLevelArg *arg =
        (const struct HarnessSetLevelArg *)gHarnessMailbox.payloadIn;
    struct HarnessLearnable *out =
        (struct HarnessLearnable *)gHarnessMailbox.payloadOut;
    struct Pokemon *mon;
    u32 lvl, target, from;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->slot >= PARTY_SIZE
     || GetMonData(&gParties[B_TRAINER_PLAYER][arg->slot], MON_DATA_SPECIES_OR_EGG) == SPECIES_NONE)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }

    mon = &gParties[B_TRAINER_PLAYER][arg->slot];
    from = GetMonData(mon, MON_DATA_LEVEL);
    target = arg->level;
    memset(out, 0, sizeof(*out));

    if (target <= from)
    {
        Harness_Ok(sizeof(*out));
        return;
    }

    // EXP must be set, not just the level: CalculateMonStats opens with
    // GetLevelFromMonExp (src/pokemon.c), so writing MON_DATA_LEVEL alone is
    // silently undone as soon as stats are recalculated.
    {
        enum Species species = GetMonData(mon, MON_DATA_SPECIES);
        u32 exp = gExperienceTables[gSpeciesInfo[species].growthRate][target];
        u8 lv8 = target;

        SetMonData(mon, MON_DATA_EXP, &exp);
        SetMonData(mon, MON_DATA_LEVEL, &lv8);
    }
    CalculateMonStats(mon);

    for (lvl = from + 1; lvl <= target; lvl++)
    {
        bool32 first = TRUE;
        enum Move got;

        while ((got = MonTryLearningNewMoveAtLevel(mon, first, lvl)) != MOVE_NONE)
        {
            first = FALSE;
            if (got == MON_HAS_MAX_MOVES)
            {
                // Four moves known, so the engine could not place it. gMoveToLearn
                // holds what was offered; the agent decides what it replaces.
                if (out->pendingCount < HARNESS_MAX_LEARNABLE)
                    out->pending[out->pendingCount++] = gMoveToLearn;
            }
            else if (got != MON_ALREADY_KNOWS_MOVE)
            {
                if (out->learnedCount < HARNESS_MAX_LEARNABLE)
                    out->learned[out->learnedCount++] = got;
            }
        }
    }

    Harness_Ok(sizeof(*out));
}

// R1: a fainted Pokemon is dead permanently. Removing it from the party is what
// makes that real -- otherwise HealPlayerParty revives it, and unlimited
// out-of-battle healing is explicitly allowed by R10.
//
// Uses the engine's own removal idiom (ZeroMonData, CompactPartySlots,
// CalculatePlayerPartyCount) rather than shuffling slots by hand; the party count
// and slot ordering are the engine's business.
//
// The referee decides who is dead and keeps the graveyard; this only carries out
// the removal.
// The bag. Balls, TMs and held items are the three item classes a run actually
// decides between; everything else in Emerald's item list is navigation or
// convenience the harness does not use.
static void Harness_GiveItem(void)
{
    const struct HarnessGiveItemArg *arg =
        (const struct HarnessGiveItemArg *)gHarnessMailbox.payloadIn;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (!AddBagItem(arg->item, arg->count ? arg->count : 1))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    Harness_Ok(0);
}

// Storage. Sixteen segments offer far more than six encounters and R1 forbids
// releasing, so a run without a box stops at the sixth catch.
static void Harness_BoxDeposit(void)
{
    const struct HarnessBoxArg *arg =
        (const struct HarnessBoxArg *)gHarnessMailbox.payloadIn;
    u32 count = CalculatePlayerPartyCount();

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->slot >= count || GetMonData(&gPlayerParty[arg->slot], MON_DATA_SPECIES) == SPECIES_NONE)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }
    // R9 ends the run when the party is empty, so the party must never be
    // emptied by a deposit -- that would end it by bookkeeping rather than by
    // losing a battle.
    if (count <= 1)
    {
        Harness_Fail(HERR_LAST_MON);
        return;
    }
    if (CopyMonToPC(&gPlayerParty[arg->slot]) != MON_GIVEN_TO_PC)
    {
        Harness_Fail(HERR_BOX_FULL);
        return;
    }

    ZeroMonData(&gPlayerParty[arg->slot]);
    CompactPartySlots();
    CalculatePlayerPartyCount();
    Harness_Ok(0);
}

// Storage is addressed as one flat list in fill order, not as box/position: the
// agent picks from what HCMD_DUMP_BOX reported, and boxes are a UI detail it
// never sees.
static struct BoxPokemon *Harness_NthStored(u32 want, u32 *seen)
{
    u32 box, pos;

    *seen = 0;
    for (box = 0; box < TOTAL_BOXES_COUNT; box++)
    {
        for (pos = 0; pos < IN_BOX_COUNT; pos++)
        {
            struct BoxPokemon *mon = GetBoxedMonPtr(box, pos);

            if (GetBoxMonData(mon, MON_DATA_SPECIES) == SPECIES_NONE)
                continue;
            if (*seen == want)
                return mon;
            (*seen)++;
        }
    }
    return NULL;
}

static void Harness_BoxWithdraw(void)
{
    const struct HarnessBoxArg *arg =
        (const struct HarnessBoxArg *)gHarnessMailbox.payloadIn;
    u32 count = CalculatePlayerPartyCount();
    struct BoxPokemon *stored;
    u32 seen;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (count >= PARTY_SIZE)
    {
        Harness_Fail(HERR_PARTY_FULL);
        return;
    }

    stored = Harness_NthStored(arg->slot, &seen);
    if (stored == NULL)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }

    BoxMonToMon(stored, &gPlayerParty[count]);
    ZeroBoxMonData(stored);
    CalculatePlayerPartyCount();
    Harness_Ok(0);
}

static void Harness_DumpBox(void)
{
    struct HarnessBoxReport *out =
        (struct HarnessBoxReport *)gHarnessMailbox.payloadOut;
    u32 box, pos, n = 0;

    for (box = 0; box < TOTAL_BOXES_COUNT && n < HARNESS_BOX_REPORT_MAX; box++)
    {
        for (pos = 0; pos < IN_BOX_COUNT && n < HARNESS_BOX_REPORT_MAX; pos++)
        {
            struct BoxPokemon *mon = GetBoxedMonPtr(box, pos);
            struct Pokemon tmp;

            if (GetBoxMonData(mon, MON_DATA_SPECIES) == SPECIES_NONE)
                continue;

            // Through a real Pokemon so HP and stats come from the engine's own
            // calculation rather than being recomputed here.
            BoxMonToMon(mon, &tmp);
            out->mons[n].species = GetMonData(&tmp, MON_DATA_SPECIES);
            out->mons[n].hp = GetMonData(&tmp, MON_DATA_HP);
            out->mons[n].maxhp = GetMonData(&tmp, MON_DATA_MAX_HP);
            out->mons[n].level = GetMonData(&tmp, MON_DATA_LEVEL);
            out->mons[n].padding = 0;
            GetMonData(&tmp, MON_DATA_NICKNAME, out->mons[n].nickname);
            n++;
        }
    }

    out->count = n;
    Harness_Ok(sizeof(out->count) + n * sizeof(out->mons[0]));
}

static void Harness_Release(void)
{
    const struct HarnessSetLevelArg *arg =
        (const struct HarnessSetLevelArg *)gHarnessMailbox.payloadIn;   // slot only

    if (gHarnessMailbox.payloadInLen < sizeof(u32))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->slot >= PARTY_SIZE
     || GetMonData(&gParties[B_TRAINER_PLAYER][arg->slot], MON_DATA_SPECIES_OR_EGG) == SPECIES_NONE)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }

    ZeroMonData(&gParties[B_TRAINER_PLAYER][arg->slot]);
    CompactPartySlots();
    CalculatePlayerPartyCount();
    Harness_Ok(0);
}

// §4.8: applies a move the agent chose, replacing one if all four slots are
// full. SetMonMoveSlot writes the move and its PP together; RemoveMonPPBonus
// clears the PP-up bonus belonging to the move being discarded, which is what
// the party menu does and is easy to forget on a hand-rolled version.
// Gender is not cosmetic in Emerald: it decides which rival you face, so the
// campaign resolves the rival's trainer id from it.
static void Harness_SetPlayer(void)
{
    const struct HarnessPlayerArg *arg =
        (const struct HarnessPlayerArg *)gHarnessMailbox.payloadIn;
    u32 i;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->name[0] == EOS)
    {
        Harness_Fail(HERR_EMPTY_NICKNAME);
        return;
    }

    gSaveBlock2Ptr->playerGender = (arg->gender == FEMALE) ? FEMALE : MALE;
    for (i = 0; i < PLAYER_NAME_LENGTH + 1; i++)
        gSaveBlock2Ptr->playerName[i] = arg->name[i];
    gSaveBlock2Ptr->playerName[PLAYER_NAME_LENGTH] = EOS;

    Harness_Ok(0);
}

static void Harness_TeachMove(void)
{
    const struct HarnessTeachMoveArg *arg =
        (const struct HarnessTeachMoveArg *)gHarnessMailbox.payloadIn;
    struct Pokemon *mon;
    u32 i;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->slot >= PARTY_SIZE
     || GetMonData(&gParties[B_TRAINER_PLAYER][arg->slot], MON_DATA_SPECIES_OR_EGG) == SPECIES_NONE)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }

    mon = &gParties[B_TRAINER_PLAYER][arg->slot];

    // A free slot needs no decision, so let the engine place it as it would on
    // a natural level-up.
    for (i = 0; i < MAX_MON_MOVES; i++)
    {
        if (GetMonData(mon, MON_DATA_MOVE1 + i) == MOVE_NONE)
        {
            GiveMoveToMon(mon, arg->move);
            Harness_Ok(0);
            return;
        }
    }

    if (arg->forgetSlot >= MAX_MON_MOVES)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }

    RemoveMonPPBonus(mon, arg->forgetSlot);
    SetMonMoveSlot(mon, arg->move, arg->forgetSlot);
    Harness_Ok(0);
}

// §4.8: evolution must be explicit, because setting levels directly never
// triggers it. Eligibility is asked of GetEvolutionTargetSpecies rather than
// worked out here -- it knows about levels, items, friendship, held items and
// the personality split that decides Silcoon from Cascoon.
//
// The apply sequence mirrors the evolution scene (src/evolution_scene.c): species,
// clear the evolution tracker, recalculate stats, rename if the nickname was the
// old species name, and register the new species in the dex.
static void Harness_Evolve(void)
{
    const struct HarnessSetLevelArg *arg =
        (const struct HarnessSetLevelArg *)gHarnessMailbox.payloadIn;   // slot only
    struct Pokemon *mon;
    enum Species before, target;
    u32 zero = 0;

    if (gHarnessMailbox.payloadInLen < sizeof(u32))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->slot >= PARTY_SIZE
     || GetMonData(&gParties[B_TRAINER_PLAYER][arg->slot], MON_DATA_SPECIES_OR_EGG) == SPECIES_NONE)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }

    mon = &gParties[B_TRAINER_PLAYER][arg->slot];
    before = GetMonData(mon, MON_DATA_SPECIES);
    target = GetEvolutionTargetSpecies(mon, EVO_MODE_NORMAL, ITEM_NONE, NULL, NULL, CHECK_EVO);

    if (target == SPECIES_NONE)
    {
        Harness_Fail(HERR_NOT_ELIGIBLE);
        return;
    }

    SetMonData(mon, MON_DATA_SPECIES, &target);
    SetMonData(mon, MON_DATA_EVOLUTION_TRACKER, &zero);
    CalculateMonStats(mon);
    EvolutionRenameMon(mon, before, target);
    GetSetPokedexFlag(SpeciesToNationalPokedexNum(target), FLAG_SET_SEEN);
    GetSetPokedexFlag(SpeciesToNationalPokedexNum(target), FLAG_SET_CAUGHT);

    gHarnessMailbox.payloadOut[0] = target & 0xFF;
    gHarnessMailbox.payloadOut[1] = target >> 8;
    Harness_Ok(2);
}

// Reorders the party. Rejects anything that is not a permutation of the living
// party, so a malformed order cannot duplicate or silently drop a Pokemon --
// with R1 in force, losing one to a bad index would be indistinguishable from a
// death.
static void Harness_PartyArrange(void)
{
    const struct HarnessArrangeArg *arg =
        (const struct HarnessArrangeArg *)gHarnessMailbox.payloadIn;
    struct Pokemon *party = gParties[B_TRAINER_PLAYER];
    struct Pokemon reordered[PARTY_SIZE];
    u32 seen = 0;
    u32 count, i;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }

    count = gPartiesCount[B_TRAINER_PLAYER];
    if (arg->count != count)
    {
        Harness_Fail(HERR_BAD_ORDER);
        return;
    }

    for (i = 0; i < count; i++)
    {
        if (arg->order[i] >= count || (seen & (1u << arg->order[i])))
        {
            Harness_Fail(HERR_BAD_ORDER);
            return;
        }
        seen |= 1u << arg->order[i];
    }

    for (i = 0; i < count; i++)
        reordered[i] = party[arg->order[i]];
    for (i = 0; i < count; i++)
        party[i] = reordered[i];

    Harness_Ok(0);
}

static void Harness_SetNickname(void)
{
    const struct HarnessNicknameArg *arg =
        (const struct HarnessNicknameArg *)gHarnessMailbox.payloadIn;

    if (gHarnessMailbox.payloadInLen < sizeof(*arg))
    {
        Harness_Fail(HERR_BAD_PAYLOAD);
        return;
    }
    if (arg->slot >= PARTY_SIZE
     || GetMonData(&gParties[B_TRAINER_PLAYER][arg->slot], MON_DATA_SPECIES_OR_EGG) == SPECIES_NONE)
    {
        Harness_Fail(HERR_BAD_SLOT);
        return;
    }
    if (arg->name[0] == EOS)
    {
        Harness_Fail(HERR_EMPTY_NICKNAME);
        return;
    }

    SetMonData(&gParties[B_TRAINER_PLAYER][arg->slot], MON_DATA_NICKNAME, arg->name);
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
    // Reaching here at all means the overworld is running. Require it to stay
    // quiet, so the first command is not issued into the tail of the new-game
    // sequence.
    // Deliberately not ArePlayerFieldControlsLocked(): the new-game script locks
    // controls and the harness bypasses the script that would unlock them, so it
    // stays locked forever and readiness would never be reported.
    if (gPaletteFade.active || ScriptContext_IsEnabled())
        sQuietFrames = 0;
    else if (sQuietFrames < HARNESS_FIELD_SETTLE_FRAMES)
        sQuietFrames++;
    else
        gHarnessFieldReady = TRUE;

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
        else if (++sWarpWaitFrames >= HARNESS_WARP_RETRY_FRAMES)
        {
            sWarpWaitFrames = 0;
            if (++sWarpRetries > HARNESS_WARP_MAX_RETRIES)
            {
                sWarpPending = FALSE;
                Harness_Fail(HERR_WARP_FAILED);
                gHarnessMailbox.command = HCMD_NONE;
                gHarnessMailbox.sequence++;
            }
            else if (!gPaletteFade.active)
            {
                Harness_IssueWarp();
            }
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

    // Accept new commands only when the field has settled. A warp issued during
    // the fade left over from the previous command is dropped, and the harness
    // then waits forever for a completion that will never come. This was
    // intermittent precisely because it depended on which frame the command
    // happened to land on.
    if (gPaletteFade.active)
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
    case HCMD_SET_NICKNAME:
        Harness_SetNickname();
        break;
    case HCMD_SET_PLAYER:
        Harness_SetPlayer();
        break;
    case HCMD_DUMP_STATE:
        Harness_DumpState();
        break;
    case HCMD_SET_LEVEL:
        Harness_SetLevel();
        break;
    case HCMD_RELEASE:
        Harness_Release();
        break;
    case HCMD_TEACH_MOVE:
        Harness_TeachMove();
        break;
    case HCMD_EVOLVE:
        Harness_Evolve();
        break;
    case HCMD_PARTY_ARRANGE:
        Harness_PartyArrange();
        break;
    case HCMD_ROLL_ENCOUNTER:
        // Asynchronous like trainer_battle: finished by the sBattlePending
        // branch once the encounter battle ends.
        Harness_RollEncounter();
        if (sBattlePending)
            return;
        break;
    case HCMD_WARP:
        // Normally completes asynchronously, finished by the sWarpPending branch
        // above. When the player is already on the target map it completes
        // synchronously instead, and must fall through to be acknowledged.
        Harness_Warp();
        if (sWarpPending)
            return;
        break;
    case HCMD_TRAINER_BATTLE:
        // Asynchronous for the same reason; finished by the sBattlePending
        // branch. A malformed payload fails synchronously, though, so only
        // return early if the command was actually accepted.
        Harness_TrainerBattle();
        if (sBattlePending)
            return;
        break;
    case HCMD_GIVE_ITEM:
        Harness_GiveItem();
        break;
    case HCMD_BOX_DEPOSIT:
        Harness_BoxDeposit();
        break;
    case HCMD_BOX_WITHDRAW:
        Harness_BoxWithdraw();
        break;
    case HCMD_DUMP_BOX:
        Harness_DumpBox();
        break;
    case HCMD_ATTEMPT_CATCH:
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
