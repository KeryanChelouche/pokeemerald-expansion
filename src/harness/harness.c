#include "global.h"
#include "harness.h"
#include "script_pokemon_util.h"
#include "task.h"

// Mailbox and dispatch task (spec §4.2-§4.3).
//
// Lives in .sbss so it is zero-initialised at boot: status starts HSTAT_IDLE and
// command starts HCMD_NONE without an explicit initialiser.

#if HARNESS_ENABLED

EWRAM_DATA struct HarnessMailbox gHarnessMailbox = {0};

#define HARNESS_DISPATCH_TASK_PRIORITY 80

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

void Task_HarnessDispatch(u8 taskId)
{
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
    case HCMD_WARP:
    case HCMD_TRAINER_BATTLE:
    case HCMD_ROLL_ENCOUNTER:
    case HCMD_ATTEMPT_CATCH:
    case HCMD_SET_LEVEL:
    case HCMD_TEACH_MOVE:
    case HCMD_EVOLVE:
    case HCMD_GIVE_ITEM:
    case HCMD_PARTY_ARRANGE:
    case HCMD_RELEASE:
    case HCMD_SET_FLAG:
    case HCMD_DUMP_STATE:
    case HCMD_SET_PARTY:
    case HCMD_SET_SEED:
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

// Idempotent: called every overworld frame so the task exists however the map
// was entered, including after a battle or a savestate load.
void Harness_EnsureDispatchTask(void)
{
    if (!FuncIsActiveTask(Task_HarnessDispatch))
        CreateTask(Task_HarnessDispatch, HARNESS_DISPATCH_TASK_PRIORITY);
}

#endif // HARNESS_ENABLED
