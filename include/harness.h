#ifndef GUARD_HARNESS_H
#define GUARD_HARNESS_H

#include "global.h"

// LLM nuzlocke harness: the ROM half of the mailbox protocol (spec §4.2-§4.3).
//
// A single fixed-size struct in EWRAM is the entire interface between the ROM
// and the outside world. There is no dynamic allocation and no serialisation
// library on this side: the ROM writes packed binary, and bridge.lua decodes it.
//
// The struct's address and every field offset are extracted from the linker map
// by tools/harness_syms/extract_symbols.py. Nothing outside the ROM may hardcode
// an address.

#define HARNESS_PAYLOAD_SIZE 2048

enum HarnessStatus
{
    HSTAT_IDLE = 0,             // ROM is waiting for a command
    HSTAT_CMD_PENDING,          // harness has written a command, ROM must run it
    HSTAT_DECISION_PENDING,     // ROM is blocked awaiting a battle decision
    HSTAT_ERROR,                // last command failed; payloadOut holds the error
};

enum HarnessCommand
{
    HCMD_NONE = 0,
    HCMD_WARP,
    HCMD_TRAINER_BATTLE,
    HCMD_ROLL_ENCOUNTER,
    HCMD_ATTEMPT_CATCH,
    HCMD_HEAL,                  // HP + status + PP
    HCMD_SET_LEVEL,
    HCMD_TEACH_MOVE,
    HCMD_EVOLVE,
    HCMD_GIVE_ITEM,
    HCMD_PARTY_ARRANGE,
    HCMD_RELEASE,
    HCMD_SET_FLAG,
    HCMD_DUMP_STATE,
    HCMD_SET_PARTY,             // eval only
    HCMD_SET_SEED,
    HCMD_DECISION,              // reply to a decision request
    HCMD_COUNT,
};

enum HarnessError
{
    HERR_NONE = 0,
    HERR_NOT_IMPLEMENTED,       // command is known but has no handler yet
    HERR_UNKNOWN_COMMAND,       // command is outside HCMD_COUNT
    HERR_BAD_PAYLOAD,           // payload too short or malformed for the command
};

// Field offsets are part of the wire contract. Do not reorder without
// regenerating harness_symbols.json and updating bridge.lua.
//
// Deviation from spec §4.2: the spec declares a single `payloadLen`. Two are
// required, because the harness cannot know how many bytes a reply occupies
// without one for each direction.
struct HarnessMailbox
{
    u16 command;                // enum HarnessCommand, HCMD_NONE = idle
    u16 status;                 // enum HarnessStatus
    u32 sequence;               // incremented by the ROM on each completion
    u16 payloadInLen;
    u16 payloadOutLen;
    u8  payloadIn[HARNESS_PAYLOAD_SIZE];    // harness -> ROM
    u8  payloadOut[HARNESS_PAYLOAD_SIZE];   // ROM -> harness
};

extern struct HarnessMailbox gHarnessMailbox;

// ---------------------------------------------------------------------------
// Battle decision hook (spec §4.7)
//
// The engine asks the player controller twice per turn: once for an action
// (FIGHT / SWITCH / ...), then again for the specific move. §4.7 requires the
// agent to receive one enumerated list and choose from it, so the harness
// publishes a single request at the action stage covering moves *and* switches,
// caches the reply, and answers the move stage from that cache without a second
// round trip.
// ---------------------------------------------------------------------------

enum HarnessActionType
{
    HACT_MOVE = 0,
    HACT_SWITCH,
};

// One battler as the agent sees it. Computed stats are included per invariant 3:
// they are what the agent reasons from, and must never be re-derived from
// IVs/EVs/nature outside the ROM.
struct HarnessBattlerView
{
    u16 species;
    u16 hp;
    u16 maxHP;
    u16 attack;
    u16 defense;
    u16 speed;
    u16 spAttack;
    u16 spDefense;
    u32 status1;
    u8  level;
    u8  types[3];
    s8  statStages[NUM_BATTLE_STATS];
    u8  ability;
    u8  padding;
};

struct HarnessMoveView
{
    u16 move;
    u8  pp;
    u8  maxPP;
};

// Written to payloadOut when status becomes HSTAT_DECISION_PENDING.
//
// Only actions listed here are legal (§4.7): a move with no PP is absent, and a
// fainted or already-active party member is not a switch target. The agent must
// choose from this list; illegal actions are impossible rather than penalised.
struct HarnessDecisionRequest
{
    u8  battler;
    u8  numLegalMoves;
    u8  numLegalSwitches;
    u8  isDouble;
    struct HarnessBattlerView player;
    struct HarnessBattlerView opponent;
    struct HarnessMoveView moves[MAX_MON_MOVES];
    u8  legalMoveSlots[MAX_MON_MOVES];
    u8  legalSwitchSlots[PARTY_SIZE];
};

// Pass as `target` to let the ROM pick the target the way the game does for a
// move with no selectable target — which is every move in singles. The agent
// only supplies a target when the choice is real.
#define HTARGET_DEFAULT 0xFF

// HCMD_DECISION payload (harness -> ROM).
struct HarnessDecision
{
    u8 type;        // enum HarnessActionType
    u8 slot;        // move slot for HACT_MOVE, party slot for HACT_SWITCH
    u8 target;      // battler id, or HTARGET_DEFAULT
    u8 padding;
};

bool8 Harness_BattleChooseAction(u32 battler);
bool8 Harness_BattleChooseMove(u32 battler);
bool8 Harness_BattleChoosePokemon(u32 battler);

// HCMD_WARP payload.
struct HarnessWarpArg
{
    u8 mapGroup;
    u8 mapNum;
    u8 x;
    u8 y;
};

enum HarnessBattleKind
{
    HKIND_SINGLE = 0,
    HKIND_DOUBLE,
    HKIND_DOUBLE_TWO_OPPONENTS,     // Tate & Liza
};

// HCMD_TRAINER_BATTLE payload.
struct HarnessTrainerBattleArg
{
    u16 trainerId;
    u16 trainerIdB;                 // HKIND_DOUBLE_TWO_OPPONENTS only
    u8  kind;                       // enum HarnessBattleKind
    u8  padding[3];
};

// HCMD_SET_PARTY payload: a u32 count followed by `count` of these.
//
// The count is u32 rather than u8 solely for alignment. These specs contain u32
// fields and ARM cannot load those from an unaligned address, so a u8 count
// would place every spec 1 byte out and yield rotated garbage rather than a
// fault.
//
// Deviation from spec §5.1, which specifies `{showdown_text}`. Parsing Showdown
// text on a GBA would be a large amount of fragile string handling for no gain:
// the Python side can parse it and send this instead. Text parsing belongs in
// Python; the ROM stays dumb.
//
// `personality` is supplied rather than derived, because in Gen 3 it determines
// nature, gender and shininess. Letting the harness choose it keeps every one of
// those deterministic and puts the search for "a personality with nature X" in
// Python rather than in the ROM.
struct HarnessMonSpec
{
    u32 personality;
    u16 species;
    u16 heldItem;
    u16 moves[MAX_MON_MOVES];
    u8  level;
    u8  abilityNum;
    u8  ivs[NUM_STATS];
    u8  evs[NUM_STATS];
    u8  nickname[POKEMON_NAME_LENGTH + 1];
};

void Task_HarnessDispatch(u8 taskId);
void Harness_EnsureDispatchTask(void);

#endif // GUARD_HARNESS_H
