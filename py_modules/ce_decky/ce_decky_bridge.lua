-- CE Decky resident bridge, stable-v1 candidate.
--
-- IMPORTANT: this script is injected only into CE Decky's private runtime copy.
-- It never executes descriptor/control files as Lua. Both files use a strict,
-- percent-encoded line protocol parsed below. Real CE 7.7 behavior remains T7/T8.

-- One resident bridge per Cheat Engine, whatever loads it.
--
-- The private runtime's `main.lua` loads this file so it runs before Cheat
-- Engine enumerates its own autorun directory, and this file lives in that
-- directory, so Cheat Engine then runs it a second time. Both copies share one
-- Lua state, one status file and one control file: they overwrite each other's
-- heartbeats and each answers the same commands. The second copy also starts
-- after the game has been attached, which is the one moment Cheat Engine's own
-- module list is no longer its own, so it is the copy that can establish least
-- and it was the one writing last.
if CE_DECKY_BRIDGE_LOADED then return end
CE_DECKY_BRIDGE_LOADED = true

local DESCRIPTOR_HEADER = "CEDECKY-DESCRIPTOR-1"
local CONTROL_HEADER = "CEDECKY-CONTROL-1"
local STATUS_HEADER = "CEDECKY-STATUS-1"
local MAX_BYTES = 1024 * 1024
local POLL_MS = 250
local ATTACH_RETRY_MS = 500
local HEARTBEAT_MS = 500
local MAX_RESULT_TEXT_BYTES = 512
local MAX_PROCESS_ROWS = 256
local MAX_PROCESS_NAME_BYTES = 1024
local MAX_TARGET_PROCESS_BYTES = 1024
local MAX_COMMANDS = 2048
local MAX_STARTUP_ACTIONS = 2048
-- How many timer ticks startup may spend waiting for the next record to appear
-- after its enclosing script was switched on. Long enough for Cheat Engine to
-- build a script's children, short enough that a record which is simply absent
-- reports its exact ID instead of retrying for the whole session.
local MAX_STARTUP_WAIT_TICKS = 40
-- What one quiesce may walk and put down. A real table's address list is tens
-- of records and a few levels deep; these are bounds on a list built to be one,
-- and a walk that meets either stops rather than running the timer out.
local MAX_QUIESCE_RECORDS = 4096
local MAX_QUIESCE_DEPTH = 32
-- What the whole walk may spend, as against what one record may. Per record the
-- bound is the same one every activation here uses; without one over the walk,
-- a table whose records all refuse to come down would hold the timer for that
-- bound times the number of them. The stop this belongs to has a bound of its
-- own and proceeds regardless, so this is about the bridge staying answerable
-- rather than about the stop.
local MAX_QUIESCE_TOTAL_TICKS = 600
-- How many times a quiesce looks again for records that are switched on.
-- Putting a script down can create work: its `[DISABLE]` runs the table's own
-- code, and a record that was off can come on again from it. The walk is
-- therefore repeated until a look finds nothing, and bounded so that a table
-- switching one of its flags back on forever cannot hold the stop.
local MAX_QUIESCE_SWEEPS = 3
-- How many of a table's on/off records the descriptor may carry an off key for.
local MAX_SWITCH_OFF_VALUES = 2048
-- Auto Assembler records may explicitly run asynchronously. Active is not a
-- completed mutation while AsyncProcessing is true, so keep the command
-- pending and poll from the timer instead of rejecting the first false read.
local MAX_ACTIVATION_WAIT_TICKS = 40
local MAX_APP_ID = 4294967295
local MAX_RECORD_ID = 2147483647
local MAX_GENERATION = 2147483647
local MAX_STARTUP_VALUE_BYTES = 4096
local MAX_COMMAND_VALUE_BYTES = 4096
-- Cheat Engine can legitimately need a moment before it will open a table;
-- past this many timer attempts the load is reported as failed rather than
-- retried for the life of the session.
local MAX_TABLE_LOAD_ATTEMPTS = 8
-- Timer ticks between window-suppression sweeps. Suppression is installed once,
-- from the main form's first show; this is what keeps it true afterwards.
local WINDOW_ENFORCE_TICKS = 4
-- Cheat Engine has a handful of forms; anything beyond this is a runaway, and
-- a sweep that runs every second may not walk an unbounded list.
local MAX_ENUMERATED_FORMS = 256
-- `WM_CLOSE`. The one lever Cheat Engine's Lua offers on a window that is not
-- one of the forms `hideAllCEWindows` reaches.
local WM_CLOSE = 0x0010
-- `WM_SYSCOMMAND` with `SC_RESTORE`: ask a window to come back from minimized.
-- A game that loses the foreground to a Cheat Engine window minimizes itself,
-- and taking that window away does not undo it: on the target the game kept its
-- sound and its controller and drew nothing at all, because a minimized window
-- has nothing to draw. The compositor cannot fix that from outside either,
-- since the window is only iconic in the game's own bookkeeping.
local WM_SYSCOMMAND = 0x0112
local SC_RESTORE = 0xF120
-- `WM_ACTIVATEAPP` with `TRUE`: tell a game that its application is active
-- again. Bringing the window back from minimized is not the same thing and does
-- not imply it. Cheat Engine takes the foreground as it starts, the game is
-- deactivated and minimizes itself, and then CE Decky hides every Cheat Engine
-- window, which leaves the session with no foreground window at all. The
-- restore then puts the window back on screen with nothing to reactivate it, so
-- the game keeps running the loop it uses when it is not the active
-- application: on the device Half-Life 2 came back at 18 frames a second
-- instead of 60, stayed there for the rest of the session, and stayed there
-- after Cheat Engine exited, because nothing ever told it otherwise. Measured
-- there: posting this is what puts it back to 60.
--
-- Posting this notification does not prove foreground ownership. A separate,
-- bounded exact-target request below observes that ownership after asking.
local WM_ACTIVATEAPP = 0x001C
-- `IsIconic`, asked of Windows itself through Cheat Engine's own local call.
-- The module-qualified name is tried first so the answer cannot come from
-- something else of that name; both forms are what Cheat Engine's own shipped
-- scripts pass to `executeCodeLocalEx`.
local ICONIC_SYMBOLS = {"IsIconic", "user32.IsIconic"}
-- Both ways Cheat Engine offers of calling a function inside its own process.
-- The shipped scripts use `executeCodeLocalEx` for `DrawIconEx` and
-- `ntdll.RtlGetVersion`, and `executeCodeLocal` for exactly this shape of call,
-- one window handle into a user32 predicate, in `ceshare/ceshare_publish.lua`:
--   executeCodeLocal('IsWindowVisible', winhandle) ~= 0
-- On the device the `Ex` form answered nothing at all for `IsIconic`, so both
-- are tried rather than either being assumed to be the way.
local LOCAL_CALLS = {"executeCodeLocalEx", "executeCodeLocal"}
-- `PostMessageW`, for the one message that leaves this process. `SendMessage`
-- does not return until the receiving window's own thread has handled the
-- message, and the receiver here is a game that may be loading, hung, or simply
-- not pumping while it is minimized: that would stop the bridge's timer for as
-- long as the game took, and with it the heartbeat and every runtime command.
-- Nothing here reads the result of the restore, so waiting for one buys
-- nothing. Posting is also what Windows' own callers pair with `IsIconic`.
local POST_MESSAGE_SYMBOLS = {"PostMessageW", "user32.PostMessageW"}
-- `BOOL` is 32 bits and the call answers with the whole return register, so the
-- upper half of it is not part of the answer.
local BOOL_MASK = 0x100000000
-- `GetWindow` commands. `GW_HWNDFIRST` walks to the front of the top-level
-- chain the given window is in, `GW_HWNDNEXT` steps along it. Enumerating that
-- chain is what gives a window handle an owner without going through a caption.
local GW_HWNDFIRST = 0
local GW_HWNDNEXT = 2
-- Window sweeps after attaching during which the game is asked to restore. The
-- minimize happens while Cheat Engine starts, which is around the attach and
-- before any sweep can see a window, and the exact moment varies with how long
-- the game takes to answer.
local RESTORE_SWEEPS_AFTER_ATTACH = 6
-- Sweeps a window that was asked back has to be told it is active in before
-- that is given up on. It is deliberately not the budget above: the restore is
-- posted in one of those sweeps, and the answer to it arrives in a later one,
-- so a window restored by the last sweep of that budget would never be told
-- anything at all. A window that stays minimized, or that is destroyed and
-- recreated under another handle, runs this out instead of pinning the sweep
-- for the rest of the session.
-- Sweeps a window asked back is given to actually come back in. A game that is
-- loading, or not pumping its queue at all, handles a posted restore whenever
-- it gets to it, and it is the restore that has to be waited out, not the
-- activation: spending the activation's own attempts while the window is still
-- minimized threw them away before there was anything to send.
local RESTORE_COMPLETION_SWEEPS = 60
-- Attempts to post the activation once the window has actually come back. This
-- one is small because by then the window is up and the post either works or
-- says it did not.
local ACTIVATION_ATTEMPTS = 6
-- Window sweeps the restore capability may spend on a symbol table that has
-- nothing to answer with before it is refused for the session. Cheat Engine
-- builds that table in a thread while it starts, and the bridge asks for it
-- during that startup, so "unresolved" there means "not yet" far more often
-- than it means "not here". One sweep is one second.
local RESTORE_DISCOVERY_SWEEPS = 20
-- Failed sweeps before the one symbol table reload is spent. Reloading a table
-- Cheat Engine has not finished building corrects nothing, so the reload is
-- kept for the case where waiting alone did not settle it.
local RESTORE_RELOAD_AFTER_SWEEPS = 4
-- The system window list is unbounded and a sweep runs every second.
local MAX_ENUMERATED_WINDOWS = 512
-- Cheat Engine's own process has a few dozen modules; anything past this is not
-- a module list.
local MAX_ENUMERATED_MODULES = 1024
-- Timer ticks the bridge may spend before attaching while Cheat Engine's own
-- module list is still empty. Attaching is what makes `enumModules()` describe
-- the game instead of Cheat Engine, so it is the moment after which this
-- session can never read that list again: a list that is merely not built yet
-- is worth a bounded wait, and an empty one is the only state worth waiting
-- for. The attach happens either way once this runs out.
local MODULE_CAPTURE_TICKS = 8
-- Reading one export out of a loaded image. Everything here is a bound on
-- untrusted numbers read out of that image's own headers, so a corrupt or
-- unexpected header costs the lookup rather than walking memory.
local IMAGE_U16 = 0x10000
local IMAGE_U32 = 0x100000000
-- `MZ`, then `PE\0\0` at the offset the DOS header points to.
local IMAGE_DOS_SIGNATURE = 0x5A4D
local IMAGE_NT_SIGNATURE = 0x00004550
local IMAGE_LFANEW_OFFSET = 0x3C
local MAX_IMAGE_LFANEW = 0x1000
-- The optional header follows the 24-byte file header, and its first field says
-- which of the two image formats this is. The file header's own last-but-one
-- field says how long the optional header is, which is what bounds reading a
-- data directory out of it.
local IMAGE_SIZE_OF_OPTIONAL_HEADER_OFFSET = 0x14
local IMAGE_OPTIONAL_HEADER_OFFSET = 0x18
local IMAGE_PE32PLUS_MAGIC = 0x20B
local IMAGE_PE32_MAGIC = 0x10B
local IMAGE_SIZE_OF_IMAGE_OFFSET = 0x38
-- Data directory entry zero is the export directory. The two image formats put
-- the directory array, and the count of directories in it, at different offsets
-- in the optional header.
local IMAGE_EXPORT_DIRECTORY_OFFSET_64 = 0x70
local IMAGE_EXPORT_DIRECTORY_OFFSET_32 = 0x60
local IMAGE_DIRECTORY_COUNT_OFFSET_64 = 0x6C
local IMAGE_DIRECTORY_COUNT_OFFSET_32 = 0x5C
-- One data directory entry is an address and a size. The export directory
-- itself is ten 32-bit fields, and this reads the last of them.
local IMAGE_DIRECTORY_ENTRY_BYTES = 8
local IMAGE_EXPORT_DIRECTORY_BYTES = 40
-- Fields of the export directory itself. A name and the address it belongs to
-- are in two different tables of two different lengths, joined by the ordinal
-- table, so the ordinal is bounded by the address table's own count.
local IMAGE_EXPORT_FUNCTION_COUNT_OFFSET = 0x14
local IMAGE_EXPORT_NAME_COUNT_OFFSET = 0x18
local IMAGE_EXPORT_FUNCTIONS_OFFSET = 0x1C
local IMAGE_EXPORT_NAMES_OFFSET = 0x20
local IMAGE_EXPORT_ORDINALS_OFFSET = 0x24
-- `user32.dll` exports around 835 names here. The search is a binary search of
-- a sorted table, so the step bound is a safety net and not a limit on size.
local MAX_IMAGE_EXPORT_NAMES = 65535
local MAX_IMAGE_EXPORT_STEPS = 24
local MAX_IMAGE_EXPORT_NAME_BYTES = 128
-- A window caption is arbitrary text from a table or from Cheat Engine itself.
local MAX_DIALOG_CAPTION_BYTES = 256
-- Timer ticks between table-load attempts. Cheat Engine can legitimately need a
-- moment after startup, and each attempt re-parses the whole table, so they are
-- spaced rather than spent in the first two seconds.
local TABLE_LOAD_RETRY_TICKS = 4

local function safeByte(b)
  return (b >= 48 and b <= 57) or (b >= 65 and b <= 90) or
         (b >= 97 and b <= 122) or b == 46 or b == 95
end

local function percentEncode(value)
  local out = {}
  for i = 1, #value do
    local b = string.byte(value, i)
    if safeByte(b) then
      out[#out + 1] = string.char(b)
    else
      out[#out + 1] = string.format("%%%02X", b)
    end
  end
  return table.concat(out)
end

local function percentDecode(value)
  local out = {}
  local i = 1
  while i <= #value do
    local c = string.sub(value, i, i)
    if c == "%" then
      if i + 2 > #value then return nil, "bad percent escape" end
      local pair = string.sub(value, i + 1, i + 2)
      if not string.match(pair, "^[0-9A-Fa-f][0-9A-Fa-f]$") then return nil, "bad percent escape" end
      out[#out + 1] = string.char(tonumber(pair, 16))
      i = i + 3
    else
      local b = string.byte(c)
      if not safeByte(b) then return nil, "unescaped protocol character" end
      out[#out + 1] = c
      i = i + 1
    end
  end
  return table.concat(out)
end

local function readBounded(path)
  local f = io.open(path, "rb")
  if not f then return nil, "open failed" end
  -- Never allocate an arbitrarily large mutable protocol file and only then
  -- check its size. Read at most one byte beyond the protocol limit.
  local data = f:read(MAX_BYTES + 1)
  f:close()
  if not data then return nil, "read failed" end
  if #data == 0 or #data > MAX_BYTES then return nil, "protocol size invalid" end
  if string.find(data, "\0", 1, true) or string.find(data, "\r", 1, true) then return nil, "forbidden control character" end
  return data
end

local function splitTabs(line)
  local fields = {}
  local start = 1
  while true do
    local pos = string.find(line, "\t", start, true)
    if not pos then
      fields[#fields + 1] = string.sub(line, start)
      return fields
    end
    fields[#fields + 1] = string.sub(line, start, pos - 1)
    start = pos + 1
  end
end

local function splitLines(data)
  local lines = {}
  for line in string.gmatch(data, "([^\n]+)\n?") do
    lines[#lines + 1] = line
  end
  return lines
end

local function canonicalInteger(text, allowZero, maxValue)
  -- Lua patterns have no alternation. Canonical decimal is therefore checked
  -- explicitly: digits only, no leading zero, and short enough that tonumber
  -- cannot lose precision before the range check below.
  if type(text) ~= "string" or #text < 1 or #text > 10 then return nil end
  if not string.match(text, "^[0-9]+$") then return nil end
  if #text > 1 and string.sub(text, 1, 1) == "0" then return nil end
  local value = tonumber(text)
  if not value then return nil end
  if allowZero then
    if value < 0 then return nil end
  else
    if value < 1 then return nil end
  end
  if maxValue and value > maxValue then return nil end
  return value
end

local function isHexDigest(value, length)
  return type(value) == "string" and #value == length and string.match(value, "^[0-9a-fA-F]+$") ~= nil
end

local function isProcessBasename(value)
  if type(value) ~= "string" or #value < 5 or #value > MAX_TARGET_PROCESS_BYTES then return false end
  if string.find(value, '[\\/:*?"<>|%c]') then return false end
  return string.lower(string.sub(value, -4)) == ".exe"
end

local function isUuid(value)
  return type(value) == "string" and string.match(string.lower(value),
    "^[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]%-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]%-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]%-[89ab][0-9a-f][0-9a-f][0-9a-f]%-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]$") ~= nil
end

local function isWineZPath(value)
  return type(value) == "string" and #value >= 4 and #value <= 16384 and
    string.sub(value, 1, 3) == "Z:\\" and
    not string.find(value, "%c")
end

local function parseDescriptor(data)
  local lines = splitLines(data)
  if lines[1] ~= DESCRIPTOR_HEADER then return nil, "descriptor header mismatch" end
  local fields = {}
  local startup = {}
  local startupSeen = {}
  local switchOff = {}
  local switchOffCount = 0
  local allowed = {
    session_id = true, app_id = true, is_shortcut = true, ce_sha256 = true, table_sha256 = true,
    table_path = true, target_process = true, control_path = true, status_path = true,
    table_has_lua = true,
  }
  for i = 2, #lines do
    local parts = splitTabs(lines[i])
    if parts[1] == "F" and #parts == 3 then
      if not allowed[parts[2]] then return nil, "unknown descriptor field" end
      if fields[parts[2]] ~= nil then return nil, "duplicate descriptor field" end
      local decoded, err = percentDecode(parts[3])
      if not decoded then return nil, err end
      fields[parts[2]] = decoded
    elseif parts[1] == "A" and (#parts == 4 or #parts == 5) then
      if #startup >= MAX_STARTUP_ACTIONS then return nil, "too many startup actions" end
      local rid = canonicalInteger(parts[2], true, MAX_RECORD_ID)
      if not rid or (parts[3] ~= "active" and parts[3] ~= "value") then return nil, "invalid startup action" end
      local decoded, err = percentDecode(parts[4])
      if not decoded then return nil, err end
      if #decoded > MAX_STARTUP_VALUE_BYTES then return nil, "startup value too large" end
      if parts[3] == "active" and decoded ~= "0" and decoded ~= "1" then return nil, "invalid startup active value" end
      -- Absent in a descriptor written by an earlier build, which the backend
      -- retires as stale rather than launching: no eligibility then, and the
      -- only thing lost is advisory proof.
      local proof = false
      if #parts == 5 then
        if parts[5] ~= "0" and parts[5] ~= "1" then return nil, "invalid startup proof eligibility" end
        proof = parts[5] == "1"
      end
      if proof and (parts[3] ~= "active" or decoded ~= "1") then return nil, "invalid startup proof eligibility" end
      local startupKey = tostring(rid) .. ":" .. parts[3]
      if startupSeen[startupKey] then return nil, "duplicate startup action" end
      startupSeen[startupKey] = true
      startup[#startup + 1] = { record_id = rid, kind = parts[3], value = decoded, proof = proof }
    elseif parts[1] == "S" and #parts == 3 then
      -- The key this record is switched off at. Releasing a frozen record
      -- leaves the value it was frozen at in the game, so a quiesce that only
      -- released them would report every flag down with every flag still on.
      if switchOffCount >= MAX_SWITCH_OFF_VALUES then return nil, "too many switch off values" end
      local rid = canonicalInteger(parts[2], true, MAX_RECORD_ID)
      if not rid then return nil, "invalid switch off value" end
      local decoded, err = percentDecode(parts[3])
      if not decoded then return nil, err end
      if decoded == "" or #decoded > MAX_COMMAND_VALUE_BYTES then return nil, "invalid switch off value" end
      if switchOff[rid] ~= nil then return nil, "duplicate switch off value" end
      switchOff[rid] = decoded
      switchOffCount = switchOffCount + 1
    else
      return nil, "invalid descriptor line"
    end
  end
  local required = { "session_id", "app_id", "is_shortcut", "ce_sha256", "table_sha256", "table_path", "target_process", "control_path", "status_path" }
  for _, name in ipairs(required) do
    if fields[name] == nil then return nil, "missing descriptor field " .. name end
  end
  if not isUuid(fields.session_id) then return nil, "invalid session UUID" end
  local appid = canonicalInteger(fields.app_id, false, MAX_APP_ID)
  if not appid then return nil, "invalid AppID" end
  if fields.is_shortcut ~= "0" and fields.is_shortcut ~= "1" then return nil, "invalid Steam/shortcut identity" end
  -- Optional only so that a session prepared by an earlier build still parses:
  -- absent reads as `false` below, and the backend retires such a session as
  -- stale rather than launching it, so no live session reaches the routes below
  -- without this stated. Anything other than the two values is a broken
  -- descriptor rather than an absent field.
  if fields.table_has_lua ~= nil and fields.table_has_lua ~= "0" and fields.table_has_lua ~= "1" then
    return nil, "invalid table Lua-script identity"
  end
  if not isHexDigest(fields.ce_sha256, 64) or not isHexDigest(fields.table_sha256, 64) then return nil, "invalid descriptor digest" end
  if not isProcessBasename(fields.target_process) then return nil, "invalid target process" end
  if not isWineZPath(fields.table_path) or not isWineZPath(fields.control_path) or not isWineZPath(fields.status_path) then
    return nil, "descriptor paths must use bounded Wine Z: paths"
  end
  fields.app_id = appid
  fields.is_shortcut = fields.is_shortcut == "1"
  fields.table_has_lua = fields.table_has_lua == "1"
  -- One successful leaf is sufficient, and which one it will be is not known
  -- before startup runs: a plan with several eligible records proves itself
  -- through whichever of them was actually off and came on. Choosing one in
  -- advance reported nothing at all whenever that record happened to be on
  -- already. The backend independently checks the reported ID against the same
  -- eligible set.
  fields.startup_proof_ids = {}
  for _, action in ipairs(startup) do
    if action.proof then fields.startup_proof_ids[action.record_id] = true end
  end
  fields.startup = startup
  fields.switch_off = switchOff
  return fields
end

local function parseControl(data)
  local lines = splitLines(data)
  if lines[1] ~= CONTROL_HEADER then return nil, "control header mismatch" end
  local commands = {}
  local previous = -1
  for i = 2, #lines do
    if #commands >= MAX_COMMANDS then return nil, "too many commands" end
    local parts = splitTabs(lines[i])
    if #parts ~= 6 or parts[1] ~= "C" then return nil, "invalid control line" end
    local generation = canonicalInteger(parts[2], false, MAX_GENERATION)
    if not generation or generation <= previous then return nil, "invalid command generation" end
    previous = generation
    local kind = parts[3]
    if kind ~= "query" and kind ~= "set_active" and kind ~= "set_value" and
       kind ~= "retry_attach" and kind ~= "list_processes" and kind ~= "quiesce" then return nil, "unsupported command" end
    local rid = nil
    if parts[4] ~= "-" then
      rid = canonicalInteger(parts[4], true, MAX_RECORD_ID)
      if not rid then return nil, "invalid MemoryRecord ID" end
    end
    local value = nil
    if parts[5] ~= "-" then
      local decoded, err = percentDecode(parts[5])
      if not decoded then return nil, err end
      if #decoded > MAX_COMMAND_VALUE_BYTES then return nil, "command value too large" end
      value = decoded
      if #value > MAX_COMMAND_VALUE_BYTES then return nil, "command value too large" end
    end
    local targetPid = nil
    if parts[6] ~= "-" then
      targetPid = canonicalInteger(parts[6], false, MAX_APP_ID)
      if not targetPid then return nil, "invalid target PID" end
    end
    if (kind == "query" or kind == "set_active" or kind == "set_value") and rid == nil then return nil, "record command missing ID" end
    if (kind == "retry_attach" or kind == "list_processes" or kind == "quiesce") and rid ~= nil then return nil, "non-record command contains ID" end
    if kind == "set_active" and value ~= "0" and value ~= "1" then return nil, "invalid active value" end
    if kind == "set_value" and value == nil then return nil, "set_value requires a value" end
    if (kind == "query" or kind == "list_processes" or kind == "quiesce") and value ~= nil then return nil, "command does not accept a value" end
    if kind == "retry_attach" and value ~= nil and value ~= "" and not isProcessBasename(value) then return nil, "invalid retry target" end
    if kind == "retry_attach" and targetPid ~= nil and value == nil then return nil, "exact retry target missing basename" end
    if kind ~= "retry_attach" and targetPid ~= nil then return nil, "target PID only applies to retry attach" end
    commands[#commands + 1] = { generation = generation, kind = kind, record_id = rid, value = value, target_pid = targetPid }
  end
  return commands
end

-- A normal command-line .CT launch sets MainForm.Visible=true only after every
-- autorun script has finished. Hiding the form here once would therefore be
-- undone by CE. Wrap CE's existing OnShow handler instead: Lazarus dispatches
-- OnShow before it tells the window system to map the form, so the bridge can
-- preserve CE initialization while preventing the first focus-stealing map.
-- Install this before descriptor validation so a rejected/tampered session
-- still cannot flash a CE window while the supervisor waits and stops it.
local function installMainFormSuppression()
  if not MainForm or type(getApplication) ~= "function" or
     type(getMethodProperty) ~= "function" or
     type(setMethodProperty) ~= "function" or type(hideAllCEWindows) ~= "function" then
    return false
  end

  -- LCL's Win32 application handle is a separate 1x1 taskbar-owner window.
  -- It is created before autorun and can remain Wine's foreground window even
  -- when the real main form never maps. CE's LuaApplication binding exposes an
  -- inverted MainFormOnTaskBar property; assigning false sets the LCL property
  -- true, which hides that owner window and makes MainForm the taskbar owner.
  local applicationOk = pcall(function()
    local application = getApplication()
    if not application then error("application unavailable", 0) end
    application.MainFormOnTaskBar = false
    if application.MainFormOnTaskBar ~= false then
      error("application owner window remained enabled", 0)
    end
  end)
  if not applicationOk then return false end

  local getOk, originalOnShow = pcall(function()
    return getMethodProperty(MainForm, "OnShow")
  end)
  if not getOk then return false end

  local setOk = pcall(function()
    setMethodProperty(MainForm, "OnShow", function(sender)
      local originalOk = true
      local originalError = nil
      if originalOnShow then
        originalOk, originalError = pcall(originalOnShow, sender)
      end
      hideAllCEWindows()
      if not originalOk then error(originalError, 0) end
    end)
  end)
  if not setOk then return false end

  return pcall(hideAllCEWindows)
end


if not installMainFormSuppression() then
  print("CE Decky bridge: could not suppress the Cheat Engine window")
  pcall(closeCE)
  return
end


local descriptorPath = os.getenv("CE_DECKY_DESCRIPTOR")
local descriptorSha = os.getenv("CE_DECKY_DESCRIPTOR_SHA256")
local descriptorMd5 = os.getenv("CE_DECKY_DESCRIPTOR_MD5")
if not descriptorPath or descriptorPath == "" or
   not descriptorSha or not string.match(descriptorSha, "^[0-9a-fA-F]+$") or #descriptorSha ~= 64 or
   not descriptorMd5 or not string.match(descriptorMd5, "^[0-9a-fA-F]+$") or #descriptorMd5 ~= 32 then
  print("CE Decky bridge: descriptor environment is missing or invalid")
  return
end
descriptorSha = string.lower(descriptorSha)
descriptorMd5 = string.lower(descriptorMd5)

-- CE's public Lua API documents md5file(), but not a SHA-256 file helper. The
-- backend still treats SHA-256 as authoritative. This MD5 is an immediate
-- bridge-side race/tamper guard before any descriptor field is trusted. Check
-- both sides of the bounded read to make simple replacement races fail closed.
local hashOk, descriptorMd5Before = pcall(function() return md5file(descriptorPath) end)
if not hashOk or type(descriptorMd5Before) ~= "string" or string.lower(descriptorMd5Before) ~= descriptorMd5 then
  print("CE Decky bridge: descriptor integrity check failed before read")
  return
end
local descriptorData, descriptorErr = readBounded(descriptorPath)
if not descriptorData then
  print("CE Decky bridge: " .. tostring(descriptorErr))
  return
end
local hashAfterOk, descriptorMd5After = pcall(function() return md5file(descriptorPath) end)
if not hashAfterOk or type(descriptorMd5After) ~= "string" or string.lower(descriptorMd5After) ~= descriptorMd5 then
  print("CE Decky bridge: descriptor integrity check failed after read")
  return
end
local descriptor, parseErr = parseDescriptor(descriptorData)
if not descriptor then
  print("CE Decky bridge: " .. tostring(parseErr))
  return
end

local state = {
  descriptor = descriptor,
  descriptor_sha256 = descriptorSha,
  target_process = descriptor.target_process,
  attached = false,
  opened_process_id = 0,
  startup_applied = false,
  startup_failed = false,
  -- nil while a rollback is still running; true/false once its outcome is known.
  startup_rolled_back = nil,
  rollback = nil,
  -- Startup progresses one action per resolution, so its position, the records
  -- and snapshots taken so far, and how long it has waited for the next record
  -- all survive between timer ticks.
  startup_index = 0,
  startup_waits = 0,
  startup_pending = nil,
  startup_resolved = {},
  startup_snapshots = {},
  pending_command = nil,
  -- A quiesce in flight: the records still to put down, the one being waited
  -- on, and what the walk found. It progresses one record per timer tick the
  -- way startup does, because deactivating a script runs the table's own
  -- `[DISABLE]` and Cheat Engine reports that asynchronously.
  quiesce = nil,
  -- True from the moment a quiesce is asked for, and it stays true. Only a stop
  -- asks for one, and the stop kills Cheat Engine as soon as it answers, so
  -- this session accepts nothing that changes the game again.
  quiesced = false,
  -- How many times a Cheat Engine window had to be hidden again after the
  -- initial suppression. Anything above zero is CE mapping a window over the
  -- running game, which is the thing that takes its audio and controller input.
  window_suppressions = 0,
  -- Sweeps that saw a Cheat Engine window and could not put it down. This is a
  -- count of attempts, not of windows: the sweep runs on a timer, so one window
  -- nothing can hide raises it once per tick for as long as it is there.
  unsuppressed_sweeps = 0,
  -- Whether a window is over the game right now, which is what the panel says
  -- and the only part of this the user can see for themselves. Kept apart from
  -- the count above because that one only ever climbs: a window that was taken
  -- down, or closed by the table itself, left the count claiming the screen was
  -- still covered for the rest of the session.
  window_over_game = false,
  -- A Cheat Engine window the form sweep cannot reach, dismissed because it was
  -- on screen over the game. Counted apart from a hidden form, because it was
  -- answered rather than hidden and the user has to be told what was answered.
  dialogs_dismissed = 0,
  last_dialog = nil,
  pending_dialog = nil,
  dismissed_dialog = nil,
  -- How many times the attached game was asked to come back from minimized
  -- after a Cheat Engine window was taken off it, or after attaching.
  game_restores = 0,
  -- How many of those windows were then told their application is active
  -- again, which is a different thing and the half a game left at a fraction
  -- of its frame rate is missing.
  game_activations = 0,
  focus_sweeps_left = 0,
  focus_pending_handle = nil,
  focus_candidates = 0,
  focus_attempts = 0,
  focus_discovery_attempts = 0,
  focus_successes = 0,
  focus_reason = "idle",
  focus_address = nil,
  visible_address = nil,
  restore_sweeps_left = 0,
  restored_handles = {},
  activated_handles = {},
  -- Windows asked back and not yet told they are active, with the sweeps each
  -- has left to be told in.
  pending_activations = {},
  -- The symbol proven to answer "is this window minimized", or false once the
  -- question has been asked and cannot be answered here.
  iconic_symbol = nil,
  -- Where establishing the restore capability actually stopped, so a game left
  -- dark is attributable to a half rather than to the pair. `minimized_query`
  -- cannot carry this: it reports one symbol, and two of the ways this fails
  -- have nothing to do with that symbol.
  restore_reason = nil,
  -- Which of the two local call forms proved the symbol, so the same one is
  -- used for every window afterwards.
  iconic_call = nil,
  -- The resolved address of that symbol, so no window is ever asked by name.
  iconic_address = nil,
  -- Whether this session has already spent its one symbol table reload.
  symbols_reloaded = false,
  -- Sweeps already spent asking a symbol table that had nothing to answer with.
  -- The first answer is not the session's answer: Cheat Engine is still
  -- starting when the bridge asks the first time.
  restore_attempts = 0,
  -- Where each module this needs is loaded in Cheat Engine's own process, read
  -- before a game is attached and kept, or false once the module list has
  -- answered and does not contain it.
  local_module_bases = {},
  -- Whether the last reading of that list was empty, which is Cheat Engine not
  -- answering yet rather than an answer.
  local_modules_transient = false,
  module_capture_ticks_left = MODULE_CAPTURE_TICKS,
  -- What the call said when it produced no answer. A `pcall` that swallows the
  -- error leaves "this Cheat Engine does not answer" as the only thing anyone
  -- can say afterwards, which is exactly what the first report of this said.
  restore_error = nil,
  -- The resolved `PostMessageW`, or false once it has been looked for and is
  -- not there.
  post_message = nil,
  window_ticks = 0,
  table_load_ticks = 0,
  -- Whether the session's exact table is in Cheat Engine's address list yet.
  table_load_state = "pending",
  table_load_attempts = 0,
  table_load_error = nil,
  -- How that table was opened. `approved` is Cheat Engine's own stream overload
  -- with the table's Lua script authorized and its question never raised;
  -- `prompted` is the path form, which is the only route an older Cheat Engine
  -- offers and which may therefore still ask.
  table_load_route = nil,
  -- Whether the synchronous bootstrap at the end of this file has finished.
  -- Until it has, the timer publishes liveness and keeps Cheat Engine's windows
  -- off the game, and does nothing the bootstrap is in the middle of doing.
  bootstrap_complete = false,
  last_generation = -1,
  last_attach_tick = 0,
  last_status_tick = 0,
  results = {},
  startup_active_ids = {},
  processes = {},
}


local function boundedStatusText(value)
  if value == nil then return nil end
  local text = tostring(value)
  if #text > MAX_RESULT_TEXT_BYTES then return "<value too large>" end
  return text
end

local function result(generation, recordId, ok, active, value, err, errorCode)
  state.results[#state.results + 1] = {
    generation = generation, record_id = recordId, ok = ok,
    active = active, value = boundedStatusText(value), error = boundedStatusText(err),
    error_code = errorCode,
  }
  while #state.results > 128 do table.remove(state.results, 1) end
end

local function matchingPids(target)
  local processOk, processes = pcall(function() return getProcesslist() end)
  if not processOk or type(processes) ~= "table" then return {} end
  local matches = {}
  for pid, name in pairs(processes) do
    local numeric = tonumber(pid)
    if numeric and numeric > 0 and type(name) == "string" and string.lower(name) == string.lower(target) then
      matches[#matches + 1] = numeric
    end
  end
  table.sort(matches)
  return matches
end

local function tryAttach(expectedPid)
  local matches = matchingPids(state.target_process)
  local pid = expectedPid
  if expectedPid then
    local found = false
    for _, candidate in ipairs(matches) do if candidate == expectedPid then found = true break end end
    if not found then
      state.attached = false
      state.opened_process_id = 0
      return false
    end
  elseif #matches == 1 then
    pid = matches[1]
  else
    state.attached = false
    state.opened_process_id = 0
    return false
  end
  local ok = pcall(function() openProcess(pid) end)
  if not ok then
    state.attached = false
    state.opened_process_id = 0
    return false
  end
  local openedOk, opened = pcall(function() return getOpenedProcessID() end)
  if not openedOk then opened = 0 end
  state.attached = opened == pid
  state.opened_process_id = state.attached and opened or 0
  if state.attached then
    -- The game is minimized by Cheat Engine starting up and taking the
    -- foreground, which happens before the first window sweep and often without
    -- any form ever reporting itself visible: on the device the counters stayed
    -- at zero through a session that drew nothing. So the ask is not conditional
    -- on having seen a window; it runs for a few sweeps from the attach, which
    -- is the window of time in which the minimize happens, and does nothing at
    -- all to a game that was never minimized.
    state.focus_sweeps_left = ACTIVATION_ATTEMPTS
    state.focus_pending_handle = nil
    state.restore_sweeps_left = RESTORE_SWEEPS_AFTER_ATTACH
    state.restored_handles = {}
    state.activated_handles = {}
    state.pending_activations = {}
  end
  return state.attached
end

local function addressListCount()
  local countOk, currentCount = pcall(function() return AddressList.getCount() end)
  if countOk and type(currentCount) == "number" and currentCount >= 0
      and currentCount <= MAX_APP_ID and currentCount == math.floor(currentCount) then
    return currentCount
  end
  return nil
end

local function memoryRecord(recordId)
  if not AddressList then return nil, "AddressList unavailable", "address_list_unavailable" end
  local ok, record = pcall(function() return AddressList.getMemoryRecordByID(recordId) end)
  if not ok or not record then return nil, "MemoryRecord missing", "record_missing" end
  return record
end


local function recordSnapshot(record)
  local ok, active, value = pcall(function()
    return record.Active, tostring(record.Value)
  end)
  if not ok then return nil, nil, tostring(active) end
  return active, value, nil
end

-- The message and code for a mutation Cheat Engine did not carry out.
--
-- Any error anyone reported - Cheat Engine's own, a failed read, a failed async
-- step - is the answer, and stays the generic failure. What is left is Cheat
-- Engine accepting the change, finishing, and the record still not being in the
-- state that was asked for. For a script record that is Cheat Engine declining
-- to run it, which for a table whose patterns no longer match this build of the
-- game is the normal outcome. It is the one mutation failure a user can act on,
-- so it carries its own code instead of an undifferentiated one.
local function mutationOutcome(...)
  for index = 1, select("#", ...) do
    local candidate = select(index, ...)
    if candidate ~= nil and candidate ~= false then
      return tostring(candidate), "mutation_failed"
    end
  end
  return "activation did not settle", "activation_rejected"
end

local function recordAsyncProcessing(record)
  local ok, processing = pcall(function() return record.AsyncProcessing end)
  if not ok then return nil, tostring(processing) end
  return processing == true, nil
end

-- Rollback is driven by the same timer and held to the same standard as the
-- forward path. Assigning the recorded value back and hoping used to be enough
-- to report an undifferentiated "failed" while an Auto Assembler script from an
-- earlier action was still patched into the game - and an enclosing script is
-- exactly what the user never sees in their own selection.
local function beginRollback(failedIndex)
  -- Start at the failing action, not the one before it. A value write that was
  -- clamped, or an activation that changed state and then failed to settle,
  -- has already moved the record and has a pre-mutation snapshot recorded, so
  -- skipping it could report `failed_rolled_back` over a record still changed.
  -- An action that failed before its snapshot existed - a record that never
  -- appeared, a read that failed - has no entry and is skipped below.
  state.rollback = { index = failedIndex, waits = 0, proven = true, pending = nil }
end

local function restoreStarted(record, action, snapshot)
  if action.kind == "value" then
    return pcall(function() record.Value = snapshot.value end)
  end
  return pcall(function() record.Active = snapshot.active end)
end

local function restoreSettled(record, action, snapshot)
  -- `nil` means "still settling"; true/false is a proven outcome.
  if action.kind == "active" then
    local processing, asyncErr = recordAsyncProcessing(record)
    if asyncErr then return false end
    if processing then return nil end
  end
  local active, value, readErr = recordSnapshot(record)
  if readErr then return false end
  if action.kind == "value" then return value == snapshot.value end
  return active == snapshot.active
end

local function advanceRollback()
  local rollback = state.rollback
  if not rollback then return end
  if rollback.pending then
    local pending = rollback.pending
    local settled = restoreSettled(pending.record, pending.action, pending.snapshot)
    if settled == nil then
      rollback.waits = rollback.waits + 1
      if rollback.waits <= MAX_ACTIVATION_WAIT_TICKS then return end
      rollback.proven = false
    elseif settled == false then
      rollback.proven = false
    end
    rollback.pending = nil
    rollback.waits = 0
  end
  while rollback.index >= 1 do
    local index = rollback.index
    rollback.index = index - 1
    local record, snapshot = state.startup_resolved[index], state.startup_snapshots[index]
    local action = state.descriptor.startup[index]
    if record and snapshot and action then
      if not restoreStarted(record, action, snapshot) then
        rollback.proven = false
      else
        local settled = restoreSettled(record, action, snapshot)
        if settled == nil then
          rollback.pending = { record = record, action = action, snapshot = snapshot }
          rollback.waits = 0
          return
        end
        if settled == false then rollback.proven = false end
      end
    end
  end
  state.startup_rolled_back = rollback.proven
  state.rollback = nil
  state.focus_sweeps_left = ACTIVATION_ATTEMPTS
  state.focus_pending_handle = nil
end

-- Whether this session has been asked to put its own cheats down.
--
-- True while the walk runs and true afterwards: a quiesce is only ever asked
-- for by a stop, and the stop kills Cheat Engine as soon as this answers. A
-- record switched on in between would be one whose `[DISABLE]` never runs, so
-- the session stops accepting anything that changes the game the moment it is
-- asked, not merely while it is busy.
local function stopping()
  return state.quiesce ~= nil or state.quiesced == true
end

local function recordStartupTransition(recordId, wasInactive)
  -- The first eligible transition is kept, and the status payload stays one ID
  -- however large the plan is.
  if wasInactive and #state.startup_active_ids == 0 and state.descriptor.startup_proof_ids[recordId] then
    state.startup_active_ids = {recordId}
  end
end

local function applyStartup()
  -- A rollback in flight is the only startup work left once the forward path
  -- has failed, and it needs the timer to advance exactly as the forward path
  -- did while it waited for an async activation to settle.
  if state.startup_failed then
    if state.attached then advanceRollback() end
    return
  end
  if state.startup_applied or not state.attached then return end
  -- A stop is putting this session's records down. What Cheat Engine is still
  -- thinking about is let finish, so the quiesce can see that record and put it
  -- down; nothing new is begun, because the stop kills Cheat Engine when the
  -- quiesce answers and a record switched on now would keep its patch.
  if stopping() and not state.startup_pending then return end

  if state.startup_pending then
    local pending = state.startup_pending
    local processing, asyncErr = recordAsyncProcessing(pending.record)
    if asyncErr then
      beginRollback(pending.index)
      result(0, pending.action.record_id, false, nil, nil, asyncErr, "mutation_failed")
      state.startup_pending = nil
      state.startup_failed = true
      return
    end
    if processing then
      pending.waits = pending.waits + 1
      if pending.waits <= MAX_ACTIVATION_WAIT_TICKS then return end
      beginRollback(pending.index)
      result(0, pending.action.record_id, false, nil, nil, "activation timed out", "mutation_failed")
      state.startup_pending = nil
      state.startup_failed = true
      return
    end
    local actual, readValue, activeErr = recordSnapshot(pending.record)
    if activeErr or actual ~= pending.desired then
      beginRollback(pending.index)
      result(0, pending.action.record_id, false, actual, readValue, mutationOutcome(activeErr))
      state.startup_pending = nil
      state.startup_failed = true
      return
    end
    recordStartupTransition(pending.action.record_id, pending.proof_eligible)
    result(0, pending.action.record_id, true, actual, readValue, nil)
    state.startup_index = pending.index
    state.startup_pending = nil
  end
  if stopping() then return end

  -- Startup is resolved one action at a time, in descriptor order, because a
  -- nested control may not exist until the script that creates it has run. The
  -- backend already orders enclosing scripts before the records inside them, so
  -- resolving every record up front asked Cheat Engine for a child that only the
  -- not-yet-executed parent would create: the lookup failed, the parent was
  -- never switched on, and every timer tick repeated the same impossible
  -- preflight. Resolve the next record immediately before acting on it, let CE
  -- settle between levels, and bound the wait so a genuinely absent record still
  -- fails with its exact ID instead of retrying forever.
  local actions = state.descriptor.startup
  while state.startup_index < #actions do
    local index = state.startup_index + 1
    local action = actions[index]
    local record = memoryRecord(action.record_id)
    if not record then
      state.startup_waits = state.startup_waits + 1
      if state.startup_waits > MAX_STARTUP_WAIT_TICKS then
        beginRollback(index)
        result(0, action.record_id, false, nil, nil, "MemoryRecord did not appear after its enclosing scripts ran", "record_missing")
        state.startup_failed = true
      end
      return
    end
    state.startup_waits = 0

    local active, value, readErr = recordSnapshot(record)
    if readErr then
      state.startup_waits = state.startup_waits + 1
      if state.startup_waits > MAX_STARTUP_WAIT_TICKS then
        beginRollback(index)
        result(0, action.record_id, false, nil, nil, tostring(readErr), "record_read_failed")
        state.startup_failed = true
      end
      return
    end
    state.startup_resolved[index] = record
    state.startup_snapshots[index] = { active = active, value = value }

    if action.kind == "value" then
      local ok, message = pcall(function() record.Value = action.value end)
      local readActive, readValue, valueErr = recordSnapshot(record)
      -- Read-back equality, the same rule live Apply uses. Recording success
      -- from a read that merely succeeded let a table clamp, coerce or reject a
      -- saved value while auto-load still reported the cheats were restored.
      if ok and not valueErr and readValue == action.value then
        result(0, action.record_id, true, readActive, readValue, nil)
      else
        beginRollback(index)
        result(0, action.record_id, false, readActive, readValue,
          tostring(message or valueErr or "value did not read back as written"), "mutation_failed")
        state.startup_failed = true
        return
      end
    elseif action.kind == "active" then
      local desired = action.value == "1"
      local ok, message = pcall(function() record.Active = desired end)
      local processing, asyncErr = recordAsyncProcessing(record)
      if ok and not asyncErr and processing then
        state.startup_pending = {
          index = index, action = action, record = record, desired = desired, waits = 0, proof_eligible = active == false and desired,
        }
        return
      end
      local actual, readValue, activeErr = recordSnapshot(record)
      if ok and not asyncErr and not activeErr and actual == desired then
        recordStartupTransition(action.record_id, active == false and desired)
        result(0, action.record_id, true, actual, readValue, nil)
      else
        beginRollback(index)
        result(0, action.record_id, false, actual, readValue, mutationOutcome(message, asyncErr, activeErr))
        state.startup_failed = true
        return
      end
    end

    state.startup_index = index
    -- No eager yield: when the next record is already there, continue in this
    -- tick. Only an action whose record has not materialized yet waits, and it
    -- waits in the branch above.
  end
  state.startup_applied = true
  if #actions > 0 then
    state.focus_sweeps_left = ACTIVATION_ATTEMPTS
    state.focus_pending_handle = nil
  end
end

local function snapshotProcesses()
  state.processes = {}
  local ok, processes = pcall(function() return getProcesslist() end)
  if not ok or type(processes) ~= "table" then return end
  for pid, name in pairs(processes) do
    local numeric = tonumber(pid)
    if numeric and numeric > 0 and numeric <= MAX_APP_ID and type(name) == "string" then
      if #name > MAX_PROCESS_NAME_BYTES then name = "<process name too large>" end
      state.processes[#state.processes + 1] = { pid = numeric, name = name }
    end
  end
  table.sort(state.processes, function(a, b) return a.pid < b.pid end)
  while #state.processes > MAX_PROCESS_ROWS do table.remove(state.processes) end
end

local function isScriptRecord(record)
  -- An Auto Assembler record, which is the one whose deactivation runs a
  -- `[DISABLE]` and frees what its `[ENABLE]` allocated. Cheat Engine's own
  -- documentation says `Script` holds the script when the type is
  -- `vtAutoAssembler`, and that constant is a global of its Lua environment, so
  -- both are asked for and either answering is enough. Neither answering means
  -- this is treated as an ordinary record, which only costs it its place in the
  -- order.
  local typeOk, recordType = pcall(function() return record.Type end)
  if typeOk and type(vtAutoAssembler) == "number" and recordType == vtAutoAssembler then return true end
  local scriptOk, script = pcall(function() return record.Script end)
  return scriptOk and type(script) == "string" and script ~= ""
end

-- Which records are switched on right now, deepest first and scripts last.
--
-- The address list is flat: Cheat Engine's `Count` is every record in the
-- table, nested ones included, and this device reported all 52 of a real
-- table's entries from it. So the list is read once, straight through, and the
-- tree is recovered from each record's own `Parent` rather than by walking
-- `Child` on top of it, which would visit every nested record twice.
--
-- Order is the whole of the care here. A child's bytes live inside the
-- allocation its enclosing script made, so the script goes last: freeing that
-- allocation first would leave every record inside it pointing at memory that
-- is no longer there. Deepest first gives that, and among records at one depth
-- the scripts still go last, because a plain record's address can be a symbol
-- a sibling script registered.
local function recordDepth(record)
  local depth = 0
  local at = record
  while depth <= MAX_QUIESCE_DEPTH do
    local ok, parent = pcall(function() return at.Parent end)
    if not ok or parent == nil then return depth end
    at = parent
    depth = depth + 1
  end
  return depth
end

local function activeRecordsToQuiesce()
  local count = addressListCount()
  if not count then return nil end
  local found = {}
  local limit = math.min(count, MAX_QUIESCE_RECORDS)
  for index = 0, limit - 1 do
    local ok, record = pcall(function() return AddressList.getMemoryRecord(index) end)
    if ok and record ~= nil then
      local activeOk, active = pcall(function() return record.Active end)
      if activeOk and active == true then
        local idOk, id = pcall(function() return record.ID end)
        found[#found + 1] = {
          record = record, id = idOk and tonumber(id) or nil,
          depth = recordDepth(record), script = isScriptRecord(record),
        }
      end
    end
  end
  table.sort(found, function(a, b)
    if a.depth ~= b.depth then return a.depth > b.depth end
    if a.script ~= b.script then return b.script end
    return (a.id or 0) < (b.id or 0)
  end)
  return found
end

-- Name a record the stop could not put down, once however often it is met.
--
-- A record is looked at again on a later sweep, so without this a flag that
-- will not come down would be named as many times as it was tried, and the
-- sentence the user reads is a list of what is still running in their game.
local function markUnsettled(run, id)
  local key = tostring(id or "?")
  if run.named[key] then return end
  run.named[key] = true
  run.unsettled[#run.unsettled + 1] = id or "?"
end

-- The key this record is switched off at, where its own table declares one.
local function switchOffValue(id)
  if id == nil then return nil end
  local values = state.descriptor.switch_off
  if not values then return nil end
  return values[id]
end

-- Whether what was read back is the value that was written.
--
-- Compared as numbers where both are numbers: Cheat Engine returns a record's
-- value formatted for the record's own type, so the key `00000000` written to a
-- record shown as hex reads back as `0`, and comparing the two as text would
-- report a cheat that was switched off perfectly well as one still running.
local function sameValue(written, readBack)
  if written == readBack then return true end
  local a, b = tonumber(written), tonumber(readBack)
  return a ~= nil and b ~= nil and a == b
end

-- Count one record as down, leaving it at its off key where it has one.
--
-- Releasing the freeze is not switching such a cheat off. The record keeps the
-- value it was frozen at, so a table of `0:Disabled/1:Enabled` flags would come
-- down with every flag still at 1 in the running game, which is the leak the
-- whole quiesce exists to close. The write happens after the release, for the
-- same reason the panel does it in that order: the value the game keeps is the
-- one written last.
local function countRecordDown(run, record, id)
  local off = switchOffValue(id)
  if off ~= nil then
    local written = pcall(function() record.Value = off end)
    local readOk, value = pcall(function() return record.Value end)
    if not written or not readOk or not sameValue(off, tostring(value)) then
      markUnsettled(run, id)
      return
    end
  end
  run.put_down = run.put_down + 1
end

-- Report what one quiesce managed, once, and let the stop proceed.
local function finishQuiesce()
  local run = state.quiesce
  if not run then return end
  local unsettled = {}
  for _, entry in ipairs(run.unsettled) do
    unsettled[#unsettled + 1] = tostring(entry)
  end
  -- Explicit branches: `x and nil or y` is always `y` in Lua, so a conditional
  -- written that way would report every quiesce as a failure.
  local value = "put_down=" .. tostring(run.put_down) .. ";unsettled=" .. table.concat(unsettled, ",")
  if #unsettled == 0 then
    result(run.generation, nil, true, nil, value, nil, nil)
  else
    result(run.generation, nil, false, nil, value, "records did not settle", "quiesce_unsettled")
  end
  state.quiesce = nil
  state.quiesced = true
end

-- Switch one record off, and say whether Cheat Engine is still thinking about
-- it. `true` means the walk stops here and the timer comes back for it.
local function putOneDown(run, entry)
  local readOk, stillOn = pcall(function() return entry.record.Active end)
  -- Already off, because a script going down took its children with it.
  if not readOk or stillOn ~= true then return false end
  local ok = pcall(function() entry.record.Active = false end)
  if not ok then
    markUnsettled(run, entry.id)
    return false
  end
  local processing, asyncErr = recordAsyncProcessing(entry.record)
  if not asyncErr and processing then
    run.pending = { record = entry.record, id = entry.id, waits = 0 }
    return true
  end
  local activeOk, active = pcall(function() return entry.record.Active end)
  if activeOk and active == false then countRecordDown(run, entry.record, entry.id)
  else markUnsettled(run, entry.id) end
  return false
end

-- Put one record down per tick, waiting for Cheat Engine the way every other
-- activation here waits. A record that will not settle inside the same bound
-- the rest of this file uses is reported and the walk moves on: the stop it
-- belongs to must never become less reliable than the stop that does none of
-- this.
local function advanceQuiesce()
  local run = state.quiesce
  if not run then return end
  if not state.attached then
    run.unsettled[#run.unsettled + 1] = "detached"
    finishQuiesce()
    return
  end
  run.ticks = (run.ticks or 0) + 1
  if run.ticks > MAX_QUIESCE_TOTAL_TICKS then
    -- Everything not reached is named rather than counted, because what the
    -- reader of this needs is which cheats were left on in a game that is
    -- about to lose the Cheat Engine that could have switched them off.
    if run.pending then markUnsettled(run, run.pending.id) end
    while run.index < #run.records do
      run.index = run.index + 1
      markUnsettled(run, run.records[run.index].id)
    end
    finishQuiesce()
    return
  end
  if run.pending then
    local pending = run.pending
    local processing, asyncErr = recordAsyncProcessing(pending.record)
    if asyncErr then
      markUnsettled(run, pending.id)
      run.pending = nil
    elseif processing then
      pending.waits = pending.waits + 1
      if pending.waits <= MAX_ACTIVATION_WAIT_TICKS then return end
      markUnsettled(run, pending.id)
      run.pending = nil
    else
      local activeOk, active = pcall(function() return pending.record.Active end)
      if activeOk and active == false then countRecordDown(run, pending.record, pending.id)
      else markUnsettled(run, pending.id) end
      run.pending = nil
    end
  end
  while run.index < #run.records do
    run.index = run.index + 1
    if putOneDown(run, run.records[run.index]) then return end
  end
  -- An activation Cheat Engine has not finished is a record that is about to be
  -- switched on, so the walk waits for it before it can say there is nothing
  -- left on. A startup that failed is rolling itself back, and a rollback puts
  -- a record back the way it found it, which for one that was already on means
  -- switching it on again. Bounded by the same number of ticks every other
  -- activation here waits, and by the walk's own bound above it.
  if (state.startup_pending or state.pending_command or state.rollback) and (run.waits or 0) < MAX_ACTIVATION_WAIT_TICKS then
    run.waits = (run.waits or 0) + 1
    return
  end
  -- The list this started from was what was on when it started. Putting a
  -- script down runs the table's own `[DISABLE]`, and a record on its way on
  -- when the stop arrived settles while this walks, so what is switched on now
  -- is asked again rather than assumed. The next tick carries the new walk: a
  -- table that switches a flag back on forever costs a few ticks, not the stop.
  if run.sweeps < MAX_QUIESCE_SWEEPS then
    local again = activeRecordsToQuiesce()
    if again then
      -- Not one already reported as a record that will not come down: trying it
      -- again costs the same wait it already spent, and three sweeps of that
      -- would spend the whole stop on an answer this already has.
      local left = {}
      for _, entry in ipairs(again) do
        if not run.named[tostring(entry.id or "?")] then left[#left + 1] = entry end
      end
      if #left > 0 then
        run.sweeps = run.sweeps + 1
        run.records = left
        run.index = 0
        return
      end
    end
  end
  finishQuiesce()
end

local function executeCommand(command)
  if command.generation <= state.last_generation then return end
  state.last_generation = command.generation
  -- While this session's cheats are being switched off, nothing may switch one
  -- back on. The stop that asked for the quiesce kills Cheat Engine when it
  -- answers, so a record activated behind the walk is one whose `[DISABLE]`
  -- never runs: the patch and the allocation stay in the running game and no
  -- later session can undo them. Reads are left alone, because a panel asking
  -- what is on is not changing anything.
  if stopping() and (command.kind == "set_active" or command.kind == "set_value" or command.kind == "retry_attach") then
    result(command.generation, command.record_id, false, nil, nil,
      "this session is being stopped", "quiesce_in_progress")
    return
  end
  if command.kind == "retry_attach" then
    if command.value and command.value ~= "" then state.target_process = command.value end
    state.startup_applied = false
    state.startup_active_ids = {}
    state.startup_failed = false
    state.startup_rolled_back = nil
    state.rollback = nil
    state.startup_index = 0
    state.startup_waits = 0
    state.startup_pending = nil
    state.startup_resolved = {}
    state.startup_snapshots = {}
    local attached = tryAttach(command.target_pid)
    local attachError = nil
    local attachErrorCode = nil
    if not attached then
      attachError = "attach failed"
      attachErrorCode = "attach_failed"
    end
    result(command.generation, nil, attached, nil, nil, attachError, attachErrorCode)
    if attached then applyStartup() end
    return
  end
  if command.kind == "list_processes" then
    snapshotProcesses()
    result(command.generation, nil, true, nil, nil, nil)
    return
  end
  if command.kind == "quiesce" then
    -- Nothing to put down when there is nothing attached, and saying so is the
    -- answer: the stop that asked for this proceeds either way.
    if not state.attached then
      result(command.generation, nil, false, nil, "put_down=0;unsettled=", "target process is not attached", "target_detached")
      return
    end
    -- One at a time. A second while one is in flight would replace it, and the
    -- first would then never be answered at all.
    if state.quiesce then
      result(command.generation, nil, false, nil, "put_down=0;unsettled=", "a quiesce is already running", "quiesce_unsettled")
      return
    end
    local records = activeRecordsToQuiesce()
    if not records then
      result(command.generation, nil, false, nil, "put_down=0;unsettled=", "AddressList unavailable", "address_list_unavailable")
      return
    end
    state.quiesce = {
      generation = command.generation, records = records, index = 0, put_down = 0,
      unsettled = {}, named = {}, pending = nil, ticks = 0, sweeps = 0, waits = 0,
    }
    advanceQuiesce()
    return
  end
  if not state.attached then
    result(command.generation, command.record_id, false, nil, nil, "target process is not attached", "target_detached")
    return
  end
  local record, err, errorCode = memoryRecord(command.record_id)
  if not record then
    result(command.generation, command.record_id, false, nil, nil, err, errorCode)
    return
  end
  if command.kind == "query" then
    local active, value, readErr = recordSnapshot(record)
    result(command.generation, command.record_id, readErr == nil, active, value, readErr, readErr and "record_read_failed" or nil)
  elseif command.kind == "set_value" then
    state.focus_sweeps_left = ACTIVATION_ATTEMPTS
    state.focus_pending_handle = nil
    local ok, message = pcall(function() record.Value = command.value end)
    local active, value, readErr = recordSnapshot(record)
    if ok and not readErr then result(command.generation, command.record_id, true, active, value, nil)
    else result(command.generation, command.record_id, false, active, value, tostring(message or readErr or "record read failed"), "mutation_failed") end
  elseif command.kind == "set_active" then
    state.focus_sweeps_left = ACTIVATION_ATTEMPTS
    state.focus_pending_handle = nil
    local desired = command.value == "1"
    local ok, message = pcall(function() record.Active = desired end)
    local processing, asyncErr = recordAsyncProcessing(record)
    if ok and not asyncErr and processing then
      state.pending_command = {
        command = command, record = record, desired = desired, waits = 0,
      }
      return
    end
    local actual, value, readErr = recordSnapshot(record)
    if ok and not asyncErr and not readErr and actual == desired then result(command.generation, command.record_id, true, actual, value, nil)
    else result(command.generation, command.record_id, false, actual, value, mutationOutcome(message, asyncErr, readErr)) end
  end
end

local function settlePendingCommand()
  local pending = state.pending_command
  if not pending then return end
  local command = pending.command
  if not state.attached then
    result(command.generation, command.record_id, false, nil, nil, "target process is not attached", "target_detached")
    state.pending_command = nil
    return
  end
  local processing, asyncErr = recordAsyncProcessing(pending.record)
  if asyncErr then
    result(command.generation, command.record_id, false, nil, nil, asyncErr, "mutation_failed")
    state.pending_command = nil
    return
  end
  if processing then
    pending.waits = pending.waits + 1
    if pending.waits <= MAX_ACTIVATION_WAIT_TICKS then return end
    result(command.generation, command.record_id, false, nil, nil, "activation timed out", "mutation_failed")
    state.pending_command = nil
    return
  end
  state.focus_sweeps_left = ACTIVATION_ATTEMPTS
  state.focus_pending_handle = nil
  local actual, value, readErr = recordSnapshot(pending.record)
  if not readErr and actual == pending.desired then
    result(command.generation, command.record_id, true, actual, value, nil)
  else
    result(command.generation, command.record_id, false, actual, value, mutationOutcome(readErr))
  end
  state.pending_command = nil
end

local function pollControl()
  if state.pending_command then return end
  local data = readBounded(state.descriptor.control_path)
  if not data then return end
  local commands = parseControl(data)
  if not commands then return end
  for _, command in ipairs(commands) do
    executeCommand(command)
    if state.pending_command then return end
  end
end

local function statusField(name, value)
  return "F\t" .. name .. "\t" .. percentEncode(tostring(value))
end

-- Keep Cheat Engine's windows down for the life of the session.
--
-- Suppression is installed once, from the main form's first show, because that
-- is when CE maps the window that takes the running game's audio and controller
-- input. It is not enough for a window CE maps later - a form it creates on
-- demand, or a main form something shows again. What is left behind is an empty
-- frame over the game, which the gamepad overlay then draws a focus box around.
--
-- So the sweep is continuous and cheap: ask the application for its own forms
-- about once a second and hide everything again the moment any of them is
-- visible. Suppression is never lifted for any reason, so a visible CE form is
-- always something to undo, and the count is published because a game losing
-- its audio is otherwise impossible to attribute.
-- "visible", "hidden" or "unknown" for one form.
--
-- Per form, not per sweep: one form that throws on `Visible` must not abort the
-- whole enumeration. But it must not read as hidden either - collapsing a
-- getter failure to false let the sweep conclude every window was down while
-- one of them could not be looked at, and skip the hide on that basis.
local function formVisibility(form)
  local ok, visible = pcall(function() return form.Visible end)
  if not ok then return "unknown" end
  return visible == true and "visible" or "hidden"
end

-- "visible", "hidden", or "unknown" when the form set cannot be proven down.
--
-- Unknown is not hidden. Cheat Engine may not expose form enumeration at all, a
-- `.CT` can create forms of its own, and a count past the scan bound leaves
-- entries nobody looked at - in each of those the sweep has no idea whether
-- something is on screen. Treating that as "nothing to do" is what would let a
-- window sit over the game indefinitely, which is the one thing this exists to
-- prevent.
local function ceWindowState()
  local mainOk, mainVisible = pcall(function() return MainForm ~= nil and MainForm.Visible end)
  if mainOk and mainVisible == true then return "visible" end
  if type(getFormCount) ~= "function" or type(getForm) ~= "function" then return "unknown" end
  local countOk, count = pcall(getFormCount)
  if not countOk or type(count) ~= "number" or count ~= math.floor(count) or count < 0 then
    return "unknown"
  end
  -- A form nobody could read leaves the sweep unable to prove the set is down,
  -- but a form that is plainly visible is the stronger answer, so keep looking.
  local uncertain = false
  for index = 0, math.min(count, MAX_ENUMERATED_FORMS) - 1 do
    local formOk, form = pcall(getForm, index)
    if not formOk then
      uncertain = true
    elseif form ~= nil then
      local visibility = formVisibility(form)
      if visibility == "visible" then return "visible" end
      if visibility == "unknown" then uncertain = true end
    end
  end
  if uncertain or count > MAX_ENUMERATED_FORMS then return "unknown" end
  return "hidden"
end

-- The window this Cheat Engine process has in the foreground, if it has one.
--
-- `getFormCount` only reaches forms assigned to the application, and a message
-- dialog is not one of them: on the target a 360x108 "Confirmation" sat over
-- the game while the form sweep reported everything hidden and the suppression
-- counter stayed at zero. A window owned by this process and in the foreground
-- is on screen whatever it is made of, and it is the foreground window that the
-- compositor puts on the plane the game was using.
local function foregroundCEWindow()
  if type(getForegroundWindow) ~= "function"
      or type(getWindowProcessID) ~= "function"
      or type(getCheatEngineProcessID) ~= "function" then
    return nil
  end
  local handleOk, handle = pcall(getForegroundWindow)
  if not handleOk or type(handle) ~= "number" or handle == 0 then return nil end
  local pidOk, pid = pcall(getWindowProcessID, handle)
  local ceOk, cePid = pcall(getCheatEngineProcessID)
  if not pidOk or not ceOk then return nil end
  if type(pid) ~= "number" or type(cePid) ~= "number" or pid ~= cePid then return nil end
  return handle
end

-- Whether the main form is provably down.
--
-- A main form still up means the hide has work left to do, and closing anything
-- before that is premature.
local function mainFormIsHidden()
  local ok, visible = pcall(function() return MainForm ~= nil and MainForm.Visible end)
  return ok and visible == false
end

-- Whether this foreground window is the main form.
--
-- Closing the main form would end the session, and a window this process owns
-- can be the main form while it is hidden: Wine leaves the foreground on a
-- window that was hidden rather than destroyed, so "the main form is down" is
-- not evidence that this handle is something else. Ask for the handle, fall
-- back to the caption, and when neither can be read say so - the caller then
-- closes nothing, because it cannot tell what it would be closing.
--
-- `Handle` is not in Cheat Engine's documented Lua surface, so it is read
-- through pcall and treated as absent when it is not there.
local function isMainFormWindow(handle)
  local handleOk, mainHandle = pcall(function() return MainForm ~= nil and MainForm.Handle end)
  if handleOk and type(mainHandle) == "number" and mainHandle ~= 0 then
    return handle == mainHandle
  end
  local captionOk, mainCaption = pcall(function() return MainForm ~= nil and MainForm.Caption end)
  if captionOk and type(mainCaption) == "string" and mainCaption ~= "" and type(getWindowCaption) == "function" then
    local windowOk, caption = pcall(getWindowCaption, handle)
    if windowOk and type(caption) == "string" then
      return caption == mainCaption
    end
  end
  return nil
end

-- Whether this handle is one of the application's own forms.
--
-- "form", "not_form", or "unknown" when the form set cannot be walked. Hiding
-- is what deals with a form, and the contract is that anything the hide sweep
-- reaches is never closed: a Cheat Engine tool window and a form an authorized
-- table created for itself are both in this list, and Wine leaves the
-- foreground on a window that was hidden rather than destroyed, so a form the
-- sweep already put down can still be the handle in front. Only the main form
-- was exempt before, which made every other form closable the sweep after it
-- was hidden.
--
-- `Handle` is not in Cheat Engine's documented Lua surface, so it is read
-- through pcall and the caption is the fallback, exactly as the main form's own
-- identity check does it. A form neither of them can identify leaves the answer
-- unknown, and the caller then closes nothing.
local function enumeratedFormState(handle)
  if type(getFormCount) ~= "function" or type(getForm) ~= "function" then return "unknown" end
  local countOk, count = pcall(getFormCount)
  if not countOk or type(count) ~= "number" or count ~= math.floor(count) or count < 0 then
    return "unknown"
  end
  local caption = nil
  if type(getWindowCaption) == "function" then
    local captionOk, text = pcall(getWindowCaption, handle)
    if captionOk and type(text) == "string" and text ~= "" then caption = text end
  end
  local uncertain = count > MAX_ENUMERATED_FORMS
  for index = 0, math.min(count, MAX_ENUMERATED_FORMS) - 1 do
    local formOk, form = pcall(getForm, index)
    if not formOk or form == nil then
      uncertain = true
    else
      local formHandleOk, formHandle = pcall(function() return form.Handle end)
      if formHandleOk and type(formHandle) == "number" and formHandle ~= 0 then
        if formHandle == handle then return "form" end
      else
        local formCaptionOk, formCaption = pcall(function() return form.Caption end)
        if formCaptionOk and type(formCaption) == "string" and formCaption ~= "" then
          if caption ~= nil and formCaption == caption then return "form" end
        else
          uncertain = true
        end
      end
    end
  end
  if uncertain then return "unknown" end
  return "not_form"
end

-- Cut on a character boundary, never inside one.
--
-- The status is decoded as UTF-8 and a value that is not valid UTF-8 fails the
-- whole parse, which reports a healthy session as unreadable protocol state.
-- A caption is arbitrary text from a table or from Cheat Engine, so a byte
-- limit has to step back off any continuation byte it lands on.
local function truncateUtf8(text, limit)
  if #text <= limit then return text end
  local cut = limit
  while cut > 0 do
    local following = string.byte(text, cut + 1)
    if following == nil or following < 0x80 or following >= 0xC0 then break end
    cut = cut - 1
  end
  return string.sub(text, 1, cut)
end

local function windowCaption(handle)
  if type(getWindowCaption) ~= "function" then return nil end
  local ok, caption = pcall(getWindowCaption, handle)
  if not ok or type(caption) ~= "string" or caption == "" then return nil end
  local bounded = truncateUtf8(caption, MAX_DIALOG_CAPTION_BYTES)
  if bounded == "" then return nil end
  return bounded
end

-- Take down a Cheat Engine window that hiding cannot reach.
--
-- The order is deliberately slow. Hiding runs first and is harmless, so a
-- window the form sweep can put down is never closed; only one still in the
-- foreground a sweep later is. A hidden helper window this process makes for
-- itself is never in the foreground at all, so it is never a candidate.
--
-- Closing answers a dialog on the user's behalf, which is why what was
-- dismissed is published rather than swallowed: over a running game the dialog
-- cannot be read or answered by anyone, and leaving it there costs the game its
-- picture, its audio or its controller.
local function dismissForegroundWindow()
  local handle = foregroundCEWindow()
  if handle == nil then
    state.pending_dialog = nil
    state.dismissed_dialog = nil
    return false
  end
  -- Answered-once applies while that window is still the one in front. Once it
  -- is gone the handle is free to be reused by something else, and the next
  -- dialog to hold it is a new dialog.
  if state.dismissed_dialog ~= nil and state.dismissed_dialog ~= handle then
    state.dismissed_dialog = nil
  end
  -- Seen once is not seen twice: give the hide that just ran a sweep to work.
  if state.pending_dialog ~= handle then
    state.pending_dialog = handle
    return false
  end
  -- Answered once. A window that ignores the close would otherwise be closed
  -- again every second, and the published count would describe a session that
  -- met a thousand dialogs instead of the one it actually met.
  if state.dismissed_dialog == handle then return false end
  if not mainFormIsHidden() or type(sendMessage) ~= "function" then return false end
  local isMain = isMainFormWindow(handle)
  if isMain == nil or isMain == true then return false end
  -- Everything the hide sweep can reach is the hide sweep's to deal with. Only
  -- a window that is provably not one of the application's forms is closed,
  -- which is exactly what the message dialog measured on the device is.
  if enumeratedFormState(handle) ~= "not_form" then return false end
  local caption = windowCaption(handle)
  if not pcall(sendMessage, handle, WM_CLOSE, 0, 0) then return false end
  state.dialogs_dismissed = state.dialogs_dismissed + 1
  state.last_dialog = caption or "(untitled window)"
  state.dismissed_dialog = handle
  state.pending_dialog = nil
  return true
end

-- The captions of the windows the attached game owns.
--
-- Cheat Engine documents `getWindowlist` as "{pid,{id,caption}}", and the build
-- this ships against answers with something else: a table keyed by process ID
-- whose value is a list of captions, with no window handle anywhere in it. That
-- was measured on the device rather than read, because reading it was wrong.
local function gameWindowCaptions(processId)
  if type(getWindowlist) ~= "function" or processId == nil or processId == 0 then return {} end
  local ok, list = pcall(getWindowlist)
  if not ok or type(list) ~= "table" then return {} end
  -- The device answers with a numeric key; a build that keys by the same number
  -- as a string would otherwise silently look like a game with no windows.
  local windows = list[processId]
  if type(windows) ~= "table" then windows = list[tostring(processId)] end
  if type(windows) ~= "table" then return {} end
  local captions = {}
  for _, entry in pairs(windows) do
    if #captions >= MAX_ENUMERATED_WINDOWS then break end
    -- A caption outright, or the caption half of the documented pair.
    local caption = entry
    if type(entry) == "table" then caption = entry[2] or entry[1] end
    if type(caption) == "string" and caption ~= "" then
      captions[#captions + 1] = caption
    end
  end
  return captions
end

-- Whether a window is minimized, asked of Windows rather than inferred.
--
-- This is the whole difference between restoring a game that was pushed aside
-- and dropping a healthy one out of fullscreen, because `SC_RESTORE` sent to a
-- window that is maximized rather than minimized means "back to windowed size".
-- Nothing in Cheat Engine's own window API answers it: there is no placement,
-- no rectangle and no style in it. `IsIconic` answers it, and Cheat Engine can
-- reach it - `executeCodeLocalEx` calls a function in Cheat Engine's own
-- process by symbol name, which is how Cheat Engine's own shipped autorun
-- scripts call `DrawIconEx`, `ExtractIconA` and `ntdll.RtlGetVersion`. The
-- window belongs to the game rather than to Cheat Engine, and `IsIconic`
-- answers for any window in the session.
-- Where a symbol is, or why it could not be found.
--
-- Nothing is ever called by name. A local call runs code inside Cheat Engine's
-- own process, and a name it cannot resolve does not fail politely: on the
-- device the added call form took Cheat Engine down with an exception rather
-- than returning anything, and a crash here costs the user their session. The
-- address is resolved first and only a resolved one is ever called.
-- Cheat Engine has two entry points into its own symbol table and they are not
-- the same lookup. `getAddressSafe(name, true)` goes to the self handler's
-- `getAddressFromName`; `getAddress(name, true)` goes to `getAddressFromNameL`,
-- which is also what `executeCodeLocal` uses to turn a name into an address.
-- On the device the first resolves nothing in `user32` at all, which says
-- nothing about the second, and it is the second that Cheat Engine's own
-- scripts rely on when they call `IsWindowVisible` by name. So the lookup is
-- made through `getAddress`, and still only ever as a lookup: the address it
-- returns is what gets called, never the name.
local function symbolTableAddress(symbol)
  if type(getAddress) ~= "function" then return nil, "getAddress is not available" end
  local ok, address = pcall(getAddress, symbol, true)
  if not ok then return nil, "getAddress " .. symbol .. ": " .. tostring(address) end
  if type(address) ~= "number" or address == 0 then
    return nil, "getAddress " .. symbol .. ": unresolved"
  end
  return address
end

-- The same two calls, as the module and the export name they actually are.
--
-- The symbol table is one way to turn those into an address. The module's own
-- image is the other, and on this device it is the only one that works: with a
-- game attached, Cheat Engine's own symbol table answers nothing at all, not
-- even for Cheat Engine's own executable, while the module list beside it is
-- complete and correct.
local IMAGE_EXPORTS = {
  ["SetForegroundWindow"] = {module = "user32.dll", name = "SetForegroundWindow"},
  ["IsWindowVisible"] = {module = "user32.dll", name = "IsWindowVisible"},
  ["IsIconic"] = {module = "user32.dll", name = "IsIconic"},
  ["user32.IsIconic"] = {module = "user32.dll", name = "IsIconic"},
  ["PostMessageW"] = {module = "user32.dll", name = "PostMessageW"},
  ["user32.PostMessageW"] = {module = "user32.dll", name = "PostMessageW"},
}

local function readLocalU32(address)
  if type(readIntegerLocal) ~= "function" then return nil end
  local ok, value = pcall(readIntegerLocal, address)
  if not ok or type(value) ~= "number" then return nil end
  value = math.floor(value)
  if value < 0 then value = value + IMAGE_U32 end
  if value < 0 or value >= IMAGE_U32 then return nil end
  return value
end

local function readLocalU16(address)
  if type(readSmallIntegerLocal) ~= "function" then return nil end
  local ok, value = pcall(readSmallIntegerLocal, address)
  if not ok or type(value) ~= "number" then return nil end
  return math.floor(value) % IMAGE_U16
end

-- Where a module of this exact name is loaded in Cheat Engine's own process.
--
-- `enumModules()` answers for the process Cheat Engine has open, and only with
-- none open is that Cheat Engine itself; asking it for an explicit process id
-- answers with nothing at all here. So the list is read before the game is
-- attached and the base is kept for the session. A base read while a game is
-- open would be a module of the game's, at the game's address, and calling
-- into that from inside Cheat Engine is exactly the mistake this file exists
-- to avoid.
local function localModuleBase(name)
  local cached = state.local_module_bases[name]
  if cached ~= nil then return cached or nil end
  if type(enumModules) ~= "function" or type(getOpenedProcessID) ~= "function" then return nil end
  local openedOk, opened = pcall(getOpenedProcessID)
  if not openedOk or type(opened) ~= "number" or opened ~= 0 then return nil end
  local ok, modules = pcall(enumModules)
  if not ok or type(modules) ~= "table" then return nil end
  local count = #modules
  -- An empty list is the one state that a moment's wait can change, and the
  -- only one worth holding the attach for.
  state.local_modules_transient = count == 0
  local wanted = string.lower(name)
  for index = 1, math.min(count, MAX_ENUMERATED_MODULES) do
    local module = modules[index]
    if type(module) == "table" and type(module.Name) == "string"
        and string.lower(module.Name) == wanted then
      local base = module.Address
      if type(base) == "number" and base > 0 and math.floor(base) == base then
        state.local_module_bases[name] = base
        return base
      end
      return nil
    end
  end
  -- An empty list is Cheat Engine not answering yet, which a later attempt may
  -- correct, and a list longer than this walks is not an answer about what is
  -- in it either. Only a list read whole says the module is absent.
  if count > 0 and count <= MAX_ENUMERATED_MODULES then
    state.local_module_bases[name] = false
  end
  return nil
end

-- Read the module list once, while nothing is open.
--
-- Everything else about the restore capability happens on the window sweep, and
-- by then the game is attached and `enumModules()` answers for the game. This
-- is the one moment the list is Cheat Engine's own, so it is taken here whether
-- or not the rest of the discovery is ready to use it: the main form may not
-- have a window yet, and waiting for one would cost the only chance to read
-- this.
local function primeLocalModuleBases()
  for _, export in pairs(IMAGE_EXPORTS) do localModuleBase(export.module) end
end

-- Whether the one reading of that list this session gets is still worth waiting
-- for before attaching.
--
-- Only while something is still unanswered, only while the last answer was the
-- empty list that says Cheat Engine has not built it yet, and only for the
-- bounded number of ticks above. A list that answered and did not contain the
-- module is an answer, and waiting on it would delay the attach for nothing.
local function waitingForLocalModules()
  if state.module_capture_ticks_left <= 0 then return false end
  if not state.local_modules_transient then return false end
  for _, export in pairs(IMAGE_EXPORTS) do
    if state.local_module_bases[export.module] == nil then return true end
  end
  return false
end

-- One export, read out of the loaded image rather than asked of anything.
--
-- Every read is of Cheat Engine's own memory, bounded, and checked before it is
-- used: the image has to start with the two signatures, the export directory
-- has to be inside it, and the name has to be found exactly. The names in an
-- export directory are sorted, so this is a binary search rather than a walk of
-- a thousand strings. A forwarded export is refused rather than followed,
-- because the address in that slot is text and not code.
local function imageExportAddress(symbol)
  local export = IMAGE_EXPORTS[symbol]
  if export == nil then return nil, nil end
  local base = localModuleBase(export.module)
  if base == nil then
    return nil, "image " .. symbol .. ": " .. export.module .. " is not in Cheat Engine's module list"
  end
  local function refuse(why)
    return nil, "image " .. symbol .. ": " .. why
  end
  if readLocalU16(base) ~= IMAGE_DOS_SIGNATURE then return refuse("not an image") end
  local lfanew = readLocalU32(base + IMAGE_LFANEW_OFFSET)
  if lfanew == nil or lfanew < IMAGE_LFANEW_OFFSET or lfanew > MAX_IMAGE_LFANEW then
    return refuse("no PE header")
  end
  local header = base + lfanew
  if readLocalU32(header) ~= IMAGE_NT_SIGNATURE then return refuse("not a PE header") end
  local optional = header + IMAGE_OPTIONAL_HEADER_OFFSET
  local magic = readLocalU16(optional)
  local directoryOffset, countOffset = nil, nil
  if magic == IMAGE_PE32PLUS_MAGIC then
    directoryOffset = IMAGE_EXPORT_DIRECTORY_OFFSET_64
    countOffset = IMAGE_DIRECTORY_COUNT_OFFSET_64
  elseif magic == IMAGE_PE32_MAGIC then
    directoryOffset = IMAGE_EXPORT_DIRECTORY_OFFSET_32
    countOffset = IMAGE_DIRECTORY_COUNT_OFFSET_32
  else
    return refuse("unknown image format")
  end
  -- The header says how much of itself there is, and how many data directories
  -- it carries. Both have to cover entry zero before it is read: they are the
  -- fields the format provides for exactly this, and without them the read is
  -- of whatever follows the header.
  local optionalSize = readLocalU16(header + IMAGE_SIZE_OF_OPTIONAL_HEADER_OFFSET)
  if optionalSize == nil or optionalSize < directoryOffset + IMAGE_DIRECTORY_ENTRY_BYTES then
    return refuse("optional header too short for a data directory")
  end
  local directoryCount = readLocalU32(optional + countOffset)
  if directoryCount == nil or directoryCount < 1 then return refuse("no data directories") end
  local imageSize = readLocalU32(optional + IMAGE_SIZE_OF_IMAGE_OFFSET)
  if imageSize == nil or imageSize == 0 then return refuse("no image size") end
  -- Every read below is of an address derived from a number in this image's own
  -- headers, so each of them is proven to lie wholly inside the image first. An
  -- address that leaves the image is not unreadable here: this is Cheat
  -- Engine's own memory, so it would return some other module's bytes.
  local function inside(rva, size)
    return type(rva) == "number" and type(size) == "number"
      and rva > 0 and size >= 0 and size <= imageSize and rva <= imageSize - size
  end
  local directory = optional + directoryOffset
  local exportRva = readLocalU32(directory)
  local exportSize = readLocalU32(directory + 4)
  if not inside(exportRva, exportSize) then return refuse("export directory outside the image") end
  if exportSize < IMAGE_EXPORT_DIRECTORY_BYTES then return refuse("export directory too short") end
  local directoryStart = base + exportRva
  local functionCount = readLocalU32(directoryStart + IMAGE_EXPORT_FUNCTION_COUNT_OFFSET)
  local count = readLocalU32(directoryStart + IMAGE_EXPORT_NAME_COUNT_OFFSET)
  local functions = readLocalU32(directoryStart + IMAGE_EXPORT_FUNCTIONS_OFFSET)
  local names = readLocalU32(directoryStart + IMAGE_EXPORT_NAMES_OFFSET)
  local ordinals = readLocalU32(directoryStart + IMAGE_EXPORT_ORDINALS_OFFSET)
  if count == nil or count == 0 or count > MAX_IMAGE_EXPORT_NAMES then return refuse("no export names") end
  if functionCount == nil or functionCount == 0 or functionCount > MAX_IMAGE_EXPORT_NAMES then
    return refuse("no exports")
  end
  -- Whole tables, not first entries: a table that starts inside the image and
  -- runs off the end is read off the end at its last index.
  if not inside(names, 4 * count) then return refuse("export name table outside the image") end
  if not inside(ordinals, 2 * count) then return refuse("export ordinal table outside the image") end
  if not inside(functions, 4 * functionCount) then
    return refuse("export address table outside the image")
  end
  local low, high, steps = 0, count - 1, 0
  while low <= high and steps < MAX_IMAGE_EXPORT_STEPS do
    steps = steps + 1
    local middle = math.floor((low + high) / 2)
    local nameRva = readLocalU32(base + names + 4 * middle)
    if not inside(nameRva, 1) then return refuse("unreadable export name") end
    -- A name is read only as far as the image goes, and one that runs to the
    -- last byte without ending is not a name.
    local room = imageSize - nameRva
    if room > MAX_IMAGE_EXPORT_NAME_BYTES then room = MAX_IMAGE_EXPORT_NAME_BYTES end
    local readOk, text = pcall(readStringLocal, base + nameRva, room)
    if not readOk or type(text) ~= "string" then return refuse("unreadable export name") end
    if #text >= room then return refuse("unterminated export name") end
    if text == export.name then
      local ordinal = readLocalU16(base + ordinals + 2 * middle)
      if ordinal == nil or ordinal >= functionCount then return refuse("bad ordinal") end
      local rva = readLocalU32(base + functions + 4 * ordinal)
      if rva == nil or rva == 0 or rva >= imageSize then return refuse("bad export address") end
      if rva >= exportRva and rva < exportRva + exportSize then return refuse("forwarded export") end
      return base + rva
    elseif text < export.name then
      low = middle + 1
    else
      high = middle - 1
    end
  end
  return refuse("not exported")
end

-- Where a symbol is, or why it could not be found, through either route.
local function localAddress(symbol)
  local address, why = symbolTableAddress(symbol)
  if address ~= nil then return address end
  local fromImage, imageWhy = imageExportAddress(symbol)
  if fromImage ~= nil then return fromImage end
  if imageWhy == nil then return nil, why end
  return nil, (why and (why .. "; " .. imageWhy)) or imageWhy
end

local function askIsIconic(call, symbol, address, handle)
  local invoke = _G[call]
  if type(invoke) ~= "function" then return nil, call .. " is not available" end
  local ok, result = pcall(invoke, address, handle)
  if not ok then return nil, call .. " " .. symbol .. ": " .. tostring(result) end
  if type(result) ~= "number" or result ~= result or result == math.huge or result == -math.huge then
    return nil, call .. " " .. symbol .. ": returned " .. type(result)
  end
  return math.floor(result) % BOOL_MASK ~= 0
end

-- The symbol that answers it here, proven before it is used, or nil.
--
-- A call that returns something other than what this thinks it does would send
-- a restore to every window it was asked about, which is the exact damage the
-- question exists to prevent. So the answer is never taken on the strength of
-- the symbol resolving: it is proven against a window whose state is already
-- known. The main form is that window. It exists, CE Decky keeps it hidden and
-- never minimizes it, so a mechanism that calls it minimized is not answering
-- this question, and is refused for the rest of the session.
--
-- Until that proof has actually been made the capability does not exist, which
-- is not the same as being refused: reading the form's handle is what
-- materializes it, and asking again on the next sweep is all a form that had
-- none yet needs. Nothing is sent to any window in the meantime.
local function iconicSymbol()
  if state.iconic_symbol ~= nil then
    return state.iconic_symbol or nil
  end
  if type(executeCodeLocalEx) ~= "function" and type(executeCodeLocal) ~= "function" then
    state.iconic_symbol = false
    state.restore_reason = "no-local-call"
    return nil
  end
  local handleOk, mainHandle = pcall(function() return MainForm ~= nil and MainForm.Handle end)
  if not handleOk or type(mainHandle) ~= "number" or mainHandle == 0 then
    -- Not a refusal: the handle materializes when the form is read, and the
    -- next sweep is all this needs. Reported so a bridge that never gets one
    -- is not indistinguishable from a call that answered wrongly.
    state.restore_reason = "no-window"
    return nil
  end
  -- A symbol that returned nothing usable and one that answered "the hidden
  -- main form is minimized" are different faults with different fixes, and
  -- neither is the missing-call case above. The first failure is kept whole,
  -- because which call refused and what it said is the part a report needs.
  -- What resolves here and what does not, in one line, when nothing answered.
  --
  -- "this name is wrong" and "no symbol in that module can be reached from this
  -- process at all" are different dead ends and only one of them has a way
  -- forward, so the report names every symbol tried, including the one the
  -- other half of the capability needs.
  local function resolutionReport()
    local parts = {}
    for _, group in ipairs({ICONIC_SYMBOLS, POST_MESSAGE_SYMBOLS}) do
      for _, symbol in ipairs(group) do
        local address = localAddress(symbol)
        parts[#parts + 1] = symbol .. (address ~= nil and "=ok" or "=unresolved")
      end
    end
    return table.concat(parts, " ")
  end
  local answered = false
  -- Whether the name was found at all. A name that resolves and then produces
  -- nothing usable is a call that does not work here, which waiting cannot
  -- change; a name that does not resolve may simply not be in the table yet.
  local resolved = false
  local function note(why)
    if why ~= nil and state.restore_error == nil then state.restore_error = why end
  end
  for _, symbol in ipairs(ICONIC_SYMBOLS) do
    local address, resolveWhy = localAddress(symbol)
    note(resolveWhy)
    if address ~= nil then
      resolved = true
      for _, call in ipairs(LOCAL_CALLS) do
        local iconic, why = askIsIconic(call, symbol, address, mainHandle)
        if iconic == false then
          state.iconic_symbol = symbol
          state.iconic_call = call
          state.iconic_address = address
          state.restore_error = nil
          return symbol
        end
        if iconic == true then answered = true end
        note(why)
      end
    end
  end
  if answered then
    -- A call that resolves and then reports Cheat Engine's own hidden main form
    -- as minimized is answering some other question, and that is the one
    -- failure that would send a restore to a healthy game. Refused outright.
    state.iconic_symbol = false
    state.restore_reason = "iconic-disagrees"
    return nil
  end
  -- Appended, not substituted: what the call said and what resolves are both
  -- needed, and a call that failed for its own reasons still has to be read.
  local report = resolutionReport()
  state.restore_error = state.restore_error and (state.restore_error .. "; " .. report) or report
  -- Nothing resolving is not the same as nothing being resolvable. Cheat Engine
  -- builds its own symbol table while it starts and the bridge asks during that
  -- startup: on the device every name in `user32` came back unresolved at that
  -- moment and resolved normally a moment later. So the answer is only the
  -- session's answer once the table has had the whole budget to appear. A name
  -- that did resolve has already had its answer: nothing about that improves by
  -- being asked again.
  if not resolved and state.restore_attempts < RESTORE_DISCOVERY_SWEEPS then
    state.restore_reason = "symbols-unresolved"
    return nil
  end
  state.iconic_symbol = false
  state.restore_reason = "iconic-unanswered"
  return nil
end

-- Where `PostMessageW` is, or nil.
--
-- Resolved rather than called, because there is no address this one could be
-- tried against harmlessly: posting to a window handle of zero puts the message
-- on this thread's own queue. The symbol table is the whole check.
local function postMessageAddress()
  if state.post_message ~= nil then
    return state.post_message or nil
  end
  -- The post carries four parameters, so it needs the many-parameter form
  -- specifically. The one-parameter form can prove the minimized question and
  -- still be unable to send anything, and an address resolved for a call that
  -- cannot be made is not a capability. That absence is a property of the build
  -- and settles immediately; an unresolved name is not.
  --
  -- Only the call is required here. Which of the two ways of turning a name
  -- into an address is available is `localAddress`'s question, and refusing on
  -- `getAddress` at this layer would have refused a Cheat Engine that can read
  -- the export out of the module and had already proven the other half that way.
  if type(executeCodeLocalEx) ~= "function" then
    state.post_message = false
    return nil
  end
  for _, symbol in ipairs(POST_MESSAGE_SYMBOLS) do
    local address, why = localAddress(symbol)
    if address ~= nil then
      state.post_message = address
      return address
    end
    -- Kept, because by this point the question half has succeeded and cleared
    -- the last refusal: without this the status would name a stopping point
    -- with nothing to say about it.
    if why ~= nil then
      state.restore_error = state.restore_error and (state.restore_error .. "; " .. why) or why
    end
  end
  if state.restore_attempts >= RESTORE_DISCOVERY_SWEEPS then
    state.post_message = false
  end
  return nil
end

-- Both halves or neither.
--
-- Seeing that a game is minimized and having no way to ask it back is not a
-- capability, and a game left dark for the second reason has to read the same
-- as one left dark for the first, or the panel would say the question could be
-- answered and then nothing would happen.
local function discoverRestore()
  if iconicSymbol() == nil then return nil end
  local address = postMessageAddress()
  if address == nil then
    if state.post_message == false then
      -- The pair is refused, because half of it is no capability at all. What
      -- is recorded is still the half that actually failed: `minimized_query`
      -- says the question cannot be answered, and here it can - what cannot be
      -- done is asking the game back. A report that names the wrong half sends
      -- whoever reads it to look at a call that is working.
      state.iconic_symbol = false
      state.restore_reason = "no-post"
    else
      state.restore_reason = "symbols-unresolved"
    end
    return nil
  end
  state.restore_reason = "ready"
  return address
end

-- The same discovery, re-asked while its answer can still change, with one
-- reload of Cheat Engine's own symbol table behind it.
--
-- The self handler can be asked before it has anything to answer with. That is
-- exactly what happens here: the bridge loads before Cheat Engine has finished
-- starting, and the first thing it asks for is a `user32` export. Treating that
-- first "unresolved" as the session's answer is what left a game dark for the
-- rest of a session on the target, so an unsettled capability is asked again on
-- every window sweep until it answers or the budget above runs out. A settled
-- refusal is never re-asked, and neither is a proven capability re-proven.
local function restoreCapability()
  if state.iconic_symbol == false then return nil end
  if state.iconic_symbol ~= nil and type(state.post_message) == "number" then
    return state.post_message
  end
  -- Each attempt reports itself. Otherwise the status would carry whatever the
  -- first sweep said for the rest of the session.
  state.restore_error = nil
  local address = discoverRestore()
  if address ~= nil then return address end
  if state.iconic_symbol == false then return nil end
  -- A main form with no handle yet is a different wait: the handle materializes
  -- when the form is read, and that is not the symbol table being late, so it
  -- does not spend the budget kept for one.
  if state.restore_reason ~= "no-window" then
    state.restore_attempts = state.restore_attempts + 1
  end
  if state.symbols_reloaded or state.restore_reason == "no-window" then return nil end
  -- Reloading a table Cheat Engine has not finished building corrects nothing,
  -- so the one reload is kept until waiting alone has plainly not settled it.
  if state.restore_attempts < RESTORE_RELOAD_AFTER_SWEEPS then return nil end
  state.symbols_reloaded = true
  if type(reinitializeSelfSymbolhandler) ~= "function" then return nil end
  if not pcall(reinitializeSelfSymbolhandler, true) then return nil end
  -- Everything discovery concluded was concluded against the old table.
  state.iconic_symbol = nil
  state.iconic_call = nil
  state.iconic_address = nil
  state.post_message = nil
  state.restore_reason = nil
  state.restore_error = nil
  return discoverRestore()
end

-- Focus exports have their own discovery budget: a settled restore pair must
-- never prevent late focus exports from becoming available.
local function focusCapability()
  if state.focus_address ~= nil and state.visible_address ~= nil then return true end
  if state.focus_discovery_attempts >= RESTORE_DISCOVERY_SWEEPS then return false end
  state.focus_discovery_attempts = state.focus_discovery_attempts + 1
  for _, export in ipairs({{"focus_address", "SetForegroundWindow"}, {"visible_address", "IsWindowVisible"}}) do
    if state[export[1]] == nil then
      local address, why = localAddress(export[2])
      state[export[1]] = address
      if why then state.focus_error = boundedStatusText(export[2] .. ": " .. why) end
    end
  end
  return state.focus_address ~= nil and state.visible_address ~= nil
end

-- Cache only a route that returned a usable Win32 BOOL. FALSE is a valid
-- refusal and must never trigger a second activation through another route.
local function focusCall(key, symbol, address, handle)
  local calls = state[key] and {state[key]} or LOCAL_CALLS
  for _, call in ipairs(calls) do
    local answer, why = askIsIconic(call, symbol, address, handle)
    if answer ~= nil then state[key] = call; return answer end
    state.focus_error = boundedStatusText(why)
  end
  if state[key] then
    local failed = state[key]
    state[key] = nil
    for _, call in ipairs(LOCAL_CALLS) do
      if call ~= failed then
        local answer, why = askIsIconic(call, symbol, address, handle)
        if answer ~= nil then state[key] = call; return answer end
        state.focus_error = boundedStatusText(why)
      end
    end
  end
  return nil
end

-- Ask one window to come back, without waiting for it to do so.
local function postRestore(address, handle)
  local ok, result = pcall(executeCodeLocalEx, address, handle, WM_SYSCOMMAND, SC_RESTORE, 0)
  if not ok or type(result) ~= "number" then return false end
  return math.floor(result) % BOOL_MASK ~= 0
end

-- Tell a window that has come back that its application is active.
--
-- Sent only to a window this session actually asked back, and only once it is
-- no longer minimized, so the game has already handled the restore when this
-- arrives. A window that was never pushed aside is never told anything.
local function postActivate(address, handle)
  local ok, result = pcall(executeCodeLocalEx, address, handle, WM_ACTIVATEAPP, 1, 0)
  if not ok or type(result) ~= "number" then return false end
  return math.floor(result) % BOOL_MASK ~= 0
end

-- True, false, or nil when this cannot be established for this window.
local function windowIsMinimized(handle)
  -- A missing PostMessage route refuses restoration, not this proven query.
  -- Focus still needs to exclude minimized windows when only Local is usable.
  if state.iconic_address ~= nil and state.iconic_call ~= nil then
    return (askIsIconic(state.iconic_call, "IsIconic", state.iconic_address, handle))
  end
  local symbol = iconicSymbol()
  if symbol == nil then return nil end
  -- The call form that proved the symbol, not whichever is tried first: the
  -- pair was proven together and only the pair is known to answer.
  return (askIsIconic(state.iconic_call, state.iconic_symbol, state.iconic_address, handle))
end

-- Every top-level window the exact attached process owns.
--
-- A caption is neither an identity nor unique. `findWindow` answers with the
-- first window in the system wearing the caption it was given, and on the
-- device the game's own "Default IME" caption resolved to a window belonging to
-- an entirely different process: rejecting that handle is right, but it is not
-- evidence that the game has no window of its own by that name, and asking for
-- the caption again returns the same foreign handle forever. So the list is
-- built by walking the system's own top-level chain and keeping what the exact
-- attached process owns, which needs no caption at all. `getWindow` is Cheat
-- Engine's wrapper around the Win32 call of the same name; the caption lookup
-- remains as a fallback for a build that does not expose it.
local function gameWindowHandles(processId, seed)
  local handles = {}
  local seen = {}
  local function keep(handle)
    if type(handle) ~= "number" or handle == 0 or seen[handle] then return end
    seen[handle] = true
    if #handles >= MAX_ENUMERATED_WINDOWS then return end
    local ownerOk, owner = pcall(getWindowProcessID, handle)
    if ownerOk and owner == processId then handles[#handles + 1] = handle end
  end
  if type(getWindow) == "function" and type(seed) == "number" and seed ~= 0 then
    local firstOk, first = pcall(getWindow, seed, GW_HWNDFIRST)
    local handle = (firstOk and type(first) == "number" and first ~= 0) and first or seed
    local walked = 0
    -- The chain is a system-wide list and a sweep runs every second, so the
    -- walk is bounded and never revisits a handle it has already answered for.
    while type(handle) == "number" and handle ~= 0 and walked < MAX_ENUMERATED_WINDOWS and not seen[handle] do
      keep(handle)
      walked = walked + 1
      local nextOk, following = pcall(getWindow, handle, GW_HWNDNEXT)
      if not nextOk then break end
      handle = following
    end
  end
  if type(findWindow) == "function" then
    for _, caption in ipairs(gameWindowCaptions(processId)) do
      local foundOk, handle = pcall(findWindow, nil, caption)
      if foundOk then keep(handle) end
    end
  end
  return handles
end

-- Ask the attached game to come back from minimized.
--
-- Losing the foreground to a Cheat Engine window is what minimizes it, and
-- removing that window does not bring it back: the game is iconic in its own
-- bookkeeping, and asking the compositor to map and activate it was measured on
-- the device to change nothing at all. `WM_SYSCOMMAND` with `SC_RESTORE` is the
-- ordinary way to ask, and it is the one thing that was measured to work. It is
-- posted rather than sent: the receiving thread is the game's, which may be
-- loading or not pumping at all, and waiting for it would stop this bridge's
-- own timer for as long as the game took.
--
-- The ask is not free, which is what decides who may receive it. For a window
-- that is maximized rather than minimized the same request means "back to
-- windowed size", so a game that stays up when it loses the foreground would be
-- dropped out of fullscreen by the very thing meant to help it. Only a window
-- Windows itself calls minimized is sent anything, and where that cannot be
-- established nothing is sent at all: being behind something is not being
-- minimized, and no game is worth guessing about.
local function restoreGameWindows(post)
  if type(getWindowProcessID) ~= "function" or not state.attached then return false end
  local processId = state.opened_process_id
  if processId == nil or processId == 0 then return false end
  -- No proof of minimized state, no request. There is no weaker test that is
  -- safe: every game that is behind something is not in front, and only one of
  -- them is minimized. The sweep resolved it once and passes it in, so a sweep
  -- that asks twice cannot spend two of the discovery budget's attempts.
  if post == nil then return false end
  -- Any top-level window in this session will do to enter the system's chain
  -- from, and Cheat Engine's own main form is the one that is always there.
  --
  -- The foreground window is not. CE Decky keeps every Cheat Engine window
  -- hidden, so with nothing of Cheat Engine's in front there is no foreground
  -- window at all: on the device it answered zero for the whole session, the
  -- chain was never entered, no window was ever found to belong to the game,
  -- and a game that could have been asked back never was. A hidden window is
  -- still in the top-level chain, which is what makes the main form a seed and
  -- not merely a window.
  local seed = nil
  local seedOk, mainHandle = pcall(function() return MainForm ~= nil and MainForm.Handle end)
  if seedOk and type(mainHandle) == "number" and mainHandle ~= 0 then seed = mainHandle end
  if seed == nil then
    local foregroundOk, foreground = pcall(getForegroundWindow)
    if foregroundOk and type(foreground) == "number" and foreground ~= 0 then seed = foreground end
  end
  local restored = false
  local handles = gameWindowHandles(processId, seed)
  local present = {}
  for _, handle in ipairs(handles) do present[handle] = true end
  for _, handle in ipairs(handles) do
    -- Once per window, until Cheat Engine puts another window up: a window that
    -- has been asked and is still minimized did not answer, and asking a second
    -- time is not what would change that.
    -- Recorded as asked only once the post actually succeeded; a request that
    -- was never queued is not one the game has failed to answer.
    if not state.restored_handles[handle] then
      if windowIsMinimized(handle) == true and postRestore(post, handle) then
        state.restored_handles[handle] = true
        state.pending_activations[handle] = {
          waiting = RESTORE_COMPLETION_SWEEPS,
          attempts = ACTIVATION_ATTEMPTS,
        }
        restored = true
      end
    elseif state.pending_activations[handle] ~= nil then
      -- The window is back. Being back is not being active, and on this device
      -- nothing else was ever going to say so. Asked only once the window
      -- reports itself no longer minimized, so the game has handled the restore
      -- before this arrives.
      local pending = state.pending_activations[handle]
      if windowIsMinimized(handle) == false then
        if postActivate(post, handle) then
          state.pending_activations[handle] = nil
          state.activated_handles[handle] = true
          state.game_activations = state.game_activations + 1
        else
          pending.attempts = pending.attempts - 1
          if pending.attempts <= 0 then state.pending_activations[handle] = nil end
        end
      else
        -- Still minimized, or not answering the question: this is the restore
        -- that has not landed yet, and it has its own patience.
        pending.waiting = pending.waiting - 1
        if pending.waiting <= 0 then state.pending_activations[handle] = nil end
      end
    end
  end
  -- A handle the attached process no longer owns cannot be told anything, and
  -- an entry for one would keep the sweep alive for the rest of the session.
  for handle in pairs(state.pending_activations) do
    if not present[handle] then state.pending_activations[handle] = nil end
  end
  if restored then state.game_restores = state.game_restores + 1 end
  return restored
end

-- A visible window may still have lost input. Never infer its identity from
-- a caption: enumerate top-level handles, require one visible exact-PID owner,
-- and confirm foreground ownership after the request. OS refusal is final for
-- this bounded attempt; no thread-input attachment or synthetic input is used.
local function recoverGameFocus()
  if state.focus_sweeps_left <= 0 then return end
  local pid = state.opened_process_id
  if not state.attached or pid == nil or pid == 0 then
    state.focus_reason = "detached"
    state.focus_sweeps_left = 0
    return
  end
  if not focusCapability() then
    state.focus_reason = "unavailable"
    if state.focus_discovery_attempts >= RESTORE_DISCOVERY_SWEEPS then state.focus_sweeps_left = 0 end
    return
  end
  state.focus_sweeps_left = state.focus_sweeps_left - 1
  local seedOk, seed = pcall(function() return MainForm.Handle end)
  if not seedOk then seed = nil end
  local candidates = {}
  for _, handle in ipairs(gameWindowHandles(pid, seed)) do
    local visible = focusCall("visible_call", "IsWindowVisible", state.visible_address, handle)
    if visible == nil then state.focus_reason = "call-error"; return end
    if visible
        and windowIsMinimized(handle) == false then
      candidates[#candidates + 1] = handle
    end
  end
  state.focus_candidates = #candidates
  if #candidates ~= 1 then
    state.focus_reason = #candidates == 0 and "no-window" or "ambiguous"
    return
  end
  local handle = candidates[1]
  local foregroundOk, foreground = pcall(getForegroundWindow)
  if foregroundOk and foreground == handle then
    if state.focus_pending_handle == handle then
      state.focus_successes = state.focus_successes + 1
      state.focus_reason = "confirmed"
    else
      state.focus_reason = "already-foreground"
    end
    state.focus_pending_handle = nil
    state.focus_sweeps_left = 0
    return
  end
  -- Revalidate the owner immediately before crossing the Win32 boundary.
  local ownerOk, owner = pcall(getWindowProcessID, handle)
  if not ownerOk or owner ~= pid then state.focus_reason = "owner-changed"; return end
  state.focus_attempts = state.focus_attempts + 1
  local accepted = focusCall("focus_call", "SetForegroundWindow", state.focus_address, handle)
  local readOk, actual = pcall(getForegroundWindow)
  if accepted ~= nil and readOk and actual == handle then
    state.focus_successes = state.focus_successes + 1
    state.focus_pending_handle = nil
    state.focus_reason = "confirmed"
    state.focus_sweeps_left = 0
  elseif accepted == nil then
    state.focus_reason = "call-error"
  elseif not accepted then
    state.focus_reason = "refused"
  else
    state.focus_pending_handle = handle
    state.focus_reason = "unconfirmed"
  end
end

-- Hide the forms this sweep can itself see.
--
-- Cheat Engine's own `hideAllCEWindows` reaches the windows Cheat Engine made.
-- It does not reach a form a table's Lua script created, and a table's Lua
-- script now runs, so that form is a window this project has to be able to take
-- off a running game. Measured on this device: a 300x120 trainer form created by
-- `createForm` reported `Visible` true both before and after `hideAllCEWindows`,
-- stayed mapped over Half-Life 2 for the whole session, and was hidden the
-- moment `Visible` was assigned false. It is enumerated by `getForm`, which is
-- the same list the visibility check already walks, so hiding what that list
-- reports visible costs one bounded pass and uses the one documented property.
local function hideEnumeratedForms()
  if type(getFormCount) ~= "function" or type(getForm) ~= "function" then return end
  local countOk, count = pcall(getFormCount)
  if not countOk or type(count) ~= "number" or count ~= math.floor(count) or count < 0 then return end
  for index = 0, math.min(count, MAX_ENUMERATED_FORMS) - 1 do
    local formOk, form = pcall(getForm, index)
    if formOk and form ~= nil and formVisibility(form) == "visible" then
      pcall(function() form.Visible = false end)
    end
  end
end

local function enforceWindowSuppression()
  local windows = ceWindowState()
  if windows == "hidden" then
    state.window_over_game = false
    return false
  end
  if not pcall(hideAllCEWindows) then return false end
  hideEnumeratedForms()
  if windows ~= "visible" then return false end
  -- What the sweep actually achieved, not what it attempted. This counted a
  -- window seen and a hide that did not raise, which for a form Cheat Engine's
  -- own helper cannot reach was neither: on the device it climbed once a second
  -- for the whole session while the window it was counting never moved, so the
  -- one number a bug report has for this said the opposite of the truth.
  --
  -- The count and the state are separate answers to separate questions. "How
  -- often could a window not be put down" is a diagnostic that only makes sense
  -- as a total, and "is a window on the game now" is what the panel tells the
  -- user, which has to be able to go back to no.
  if ceWindowState() == "visible" then
    state.unsuppressed_sweeps = state.unsuppressed_sweeps + 1
    state.window_over_game = true
    return false
  end
  state.window_suppressions = state.window_suppressions + 1
  state.window_over_game = false
  return true
end

local function enforceWindowState()
  -- Re-asked until it answers or is refused for good, because the moment the
  -- bridge first asks is the moment Cheat Engine is least able to answer.
  local post = restoreCapability()
  local seen = enforceWindowSuppression()
  local dismissed = dismissForegroundWindow()
  if seen or dismissed then
    state.focus_sweeps_left = ACTIVATION_ATTEMPTS
    state.focus_pending_handle = nil
    -- A window just taken off the screen is another moment the game may have
    -- been left minimized behind it, so every window may be asked once more.
    state.restored_handles = {}
    state.activated_handles = {}
    state.pending_activations = {}
  end
  local ask = false
  if state.restore_sweeps_left > 0 then
    -- A sweep spent waiting for the capability is not a sweep spent asking. The
    -- game minimizes while Cheat Engine starts, which is the same moment the
    -- capability may not exist yet, so a budget that drained while the answer
    -- was still being established would run out before the first ask. It drains
    -- once there is something to spend it on, or once nothing ever will be.
    if post ~= nil then
      state.restore_sweeps_left = state.restore_sweeps_left - 1
      ask = true
    elseif state.iconic_symbol == false then
      state.restore_sweeps_left = 0
    end
  elseif seen or dismissed then
    ask = true
  end
  -- A window asked back and not yet told it is active is unfinished work with a
  -- budget of its own. It has to outlive the budget above, because the restore
  -- is posted in one of those sweeps and answered in a later one: charging both
  -- halves to one budget stranded a window restored by its last sweep.
  if next(state.pending_activations) ~= nil then ask = true end
  if ask and post ~= nil then restoreGameWindows(post) end
  recoverGameFocus()
end

-- Load the session's exact table through Cheat Engine's own API.
--
-- CE opens a table named on its command line only once its main window is
-- actually shown, and CE Decky deliberately never lets that window map: a
-- Cheat Engine window that maps takes the running game's audio and controller
-- input with it. Passing the path and hoping therefore produced a Cheat Engine
-- attached to the game with an empty address list - every record missing, no
-- cheat to switch on and nothing to pin - which is indistinguishable from a
-- table CE Decky cannot support. Loading it here depends on no window at all.
--
-- `loadTable` without `merge` replaces whatever is loaded, so repeating it is
-- idempotent and cannot stack two copies of the same table. The launch no
-- longer names the table on the command line, so nothing else can load one and
-- turn a retry into Cheat Engine's "merge tables?" prompt.
--
-- A table that carries its own Lua script is loaded through Cheat Engine's
-- stream overload, which takes the decision to run that script as a parameter.
-- The path form asks instead, and the question is one of Cheat Engine's own
-- modal forms: it stops this bootstrap where it stands, the window sweep will
-- not close it because closing an enumerated form is not the sweep's job, and
-- over a running game nobody can see it to answer it. The user has already
-- authorized this exact table SHA for execution in Review, so the answer is
-- settled before Cheat Engine is started and the same answer holds for every
-- table and every game. Measured on this device: `loadTable(stream, false,
-- true)` runs the table's Lua script and raises no dialog.
local FM_OPEN_READ_SHARED = 0x0040  -- fmOpenRead or-ed with fmShareDenyNone

local function loadTableApproved(path)
  local stream = createFileStream(path, FM_OPEN_READ_SHARED)
  if stream == nil then error("Cheat Engine returned no stream for the session table", 0) end
  -- The load reads the whole stream before it returns, so the handle is spent
  -- either way, and a load that raises is exactly the case that retries: eight
  -- attempts releasing nothing would hold eight handles on the session table
  -- for the life of this Cheat Engine. Not every Cheat Engine exposes
  -- `destroy`; one that does not collects the stream itself.
  local ok, loaded = pcall(loadTable, stream, false, true)
  pcall(function() stream.destroy() end)
  if not ok then error(loaded, 0) end
  return loaded
end

local function loadOwnedTable()
  if state.table_load_state == "loaded" then return true end
  if type(loadTable) ~= "function" then
    -- CE Decky can be pointed at a Cheat Engine the user imported themselves.
    -- One that does not expose `loadTable` cannot be driven at all, and saying
    -- so once beats eight retries ending in "attempt to call a nil value".
    state.table_load_error = "this Cheat Engine does not expose loadTable, so CE Decky cannot open the table in it"
    state.table_load_state = "failed"
    return false
  end
  state.table_load_attempts = state.table_load_attempts + 1
  local ok, loaded = false, nil
  -- What the approved route said when it refused, kept because it is the only
  -- account of why that route was not the one used. Nil means there was no
  -- approved route to try at all, which is a different fact and says so below.
  local approvedError = nil
  if type(createFileStream) == "function" then
    state.table_load_route = "approved"
    ok, loaded = pcall(loadTableApproved, state.descriptor.table_path)
    if not ok then approvedError = loaded end
  end
  if not ok then
    -- An imported Cheat Engine that has no stream overload, or refuses one,
    -- still has to be able to open a table. The path form is that fallback and
    -- it is published as such, because it is the route that can still be
    -- stopped by a question this bridge cannot answer.
    state.table_load_route = "prompted"
    if state.descriptor.table_has_lua then
      -- And for a table that carries a Lua script, that question is not a risk
      -- but a certainty: Cheat Engine raises a modal form from inside the load,
      -- the sweep hides it because hiding is what it does to Cheat Engine's own
      -- forms, and nothing here can answer it. Failing now costs a second and
      -- says why; the alternative is the launch waiting out its whole budget
      -- every time and reporting that nothing was heard.
      --
      -- Which of the two situations this is decides what is true about it, so
      -- the reason is never asserted beyond what was established: a Cheat
      -- Engine that offers no such route at all, or one that offered it and
      -- refused, in which case its own refusal is the finding and this bridge
      -- has proved nothing about the Cheat Engine itself.
      if approvedError == nil then
        state.table_load_error =
          "this Cheat Engine cannot be given the table without being asked whether to run its Lua script, "
          .. "and that question cannot be answered from Game Mode; use the Cheat Engine CE Decky installs"
      else
        state.table_load_error =
          "Cheat Engine refused to open the table through the route that authorizes its Lua script ("
          .. tostring(approvedError)
          .. "), and the only other route asks a question that cannot be answered from Game Mode"
      end
      state.table_load_state = "failed"
      return false
    end
    ok, loaded = pcall(function() return loadTable(state.descriptor.table_path) end)
  end
  local count = addressListCount()
  if ok and loaded ~= false and count ~= nil and count > 0 then
    state.table_load_state = "loaded"
    state.table_load_error = nil
    return true
  end
  if not ok then
    state.table_load_error = tostring(loaded)
  elseif loaded == false then
    state.table_load_error = "Cheat Engine refused to open the table"
  else
    state.table_load_error = "the table opened with no records"
  end
  if state.table_load_attempts >= MAX_TABLE_LOAD_ATTEMPTS then
    state.table_load_state = "failed"
  end
  return false
end

local function writeStatus()
  local listCount = addressListCount()
  local lines = {
    STATUS_HEADER,
    statusField("session_id", state.descriptor.session_id),
    statusField("app_id", state.descriptor.app_id),
    statusField("ce_sha256", state.descriptor.ce_sha256),
    statusField("table_sha256", state.descriptor.table_sha256),
    statusField("descriptor_sha256", state.descriptor_sha256),
    statusField("heartbeat_ms", getTickCount()),
    statusField("attached", state.attached and "1" or "0"),
    statusField("target_process", state.target_process),
    statusField("opened_process_id", state.opened_process_id),
  }
  if listCount ~= nil then
    lines[#lines + 1] = statusField("address_list_count", listCount)
  end
  lines[#lines + 1] = statusField("window_suppressions", tostring(state.window_suppressions))
  -- Published only once there is something to publish, so a session that never
  -- met an unhideable window writes exactly what it always wrote. Once one has
  -- been met, both go out on every status: the total is the diagnostic, and the
  -- flag is the only one of the two that can say the screen is clear again.
  if state.unsuppressed_sweeps > 0 then
    lines[#lines + 1] = statusField("unsuppressed_sweeps", tostring(state.unsuppressed_sweeps))
    lines[#lines + 1] = statusField("window_over_game", state.window_over_game and "1" or "0")
  end
  -- Published only once there is something to publish, so a bridge that never
  -- meets a dialog writes exactly what it always wrote.
  if state.iconic_symbol ~= nil then
    -- Whether this Cheat Engine can be asked if a window is minimized. Nothing
    -- restores a game without it, so a game that stays dark needs this line to
    -- be attributable to anything.
    lines[#lines + 1] = statusField(
      "minimized_query", state.iconic_symbol and tostring(state.iconic_symbol) or "unavailable"
    )
  end
  -- Published from the first sweep that reaches a conclusion, including the
  -- ones that are still waiting, because a game that never came back is the
  -- one case where "nothing was established yet" is the whole answer.
  if state.restore_reason ~= nil then
    lines[#lines + 1] = statusField("restore_capability", state.restore_reason)
  end
  -- What the refused call actually said. Bounded like every other text here,
  -- and absent once something answered.
  if state.restore_error ~= nil then
    lines[#lines + 1] = statusField("restore_error", boundedStatusText(state.restore_error))
  end
  if state.game_restores > 0 then
    lines[#lines + 1] = statusField("game_restores", tostring(state.game_restores))
  end
  for _, name in ipairs({"focus_candidates", "focus_attempts", "focus_successes"}) do
    lines[#lines + 1] = statusField(name, tostring(state[name]))
  end
  lines[#lines + 1] = statusField("focus_discovery_attempts", tostring(state.focus_discovery_attempts))
  for _, recordId in ipairs(state.startup_active_ids) do lines[#lines + 1] = "S\t" .. tostring(recordId) end
  lines[#lines + 1] = statusField("focus_reason", state.focus_reason)
  lines[#lines + 1] = statusField("focus_capability", "IsWindowVisible=" .. (state.visible_call or "unproven") .. "; SetForegroundWindow=" .. (state.focus_call or "unproven"))
  if state.focus_error then lines[#lines + 1] = statusField("focus_error", state.focus_error) end
  if state.game_activations > 0 then
    lines[#lines + 1] = statusField("game_activations", tostring(state.game_activations))
  end
  if state.dialogs_dismissed > 0 then
    lines[#lines + 1] = statusField("dialogs_dismissed", tostring(state.dialogs_dismissed))
    if state.last_dialog ~= nil then
      lines[#lines + 1] = statusField("last_dialog", boundedStatusText(state.last_dialog))
    end
  end
  -- Whether the synchronous bootstrap has finished. A heartbeat published while
  -- it is still running proves the bridge is alive and nothing else: the table
  -- may not be open, nothing is attached, and startup has not begun. It exists
  -- so that a Cheat Engine stopped by one of its own modal forms is a reported
  -- state rather than five minutes of silence.
  lines[#lines + 1] = statusField("bridge_phase", state.bootstrap_complete and "ready" or "starting")
  lines[#lines + 1] = statusField("table_load_state", state.table_load_state)
  -- Which route opened the table, and so whether the table's own Lua script was
  -- authorized without a question or Cheat Engine was still free to ask one.
  if state.table_load_route ~= nil then
    lines[#lines + 1] = statusField("table_load_route", state.table_load_route)
  end
  if state.table_load_error ~= nil then
    lines[#lines + 1] = statusField("table_load_error", boundedStatusText(state.table_load_error))
  end
  -- Startup is a lifecycle, not a side effect of connecting. A fresh heartbeat
  -- only proves the bridge is alive; auto-load also has to know whether the
  -- remembered cheats were actually applied, are still waiting for a record an
  -- enclosing script must create, or failed outright.
  local startupState = "applied"
  if state.startup_failed then
    -- "failed" alone said nothing about whether the game was actually put back,
    -- so a partially applied table looked identical to a clean abort.
    if state.rollback ~= nil then
      startupState = "failed"
    elseif state.startup_rolled_back == false then
      startupState = "failed_partial"
    else
      startupState = "failed_rolled_back"
    end
  elseif not state.startup_applied then
    startupState = #state.descriptor.startup == 0 and "applied" or "pending"
  end
  lines[#lines + 1] = statusField("startup_state", startupState)
  -- Monotonic progress, published separately because the result list above is
  -- deliberately bounded: every startup result carries generation 0, so past
  -- 128 of them each new success evicted an old one and a host counting
  -- results could no longer see that startup was still advancing.
  lines[#lines + 1] = statusField("startup_completed", tostring(state.startup_index))
  lines[#lines + 1] = statusField("startup_total", tostring(#state.descriptor.startup))
  for _, r in ipairs(state.results) do
    local rid = r.record_id == nil and "-" or tostring(r.record_id)
    local active = r.active == nil and "-" or (r.active and "1" or "0")
    local value = r.value == nil and "-" or percentEncode(tostring(r.value))
    local err = r.error == nil and "-" or percentEncode(tostring(r.error))
    local errorCode = r.error_code == nil and "-" or r.error_code
    lines[#lines + 1] = "R\t" .. tostring(r.generation) .. "\t" .. rid .. "\t" .. (r.ok and "1" or "0") .. "\t" .. active .. "\t" .. value .. "\t" .. err .. "\t" .. errorCode
  end
  for _, p in ipairs(state.processes) do
    lines[#lines + 1] = "P\t" .. tostring(p.pid) .. "\t" .. percentEncode(p.name)
  end
  local payload = table.concat(lines, "\n") .. "\n"
  if #payload > MAX_BYTES then return end
  local temp = state.descriptor.status_path .. ".tmp"
  local backup = state.descriptor.status_path .. ".old"
  local f = io.open(temp, "wb")
  if not f then return end
  -- A full disk or an I/O error must cost this heartbeat, never the last good
  -- one: unlinking the current status before the staged bytes are known
  -- complete turned a transient storage failure into durable protocol
  -- corruption that the host could only report as a parse failure.
  local written = f:write(payload)
  local flushed = written and f:flush()
  local closed = f:close()
  if not written or not flushed or not closed then
    os.remove(temp)
    return
  end
  -- Wine's rename cannot overwrite an existing name, so the current status has
  -- to move out of the way - but deleting it outright meant a rename that then
  -- failed left the session with no heartbeat at all, which the host can only
  -- read as protocol corruption. Keep the previous one until the replacement is
  -- actually in place, and put it back if it never lands.
  os.remove(backup)
  local displaced = os.rename(state.descriptor.status_path, backup)
  if os.rename(temp, state.descriptor.status_path) then
    if displaced then os.remove(backup) end
    return
  end
  os.remove(temp)
  if displaced then os.rename(backup, state.descriptor.status_path) end
end

local timer = createTimer(nil, false)
timer.Interval = POLL_MS
timer.OnTimer = function()
  local now = getTickCount()
  -- A Cheat Engine form that runs its own modal message loop keeps dispatching
  -- WM_TIMER, so this fires while the bootstrap below is still inside a table
  -- load. Everything the bootstrap is in the middle of doing is therefore left
  -- alone until it says it is finished: the table load, the attach, startup and
  -- the control file are all reached from there and none of them may be
  -- re-entered underneath themselves. What is left is exactly what a stopped
  -- bootstrap needs - the sweep that keeps Cheat Engine's windows off the game,
  -- and a heartbeat that says the bridge is alive and still starting.
  if not state.bootstrap_complete then
    state.window_ticks = state.window_ticks + 1
    if state.window_ticks >= WINDOW_ENFORCE_TICKS then
      state.window_ticks = 0
      enforceWindowState()
    end
    if now - state.last_status_tick >= HEARTBEAT_MS then
      state.last_status_tick = now
      writeStatus()
    end
    return
  end
  -- Read again while the list can still be Cheat Engine's own and was empty
  -- last time. This is the whole of the retry: after the attach below there is
  -- nothing left to read.
  if state.module_capture_ticks_left > 0 then
    state.module_capture_ticks_left = state.module_capture_ticks_left - 1
    if state.local_modules_transient then primeLocalModuleBases() end
  end
  state.window_ticks = state.window_ticks + 1
  if state.window_ticks >= WINDOW_ENFORCE_TICKS then
    state.window_ticks = 0
    enforceWindowState()
  end
  if state.table_load_state == "pending" and not waitingForLocalModules() then
    state.table_load_ticks = state.table_load_ticks + 1
    if state.table_load_ticks >= TABLE_LOAD_RETRY_TICKS then
      state.table_load_ticks = 0
      loadOwnedTable()
    end
  end
  local openedOk, openedNow = pcall(function() return getOpenedProcessID() end)
  if not openedOk then openedNow = 0 end
  if not state.attached or openedNow ~= state.opened_process_id then
    state.attached = false
    state.opened_process_id = 0
    if now - state.last_attach_tick >= ATTACH_RETRY_MS and not waitingForLocalModules() then
      state.last_attach_tick = now
      if tryAttach() then applyStartup() end
    end
  end
  -- Startup resolves one action at a time and waits for records an enclosing
  -- script has yet to create, so it must be driven by the timer. It was only
  -- ever called on the attach transition, which left a pending startup with
  -- nothing to advance it until the process was re-attached.
  if state.attached then applyStartup() end
  settlePendingCommand()
  -- A quiesce puts one record down per tick and waits for Cheat Engine between
  -- them, exactly as startup does, so the timer is what advances it.
  advanceQuiesce()
  pollControl()
  if now - state.last_status_tick >= HEARTBEAT_MS then
    state.last_status_tick = now
    writeStatus()
  end
end

-- main.lua loads the trusted bridge before CE enumerates its shipped autorun
-- directory. Publish one exact-identity heartbeat immediately so a later
-- blocking or interactive upstream autorun script cannot hide a healthy
-- bridge from the backend. Attachment and startup remain fail-closed.
-- First, before the table: `enumModules()` is Cheat Engine's own list only
-- while Cheat Engine has no process open, and loading a table is not inert.
-- Cheat Engine runs the table's own hooks, and a table's Lua script may open a
-- process by itself, which would leave this reading the game's module list at
-- the game's addresses for the rest of the session, or refusing to read one at
-- all. Nothing above this line may open a process.
primeLocalModuleBases()
-- Liveness before the table, and the sweep with it. Loading a table is the one
-- step of this bootstrap that runs code Cheat Engine and the table brought with
-- them, so it is the one that can stop for as long as it likes; before this,
-- everything a stopped bootstrap needed in order to be visible at all only
-- started once the bootstrap had already finished. The timer takes nothing over
-- from the bootstrap while `bootstrap_complete` is false, so the order of the
-- steps below is unchanged.
writeStatus()
timer.Enabled = true
-- The table is loaded only once that reading has settled, for the same reason
-- it is not loaded before the first one: Cheat Engine runs the table's own
-- hooks and its Lua, and either may open a process, which would end the only
-- chance this session has to read Cheat Engine's own module list. When the
-- first reading came back empty the timer takes both this and the attach, in
-- that order, as soon as the list appears or the wait runs out.
if waitingForLocalModules() then
  -- Ready to load on the first tick the wait clears, rather than a whole retry
  -- interval after it.
  state.table_load_ticks = TABLE_LOAD_RETRY_TICKS
else
  loadOwnedTable()
end
-- Both halves of asking a minimized game back, asked for before the first
-- heartbeat so the status carries what this Cheat Engine can do from the start,
-- and the self-test answers it without a game. This is the earliest and worst
-- moment to ask - Cheat Engine is still starting and its own symbol table may
-- have nothing in it yet - so what comes back here is a first reading, not the
-- session's answer; the window sweep keeps asking until it settles.
restoreCapability()
focusCapability()
-- Attaching is what makes Cheat Engine's module list the game's, so a list that
-- is simply not built yet is worth a moment first: it is the only reading of it
-- this session gets. Bounded, and the timer attaches either way.
if not waitingForLocalModules() then
  if tryAttach() then applyStartup() end
end
state.bootstrap_complete = true
writeStatus()
