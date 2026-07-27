#!/usr/bin/env python3
"""A live harness session: boot the ROM once, then drive it command by command.

The one-shot scripts each baked a fixed command list into Lua before booting,
which cannot express a campaign -- what happens after a battle depends on how it
went. Here Lua owns the mailbox and takes commands from a directory as they
appear, so Python decides what to send next while the ROM is running.

This is the shape the real bridge (spec §5) needs; it uses numbered files rather
than TCP so there is no port, no handshake, and no partially-read record.

Protocol, per command N:
    Python writes  cmd_N    "<id> <hex payload>"
    Lua  writes    done_N   "<status> <hex payloadOut>"
Decisions are separate, since they arrive mid-command:
    Lua  writes    req_M / txt_M
    Python writes  reply_M "<type> <slot> <target>"

Every file is written to a .tmp and renamed, because a reader that catches a
half-written record sees a plausible wrong value rather than an error.
"""

import json
import os
import pathlib
import struct
import subprocess
import tempfile
import time

# Mirrors include/harness.h.
HCMD_WARP, HCMD_TRAINER_BATTLE, HCMD_ROLL_ENCOUNTER = 1, 2, 3
HCMD_HEAL, HCMD_SET_LEVEL = 5, 6
HCMD_RELEASE, HCMD_SET_FLAG, HCMD_DUMP_STATE, HCMD_SET_PARTY = 11, 12, 13, 14
HCMD_SET_SEED, HCMD_DECISION, HCMD_SET_NICKNAME = 15, 16, 17
HSTAT_IDLE, HSTAT_DECISION_PENDING, HSTAT_ERROR = 0, 2, 3

SYSTEM_FLAGS = 0x860
BADGE_FLAGS = [SYSTEM_FLAGS + 0x7 + i for i in range(8)]

TEXTLOG_SIZE = 4096
PAYLOAD_SIZE = 2048
OFF_PAYLOAD_OUT = 12 + PAYLOAD_SIZE

LUA = """
local B = {base}
local OUT = B + {off_out}
local TLOG = {tlog}
local TSIZE = {tsize}
local DIR = "{dir}"
local READY_FLAG = {ready}

local n, booted = 0, false
local cmdN, busy, answered, inBattleCmd = 1, false, 0, false
local reqN, awaiting, lastW = 0, false, 0

local function writeAtomic(name, body)
  local f = io.open(DIR .. "/" .. name .. ".tmp", "w")
  f:write(body); f:close()
  os.rename(DIR .. "/" .. name .. ".tmp", DIR .. "/" .. name)
end

local function hexFrom(addr, len)
  local p = {{}}
  for i = 0, len - 1 do p[#p + 1] = string.format("%02X", emu:read8(addr + i)) end
  return table.concat(p)
end

-- Text appended since the caller last looked, so each report covers exactly the
-- span that just resolved.
local function drainText()
  local nowW = emu:read32(TLOG)
  local p = {{}}
  local i = lastW
  while i < nowW do
    p[#p + 1] = string.format("%02X", emu:read8(TLOG + 4 + (i % TSIZE)))
    i = i + 1
  end
  lastW = nowW
  return table.concat(p)
end

callbacks:add("frame", function()
  n = n + 1
  -- Quickstart: SELECT at the title screen skips the intro, naming and Birch
  -- sequence, so the ROM reaches the overworld unattended.
  if n >= 600 and n <= 1200 then
    if (n % 30) < 5 then emu:setKeys(4) else emu:setKeys(0) end
    return
  end
  -- Keep pressing A past the title screen: the new-game sequence has its own
  -- prompts. Readiness is reported by the ROM, not guessed from a frame number.
  if n > 1200 and not booted then
    if (n % 16) < 5 then emu:setKeys(1) else emu:setKeys(0) end
    if emu:read8(READY_FLAG) ~= 0 then
      emu:setKeys(0); booted = true; writeAtomic("READY", string.format("%d", n))
    end
    return
  end
  if not booted then return end

  -- Press A only while a battle is resolving, to advance its messages. On the
  -- overworld A interacts with whatever the player is facing, and pressing it
  -- during a warp was interfering with the warp itself.
  if inBattleCmd or emu:read16(B + 2) == {pending} then
    if (n % 16) < 5 then emu:setKeys(1) else emu:setKeys(0) end
  else
    emu:setKeys(0)
  end

  if emu:read16(B + 2) == {pending} and not awaiting then
    reqN = reqN + 1
    writeAtomic("txt_" .. reqN, drainText())
    writeAtomic("req_" .. reqN, hexFrom(OUT, emu:read16(B + 10)))
    awaiting = true
  end

  if awaiting then
    local rf = io.open(DIR .. "/reply_" .. reqN, "r")
    if rf then
      local a, b2, c = rf:read("n"), rf:read("n"), rf:read("n")
      rf:close(); os.remove(DIR .. "/reply_" .. reqN)
      for i, v in ipairs({{a, b2, c, 0}}) do emu:write8(B + 12 + i - 1, v) end
      emu:write16(B + 8, 4); emu:write16(B + 0, {c_dec}); emu:write16(B + 2, 1)
      -- Answering a decision leaves the mailbox idle with the command cleared,
      -- exactly as a finished command does. Remember that one happened so the
      -- check below can tell them apart.
      answered = answered + 1
      awaiting = false
    end
    return
  end

  if busy then
    -- Idle with the command cleared normally means the command finished, but a
    -- decision reply looks identical. Once a decision has been answered, only a
    -- reply carrying the outcome byte counts as the end of the battle; a plain
    -- idle is just the turn continuing.
    if emu:read16(B + 2) ~= 1 and emu:read16(B + 0) == 0
       and (answered == 0 or emu:read16(B + 10) == 1) then
      writeAtomic("txt_done_" .. cmdN, drainText())
      writeAtomic("done_" .. cmdN,
                  string.format("%d %s", emu:read16(B + 2), hexFrom(OUT, emu:read16(B + 10))))
      busy = false
      inBattleCmd = false
      cmdN = cmdN + 1
    end
    return
  end

  local cf = io.open(DIR .. "/cmd_" .. cmdN, "r")
  if cf then
    local line = cf:read("l"); cf:close()
    local id, hex = line:match("^(%d+)%s*(%x*)$")
    id = tonumber(id)
    local len = #hex / 2
    for i = 0, len - 1 do
      emu:write8(B + 12 + i, tonumber(hex:sub(i * 2 + 1, i * 2 + 2), 16))
    end
    answered = 0
    -- Only these produce battle messages that need advancing.
    inBattleCmd = (id == {c_battle} or id == {c_enc})
    emu:write16(B + 8, len); emu:write16(B + 0, id); emu:write16(B + 2, 1)
    busy = true
  end
end)
"""


class SessionError(RuntimeError):
    pass


class Session:
    """Owns the emulator process and the command/reply directory."""

    def __init__(self, mgba: pathlib.Path, rom: pathlib.Path, symbols: pathlib.Path,
                 timeout: float = 120.0):
        self.mgba, self.rom, self.timeout = mgba, rom, timeout
        syms = json.loads(symbols.read_text())["symbols"]
        for name in ("gHarnessMailbox", "gHarnessTextLog"):
            if name not in syms:
                raise SessionError(f"{name} absent from symbols — ROM built without "
                                   f"the harness?")
        if "gHarnessFieldReady" not in syms:
            raise SessionError("gHarnessFieldReady absent from symbols — rebuild the ROM")
        self.base, self.tlog = syms["gHarnessMailbox"], syms["gHarnessTextLog"]
        self.ready = syms["gHarnessFieldReady"]
        self._td = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._td.name)
        self.n = 0          # commands issued
        self.reqs = 0       # decisions served
        self.proc = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        (self.dir / "play.lua").write_text(LUA.format(
            base=self.base, off_out=OFF_PAYLOAD_OUT, tlog=self.tlog,
            tsize=TEXTLOG_SIZE, dir=self.dir, c_dec=HCMD_DECISION,
            pending=HSTAT_DECISION_PENDING, ready=self.ready,
            c_battle=HCMD_TRAINER_BATTLE, c_enc=HCMD_ROLL_ENCOUNTER))
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = (f"{self.mgba.resolve().parent}:"
                                  f"{env.get('LD_LIBRARY_PATH', '')}")
        self.proc = subprocess.Popen(
            [str(self.mgba), "--script", str(self.dir / "play.lua"), str(self.rom),
             "-l", "0"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._await(self.dir / "READY", "boot")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._td.cleanup()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- plumbing -----------------------------------------------------------
    def _await(self, path: pathlib.Path, what: str) -> str:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if path.exists():
                return path.read_text()
            if self.proc and self.proc.poll() is not None:
                raise SessionError(f"emulator exited while waiting for {what}")
            time.sleep(0.02)
        raise SessionError(f"timed out waiting for {what}")

    def _write(self, name: str, body: str) -> None:
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(body)
        tmp.rename(self.dir / name)

    # -- commands -----------------------------------------------------------
    def send(self, cmd: int, payload: bytes = b"") -> None:
        """Queue a command. Does not wait: battles and encounters publish
        decision requests before they complete."""
        self.n += 1
        self._write(f"cmd_{self.n}", f"{cmd} {payload.hex().upper()}\n")

    def pump(self):
        """Return the next event, blocking until one arrives.

        ('decision', request_bytes, text_lines) -- a choice is required
        ('done', status, payload_out, text_lines) -- the command finished
        """
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            req = self.dir / f"req_{self.reqs + 1}"
            done = self.dir / f"done_{self.n}"
            if req.exists():
                self.reqs += 1
                txt = self._take_text(f"txt_{self.reqs}")
                return ("decision", bytes.fromhex(req.read_text()), txt)
            if done.exists():
                status, _, hexout = done.read_text().partition(" ")
                txt = self._take_text(f"txt_done_{self.n}")
                return ("done", int(status), bytes.fromhex(hexout.strip()), txt)
            if self.proc and self.proc.poll() is not None:
                raise SessionError("emulator exited mid-command")
            time.sleep(0.02)
        raise SessionError("timed out waiting for the ROM")

    def _take_text(self, name: str) -> bytes:
        p = self.dir / name
        return bytes.fromhex(p.read_text()) if p.exists() else b""

    def decide(self, kind: int, slot: int, target: int) -> None:
        self._write(f"reply_{self.reqs}", f"{kind} {slot} {target}\n")

    def run(self, cmd: int, payload: bytes = b"", on_decision=None):
        """Send a command and drive it to completion.

        `on_decision(request_bytes, text)` must return (kind, slot, target).
        Raises if a command reports HSTAT_ERROR rather than letting a failed
        step pass silently -- the harness's failures tend to look like stalls.
        """
        self.send(cmd, payload)
        transcript = []
        while True:
            ev = self.pump()
            if ev[0] == "decision":
                _, raw, txt = ev
                transcript.append(txt)
                if on_decision is None:
                    raise SessionError("a decision was requested but no handler "
                                       "was given")
                self.decide(*on_decision(raw, txt))
            else:
                _, status, out, txt = ev
                transcript.append(txt)
                if status == HSTAT_ERROR:
                    raise SessionError(f"command {cmd} failed, error code "
                                       f"{out[0] if out else '?'}")
                return out, b"".join(transcript)

    # -- convenience --------------------------------------------------------
    def bootstrap(self, seed: int, party: bytes, badges: bool = True) -> None:
        """Seed, grant badges, then build the party.

        Badges come first and are not cosmetic: a Pokemon above the obedience
        level for the badges held refuses orders, naps, or hits itself, which
        reads as a broken decision hook rather than a rules mechanic.
        """
        self.run(HCMD_SET_SEED, struct.pack("<I", seed))
        if badges:
            for flag in BADGE_FLAGS:
                self.run(HCMD_SET_FLAG, struct.pack("<H", flag))
        self.run(HCMD_SET_PARTY, party)
