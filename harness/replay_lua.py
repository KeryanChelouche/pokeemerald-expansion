#!/usr/bin/env python3
"""Frame-exact replay driver.

The interactive session cannot replay a run. It writes each command when Python
gets round to it, so a command lands on whichever frame the emulator happens to
poll -- and the overworld consumes RNG while it idles, so a different number of
idle frames changes the RNG state before a battle even begins. The same decisions
then produce a different battle. Measured: replaying an 11-decision run through
the live driver needed only 7.

So replay does not keep Python in the loop. The entire plan -- every command and
every decision reply, each with the frame it was applied on -- is written out
before the emulator starts, and Lua applies each entry on exactly that frame.
Nothing depends on wall-clock timing, which is what makes the run reproducible.
"""

import pathlib

PLAN_LUA = """
local B = {base}
local OUT = B + {off_out}
local PLAN = "{plan}"
local DIR = "{dir}"
local shotDir = "{shots}"
local every = {every}

-- The plan in order. Entries are applied when their frame is reached, and any
-- entry whose frame has already passed is applied at the first opportunity --
-- the first command is recorded at frame 0, before a frame has elapsed, so an
-- exact match would never fire for it.
local plan, pos = {{}}, 1
for line in io.lines(PLAN) do
  local f, kind, rest = line:match("^(%d+) (%a) (.*)$")
  if f then
    plan[#plan + 1] = {{frame = tonumber(f), kind = kind, rest = rest}}
  end
end

local n, shot, applied, lastApplied = 0, 0, 0, 0
local log = io.open(DIR .. "/replay.log", "w")

local function writePayload(hex)
  local len = #hex / 2
  for i = 0, len - 1 do
    emu:write8(B + 12 + i, tonumber(hex:sub(i * 2 + 1, i * 2 + 2), 16))
  end
  return len
end

callbacks:add("frame", function()
  n = n + 1

  -- Boot exactly as the live driver does, so the frames line up from the start.
  if n >= 600 and n <= 1200 then
    if (n % 30) < 5 then emu:setKeys(4) else emu:setKeys(0) end
    return
  end

  -- The same message-advancing pulse. It has to match, because it is part of
  -- what determines when the ROM reaches each decision.
  if emu:read16(B + 2) == 2 or emu:read16(B + 0) ~= 0 then
    if (n % 16) < 5 then emu:setKeys(1) else emu:setKeys(0) end
  else
    emu:setKeys(0)
  end

  -- Only one entry per frame: each command has to be acknowledged by the ROM
  -- before the next is written, or the second would overwrite the first.
  if pos <= #plan and plan[pos].frame <= n and emu:read16(B + 2) ~= 1 then
    do
      local e = plan[pos]
      if e.kind == "C" then
        local cmd, hex = e.rest:match("^(%d+)%s*(%x*)$")
        local len = writePayload(hex or "")
        emu:write16(B + 8, len)
        emu:write16(B + 0, tonumber(cmd))
        emu:write16(B + 2, 1)
      else
        local a, b2, c = e.rest:match("^(%d+) (%d+) (%d+)$")
        emu:write8(B + 12, tonumber(a))
        emu:write8(B + 13, tonumber(b2))
        emu:write8(B + 14, tonumber(c))
        emu:write8(B + 15, 0)
        emu:write16(B + 8, 4)
        emu:write16(B + 0, {c_dec})
        emu:write16(B + 2, 1)
      end
      applied = applied + 1
      pos = pos + 1
      lastApplied = n
      log:write(string.format("f%d applied %s (want f%d)\\n", n, e.kind, e.frame))
      log:flush()
    end
  end

  if every > 0 and (n % every) == 0 then
    shot = shot + 1
    emu:screenshot(string.format("%s/%06d.png", shotDir, shot))
  end

  if pos > #plan and n == lastApplied + 600 then
    log:write(string.format("done applied=%d frames=%d\\n", applied, n)); log:flush()
    local w = io.open(DIR .. "/REPLAY_DONE", "w")
    w:write(string.format("%d", applied)); w:close()
  end
end)
"""


def write_plan(path: pathlib.Path, entries) -> int:
    """entries: (frame, 'C', cmd, hex) or (frame, 'D', type, slot, target)."""
    last = 0
    with path.open("w") as fh:
        for e in sorted(entries, key=lambda x: x[0]):
            last = max(last, e[0])
            if e[1] == "C":
                fh.write(f"{e[0]} C {e[2]} {e[3]}\n")
            else:
                fh.write(f"{e[0]} D {e[2]} {e[3]} {e[4]}\n")
    return last
