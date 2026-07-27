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
    HCMD_SET_NICKNAME,          // R5: every caught Pokemon must be named
    HCMD_SET_PLAYER,            // trainer name and gender: identity per attempt
    HCMD_COUNT,
};

enum HarnessError
{
    HERR_NONE = 0,
    HERR_NOT_IMPLEMENTED,       // command is known but has no handler yet
    HERR_UNKNOWN_COMMAND,       // command is outside HCMD_COUNT
    HERR_BAD_PAYLOAD,           // payload too short or malformed for the command
    HERR_NO_ENCOUNTER_TABLE,    // this map has no table for the requested method
    HERR_WARP_FAILED,           // the warp was dropped and did not take after retries
    HERR_EMPTY_NICKNAME,        // R5 rejects an empty name; the referee must supply one
    HERR_BAD_SLOT,              // party slot out of range or empty
    HERR_NOT_ELIGIBLE,          // nothing to evolve into right now
    HERR_BAD_ORDER,             // party_arrange order is not a permutation
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
// Battle text log (spec §4.7 battle events)
//
// Every line the battle would print is captured here, in ROM character
// encoding, terminated by EOS. Without it the agent sees state snapshots with
// no account of what happened between them: whether a move missed, what the
// opponent did, whether a hit was critical or ineffective.
//
// A ring buffer with a monotonic byte counter, rather than a queue the ROM has
// to manage: the ROM only ever appends and never blocks, and a reader that
// falls behind loses old text instead of stalling the battle. Readers track
// their own position against `written`.
//
// Text is emitted raw rather than decoded here. Decoding needs charmap.txt,
// which belongs on the Python side; the ROM stays dumb.
// ---------------------------------------------------------------------------

#define HARNESS_TEXTLOG_SIZE 4096

// Balls stocked when an encounter authorises a catch. Open item §15.6 asks
// whether ball supply should be a real constraint; until that is decided the
// harness keeps enough that supply is never the thing that fails.
#define HARNESS_BALL_STOCK 10

struct HarnessTextLog
{
    u32 written;                        // total bytes ever appended, monotonic
    u8  buf[HARNESS_TEXTLOG_SIZE];
};

extern struct HarnessTextLog gHarnessTextLog;

void Harness_LogBattleText(const u8 *text);
extern bool8 gHarnessCatchAllowed;

// Set by the dispatch task once the overworld has been quiet for a while: no
// palette fade, no running script, field controls unlocked. The harness must
// wait for this before issuing its first command.
//
// Guessing a frame number instead does not work. A warp issued before the field
// settles is dropped, and measured behaviour was not even monotonic in the
// delay -- settling at frame 2400 worked where both 1201 and 3600 failed,
// because the new-game sequence is in a different state at each.
extern bool8 gHarnessFieldReady;

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
    HACT_BALL,          // wild battles only; see encounterCatchAllowed (§4.6)
    HACT_RUN,           // wild battles only; fleeing a trainer is not possible
};

// HCMD_ROLL_ENCOUNTER payload. Methods mirror spec §4.5.
//
// The roll always uses the CURRENT map's tables, so the harness must warp first
// (invariant 2): met-location is stamped from the map the player stands on, and
// R2's location registry depends on it.
enum HarnessEncounterMethod
{
    HENC_GRASS = 0,
    HENC_SURF,
    HENC_ROCKSMASH,
    HENC_ROD_OLD,
    HENC_ROD_GOOD,
    HENC_ROD_SUPER,
};

struct HarnessEncounterArg
{
    u8 method;          // enum HarnessEncounterMethod
    u8 ballAllowed;     // sets encounterCatchAllowed: the referee decides, not the agent
    u8 padding[2];
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

// Per party slot (spec §4.9). Enough for the agent to reason about a switch
// without a second round trip. Full computed stats per slot are still to come.
struct HarnessPartyView
{
    u16 species;
    u16 hp;
    u16 maxHP;
    u8  level;
    u8  isLegalSwitch;
    u8  nickname[POKEMON_NAME_LENGTH + 1];
    // POKEMON_NAME_LENGTH is 12, so the name is 13 bytes and this pads the
    // struct to 22. Stated explicitly because assuming 10 produced a decoder
    // that read the wrong field and a payload the ROM rejected.
    u8  padding[1];
};

// HCMD_SET_LEVEL payload, and its reply.
//
// Setting the level with SetMonData bypasses the normal level-up path, which is
// also what offers level-up moves and triggers evolution (§4.8). So the reply
// lists the level-up moves learnable at or below the new level that the Pokemon
// does not already know; omitting it would silently strip movesets from an
// entire run.
struct HarnessSetLevelArg
{
    u8  slot;
    u8  level;
    u8  padding[2];
};

#define HARNESS_MAX_LEARNABLE 24

// Reply to HCMD_SET_LEVEL.
//
// `learned` are moves the Pokemon picked up on the way, already applied: the
// engine puts a new move straight into a free slot, so there is nothing for the
// agent to decide. `pending` are moves it could not fit because all four slots
// are full; each needs a TEACH_MOVE naming what to forget, or to be declined.
struct HarnessLearnable
{
    u32 learnedCount;
    u32 pendingCount;
    u16 learned[HARNESS_MAX_LEARNABLE];
    u16 pending[HARNESS_MAX_LEARNABLE];
};

// HCMD_SET_PLAYER payload.
//
// The trainer's name and gender belong to the attempt, not the build: §11 keys a
// ledger partly on trainer_name, and a run reads differently when the trainer is
// someone rather than a default. Applied as a command after boot rather than
// baked into config, so the choice is recorded and replayable.
struct HarnessPlayerArg
{
    u8 gender;                              // MALE or FEMALE
    u8 padding[3];
    u8 name[PLAYER_NAME_LENGTH + 1];
};

// HCMD_TEACH_MOVE payload. `forgetSlot` is ignored unless all four move slots
// are full, which is the only case the engine cannot resolve on its own.
struct HarnessTeachMoveArg
{
    u16 move;
    u8  slot;
    u8  forgetSlot;
};

// HCMD_PARTY_ARRANGE payload: the new order as party slot indices, so
// {2,0,1} means "the mon currently in slot 2 leads".
struct HarnessArrangeArg
{
    u8 count;
    u8 order[PARTY_SIZE];
    u8 padding;
};

// HCMD_SET_NICKNAME payload. The name is in ROM character encoding, EOS
// terminated; encoding it is the harness's job, as with battle text.
struct HarnessNicknameArg
{
    u8 slot;
    u8 padding[3];
    u8 name[POKEMON_NAME_LENGTH + 1];
};

// HCMD_DUMP_STATE reply: the party as the agent sees it outside battle. Reuses
// HarnessPartyView so there is one layout to keep in step, not two.
struct HarnessStateDump
{
    u32 count;
    struct HarnessPartyView party[PARTY_SIZE];
};

// Written to payloadOut when status becomes HSTAT_DECISION_PENDING.
//
// Only actions listed here are legal (§4.7): a move with no PP is absent, and a
// fainted or already-active party member is not a switch target. The agent must
// choose from this list; illegal actions are impossible rather than penalised.
//
// `battlers` is indexed by battler id, so it doubles as the §4.9 position map.
// In singles only ids 0 and 1 are populated. In doubles the layout is
// 0 = player left, 1 = opponent left, 2 = player right, 3 = opponent right;
// `aliveMask` has one bit per id, and entries for absent battlers are zeroed.
// The agent is asked once per living player battler each turn, with `battler`
// naming which one.
struct HarnessDecisionRequest
{
    u8  battler;
    u8  numLegalMoves;
    u8  numLegalSwitches;
    u8  isDouble;
    u8  numBattlers;
    u8  aliveMask;
    u8  isWild;         // ball actions are only ever legal in a wild battle
    u8  ballAllowed;    // mirrors encounterCatchAllowed (§4.6)
    struct HarnessBattlerView battlers[MAX_BATTLERS_COUNT];
    struct HarnessMoveView moves[MAX_MON_MOVES];
    u8  legalMoveSlots[MAX_MON_MOVES];
    u8  legalSwitchSlots[PARTY_SIZE];
    // Set when the engine is demanding a replacement because the active Pokemon
    // fainted, rather than the agent choosing to switch. Moves are not legal in
    // that state, so numLegalMoves is 0 and only switch targets are offered.
    //
    // Carved out of the existing padding so the party array does not move; that
    // offset has been miscounted enough times already.
    u8  forcedSwitch;
    u8  padding2[1];
    struct HarnessPartyView party[PARTY_SIZE];
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
void Harness_BattleChooseTarget(u32 battler);

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
