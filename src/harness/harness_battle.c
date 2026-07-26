#include "global.h"
#include "harness.h"
#include "battle.h"
#include "battle_controllers.h"
#include "move.h"
#include "party_menu.h"
#include "pokemon.h"
#include "constants/battle.h"

// Battle decision hook (spec §4.7).
//
// Replaces the player controller's input reads. Where it would poll the D-pad
// and A button, it instead serialises battle state plus an enumerated list of
// legal actions, blocks on HSTAT_DECISION_PENDING, and proceeds as if the
// returned action had been selected.

#if HARNESS_ENABLED

// The action stage publishes the request; the move stage answers from this,
// because the engine asks twice per turn but the agent chooses once.
static EWRAM_DATA struct HarnessDecision sDecision[MAX_BATTLERS_COUNT] = {0};
static EWRAM_DATA bool8 sHaveDecision[MAX_BATTLERS_COUNT] = {0};

static void Harness_FillBattlerView(struct HarnessBattlerView *v, u32 battler)
{
    struct BattlePokemon *mon = &gBattleMons[battler];
    u32 i;

    v->species    = mon->species;
    v->hp         = mon->hp;
    v->maxHP      = mon->maxHP;
    v->attack     = mon->attack;
    v->defense    = mon->defense;
    v->speed      = mon->speed;
    v->spAttack   = mon->spAttack;
    v->spDefense  = mon->spDefense;
    v->status1    = mon->status1;
    v->level      = mon->level;
    v->ability    = mon->ability;
    for (i = 0; i < 3; i++)
        v->types[i] = mon->types[i];
    for (i = 0; i < NUM_BATTLE_STATS; i++)
        v->statStages[i] = mon->statStages[i];
}

// Legality, not preference. A move with no PP or no move id is omitted, as is a
// party member that is fainted, empty, or already on the field. Under Set style
// the engine never reaches the action stage for a free post-faint switch, so
// nothing extra is needed here to satisfy that rule.
static void Harness_PublishRequest(u32 battler)
{
    struct HarnessDecisionRequest *req =
        (struct HarnessDecisionRequest *)gHarnessMailbox.payloadOut;
    struct Pokemon *party = gParties[B_TRAINER_PLAYER];
    u32 i;

    memset(req, 0, sizeof(*req));
    req->battler = battler;
    req->isDouble = IsDoubleBattle();
    req->numBattlers = gBattlersCount;
    // §4.7: the BAG action is absent unless the referee authorised a catch, and
    // even then only in a wild battle. Legality is decided here, not by the
    // agent declining to use it.
    req->isWild = !(gBattleTypeFlags & BATTLE_TYPE_TRAINER);
    req->ballAllowed = req->isWild && gHarnessCatchAllowed;

    // Indexed by battler id so it also serves as the §4.9 position map. Absent
    // battlers stay zeroed and simply have no aliveMask bit.
    for (i = 0; i < gBattlersCount && i < MAX_BATTLERS_COUNT; i++)
    {
        Harness_FillBattlerView(&req->battlers[i], i);
        if (IsBattlerAlive(i))
            req->aliveMask |= 1u << i;
    }

    for (i = 0; i < MAX_MON_MOVES; i++)
    {
        req->moves[i].move  = gBattleMons[battler].moves[i];
        req->moves[i].pp    = gBattleMons[battler].pp[i];
        req->moves[i].maxPP = CalculatePPWithBonus(gBattleMons[battler].moves[i],
                                                  gBattleMons[battler].ppBonuses, i);
        if (gBattleMons[battler].moves[i] != MOVE_NONE && gBattleMons[battler].pp[i] != 0)
            req->legalMoveSlots[req->numLegalMoves++] = i;
    }

    for (i = 0; i < PARTY_SIZE; i++)
    {
        u32 species = GetMonData(&party[i], MON_DATA_SPECIES_OR_EGG);
        bool32 legal = species != SPECIES_NONE
                    && GetMonData(&party[i], MON_DATA_HP) != 0
                    && i != gBattlerPartyIndexes[battler];

        req->party[i].species = species;
        req->party[i].hp = GetMonData(&party[i], MON_DATA_HP);
        req->party[i].maxHP = GetMonData(&party[i], MON_DATA_MAX_HP);
        req->party[i].level = GetMonData(&party[i], MON_DATA_LEVEL);
        req->party[i].isLegalSwitch = legal;
        GetMonData(&party[i], MON_DATA_NICKNAME, req->party[i].nickname);

        if (legal)
            req->legalSwitchSlots[req->numLegalSwitches++] = i;
    }

    gHarnessMailbox.payloadOutLen = sizeof(*req);
    gHarnessMailbox.status = HSTAT_DECISION_PENDING;
}

// Installed as the controller function while blocked. Polls rather than busy
// waits: one check per frame, exactly as the input read it replaces did.
static void Harness_WaitForDecision(enum BattlerId battler)
{
    const struct HarnessDecision *d;

    if (gHarnessMailbox.status != HSTAT_CMD_PENDING
     || gHarnessMailbox.command != HCMD_DECISION)
        return;

    d = (const struct HarnessDecision *)gHarnessMailbox.payloadIn;
    if (gHarnessMailbox.payloadInLen < sizeof(*d))
    {
        // Refuse rather than guess: a malformed decision must not silently
        // become a move choice.
        gHarnessMailbox.payloadOut[0] = HERR_BAD_PAYLOAD;
        gHarnessMailbox.payloadOutLen = 1;
        gHarnessMailbox.status = HSTAT_ERROR;
        gHarnessMailbox.command = HCMD_NONE;
        gHarnessMailbox.sequence++;
        return;
    }

    sDecision[battler] = *d;
    sHaveDecision[battler] = TRUE;

    gHarnessMailbox.command = HCMD_NONE;
    gHarnessMailbox.payloadOutLen = 0;
    gHarnessMailbox.status = HSTAT_IDLE;
    gHarnessMailbox.sequence++;

    if (d->type == HACT_SWITCH)
    {
        BtlController_EmitTwoReturnValues(battler, B_COMM_TO_ENGINE, B_ACTION_SWITCH, 0);
    }
    else if (d->type == HACT_BALL)
    {
        // `slot` and `target` carry the ball item id, low byte first.
        //
        // It must go in gBallToDisplay, not gLastUsedItem: HandleAction_ThrowBall
        // starts with `gLastUsedItem = gBallToDisplay`, so setting only
        // gLastUsedItem is overwritten with zero and the throw silently stalls
        // the battle instead of failing.
        gBallToDisplay = gLastThrownBall = gLastUsedItem = d->slot | (d->target << 8);
        BtlController_EmitTwoReturnValues(battler, B_COMM_TO_ENGINE, B_ACTION_THROW_BALL, 0);
    }
    else
    {
        BtlController_EmitTwoReturnValues(battler, B_COMM_TO_ENGINE, B_ACTION_USE_MOVE, 0);
    }
    BtlController_Complete(battler);
}

// Returns TRUE when the harness has taken over, so the caller returns without
// drawing menus or reading input.
bool8 Harness_BattleChooseAction(u32 battler)
{
    sHaveDecision[battler] = FALSE;
    Harness_PublishRequest(battler);
    gBattlerControllerFuncs[battler] = Harness_WaitForDecision;
    return TRUE;
}

// A switch action would otherwise open the party menu and block forever on
// input. The slot the agent chose was already validated against
// legalSwitchSlots when the request was built.
bool8 Harness_BattleChoosePokemon(u32 battler)
{
    if (!sHaveDecision[battler] || sDecision[battler].type != HACT_SWITCH)
        return FALSE;

    BtlController_EmitChosenMonReturnValue(battler, B_COMM_TO_ENGINE,
                                          sDecision[battler].slot,
                                          gBattlePartyCurrentOrder);
    BtlController_Complete(battler);
    return TRUE;
}

// Preselects the agent's move and then deliberately does NOT take over: the
// game's own move-selection code runs and confirms the already-correct cursor.
//
// Emitting B_ACTION_EXEC_SCRIPT by hand instead meant re-deriving move targeting
// outside the engine, and got it wrong — moves resolved against the user rather
// than the opponent. Target selection depends on move target type, gimmick
// state, doubles layout and ally liveness, all of which HandleInputChooseMove
// already handles. Reusing it is both shorter and correct by construction.
//
// The agent still never makes a navigation decision: it says "Tackle", and the
// harness positions the cursor. Only the confirm keypress is synthetic.
bool8 Harness_BattleChooseMove(u32 battler)
{
    if (sHaveDecision[battler] && sDecision[battler].type == HACT_MOVE)
        gMoveSelectionCursor[battler] = sDecision[battler].slot;

    return FALSE;   // fall through to the normal controller
}

// Same principle one stage later. In doubles a move with a selectable target
// routes through HandleInputChooseTarget, which highlights a default and waits
// for a confirm. Overriding the highlight here honours the agent's chosen target
// without reimplementing which targets are selectable for this move.
//
// HTARGET_DEFAULT leaves the engine's own default highlighted, which is what
// singles always wants and what doubles wants for moves with no real choice.
void Harness_BattleChooseTarget(u32 battler)
{
    if (!sHaveDecision[battler]
     || sDecision[battler].type != HACT_MOVE
     || sDecision[battler].target == HTARGET_DEFAULT)
        return;

    if (sDecision[battler].target >= MAX_BATTLERS_COUNT)
        return;     // out of range: leave the engine's default rather than crash

    gMultiUsePlayerCursor = sDecision[battler].target;
}

#endif // HARNESS_ENABLED
