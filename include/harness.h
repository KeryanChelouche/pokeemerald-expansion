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

void Task_HarnessDispatch(u8 taskId);
void Harness_EnsureDispatchTask(void);

#endif // GUARD_HARNESS_H
