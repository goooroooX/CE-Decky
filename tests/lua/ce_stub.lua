-- Cheat Engine API stub for off-target bridge conformance runs.
--
-- This is a test harness, never production code. It provides only the exact
-- surface `py_modules/ce_decky/ce_decky_bridge.lua` uses, plus a Wine `Z:`
-- filesystem shim so the real bridge can run on a development host.
--
-- It deliberately does NOT emulate Cheat Engine semantics beyond that surface:
-- real MemoryRecord activation, Lua/Auto Assembler execution, process
-- attachment and `md5file` remain exact-CE behaviour owned by T7/T8.

local stub = {}

local state = {
  root = nil,
  tick_count = 0,
  clock = 1000,
  target_pid = 0,
  opened_pid = 0,
  processes = {},
  records = {},
  md5_queue = {},
  md5_default = nil,
  md5_calls = 0,
  timer = nil,
  output = {},
  open_failures = {},
  hidden_windows_calls = 0,
  main_form_original_show_calls = 0,
  application_window_visible = true,
  async_records = {},
  -- A window owned by this process that the form list does not know about.
  foreground_window = nil,
  foreground_window_pid = nil,
  game_window_captions = {},
  window_handles = {},
  window_owners = {},
  window_chain = {},
  iconic_windows = {},
  foreground_window_caption = nil,
  sent_messages = {},
}

local real_io_open = io.open
local real_os_remove = os.remove
local real_os_rename = os.rename

local function translate(path)
  if type(path) ~= "string" then return path end
  if string.sub(path, 1, 3) ~= "Z:\\" then return path end
  local relative = string.gsub(string.sub(path, 4), "\\", "/")
  return state.root .. "/" .. relative
end

local function read_all(path)
  local handle = real_io_open(path, "rb")
  if not handle then return nil end
  local data = handle:read("a")
  handle:close()
  return data
end

local function write_all(path, data)
  local handle = real_io_open(path, "wb")
  if not handle then return false end
  handle:write(data)
  handle:close()
  return true
end

local function record_proxy(definition)
  local backing = {
    ID = definition.id,
    Active = definition.active and true or false,
    AsyncProcessing = false,
    Value = tostring(definition.value or "0"),
    -- What Cheat Engine's own documentation says a record carries: the script
    -- of an Auto Assembler record, and the tree it sits in. A quiesce has to
    -- put a script's children down before the script frees what it allocated,
    -- so the tree is the thing being modelled here rather than a detail.
    Script = definition.script,
    Count = definition.children and #definition.children or 0,
  }
  local function settle_active(value)
    backing.Active = value and true or false
    backing.AsyncProcessing = false
    -- The order records come down in is the whole of what a quiesce has to get
    -- right: a script frees what its children live inside, so it goes last.
    if not value then
      state.deactivations[#state.deactivations + 1] = definition.id
    end
    if definition.focus_on_settle then
      state.foreground_window = 1024
      state.foreground_window_pid = state.scenario.ce_process_id or 4242
    end
    -- A table whose own `[DISABLE]` switches another record on. Real tables do
    -- this: one hook's restore enables the record that carries the next one, so
    -- a walk that put everything down once is not finished.
    if not value and definition.activates_on_disable then
      local other = state.records[definition.activates_on_disable]
      if other then other.Active = true end
    end
    -- Switching on a script is what creates the records inside it.
    if backing.Active then
      for id, pending in pairs(state.pending_records) do
        if pending.created_by == definition.id then
          state.records[id] = record_proxy(pending)
          state.pending_records[id] = nil
        end
      end
    end
  end
  return setmetatable({}, {
    __index = function(_, key)
      if definition.read_error then error("record read failed", 0) end
      -- `Child[index]`, zero based, the way Cheat Engine indexes it.
      if key == "Child" then
        return setmetatable({}, {__index = function(_, index)
          local child = (definition.children or {})[index + 1]
          return child and state.records[child] or nil
        end})
      end
      if key == "Type" then
        return definition.script and _G.vtAutoAssembler or 2
      end
      -- Cheat Engine's address list is flat and a record knows its own parent,
      -- which is how the tree is recovered without walking it twice.
      if key == "Parent" then
        return definition.parent and state.records[definition.parent] or nil
      end
      return backing[key]
    end,
    __newindex = function(object, key, value)
      if key == "Active" then
        if definition.activation_error then error("activation refused", 0) end
        -- A record that switches on but cannot be switched back off is what a
        -- rollback has to be able to report honestly.
        if definition.restore_error and backing.Active and not value then error("restore refused", 0) end
        if definition.restore_never_settles and backing.Active and not value then return end
        -- A record that never settles is a real Cheat Engine outcome: the
        -- bridge must report it as a failure, not as a successful write.
        if definition.activation_never_settles then return end
        -- A record Cheat Engine takes its time switching on and puts down at
        -- once, which is an ordinary shape: the enable compiles and allocates,
        -- the disable writes the original bytes back.
        local slow = definition.activation_async_ticks or definition.activation_async_never_settles
        if slow and not (value == false and definition.disable_settles_at_once) then
          backing.AsyncProcessing = true
          state.async_records[definition.id] = {
            ticks = definition.activation_async_ticks,
            settle = function() settle_active(value) end,
          }
          return
        end
        settle_active(value)
        return
      end
      if key == "Value" then
        if definition.value_error then error("value refused", 0) end
        -- A table that clamps or coerces what was written is a real outcome.
        backing.Value = tostring(definition.clamp_value or value)
        return
      end
      backing[key] = value
    end,
  })
end

function stub.install(scenario)
  state.root = assert(scenario.root, "scenario.root is required")
  -- The form closures below read this table live, so a step can change what a
  -- window does part way through a run.
  state.scenario = scenario
  state.target_pid = scenario.target_pid or 0
  state.processes = scenario.processes or {}
  state.load_table_calls = 0
  state.forms = {}
  for index = 1, (scenario.extra_forms or 0) do
    if scenario.form_visibility_error == index then
      -- A form whose `Visible` getter throws: readable object, unreadable state.
      -- The setter has to refuse too, or the first `hideAllCEWindows` stores a
      -- real field on it and the getter is never consulted again.
      state.forms[index] = setmetatable({}, {
        __index = function() error("visibility unavailable", 0) end,
        __newindex = function() error("visibility unavailable", 0) end,
      })
    elseif scenario.forms_without_identity then
      -- A build whose forms answer to neither `Handle` nor `Caption`: the sweep
      -- cannot then prove a foreground window is not one of them.
      state.forms[index] = { Visible = false }
    elseif scenario.script_forms and scenario.script_forms[index] then
      -- A form a table's Lua script created. Measured on the device: Cheat
      -- Engine's own `hideAllCEWindows` leaves it visible, and assigning
      -- `Visible` hides it. `hidden_by_helper` false is that first half.
      state.forms[index] = setmetatable(
        { Handle = 2048 + index, Caption = "Script Form " .. index, hidden_by_helper = false },
        {
          __index = function(object, key)
            if key == "Visible" then return rawget(object, "_visible") ~= false end
            return nil
          end,
          __newindex = function(object, key, value)
            if key == "Visible" then
              -- A window nothing can take off the game: the assignment is
              -- refused as well, which is the outcome the sweep must report as
              -- a failure rather than as a suppression.
              if scenario.unhidable_script_forms then return end
              rawset(object, "_visible", value)
              return
            end
            rawset(object, key, value)
          end,
        }
      )
    else
      state.forms[index] = { Visible = false, Handle = 2048 + index, Caption = "CE Form " .. index }
    end
  end
  state.load_table_path = nil
  state.load_table_route = nil
  state.status_before_load = nil
  state.load_table_ignores_prompt = nil
  state.load_table_merge = nil
  state.file_stream_calls = 0
  state.file_stream_path = nil
  state.file_stream_mode = nil
  state.file_stream_destroyed = 0
  state.md5_queue = scenario.md5 or {}
  state.md5_default = scenario.md5_default
  state.open_failures = scenario.open_failures or {}
  state.write_failures = scenario.write_failures or {}
  state.rename_failures = scenario.rename_failures or {}
  state.records = {}
  state.deactivations = {}
  state.pending_records = {}
  state.async_records = {}
  state.definitions = {}
  for id, definition in pairs(scenario.records or {}) do
    definition.id = id
    -- Kept so a step can make a record unreadable part way through a run. Cheat
    -- Engine does that on its own: a record an enclosing script destroyed, or
    -- one whose address stopped resolving, throws on every read from then on,
    -- and what a stop does about that is exactly what a test has to be able to
    -- ask.
    state.definitions[id] = definition
    if definition.missing then
      -- A record Cheat Engine only creates once an enclosing script runs. The
      -- scenario names that script in `created_by`; activating it materializes
      -- this record, which is the shape nested tables actually have.
      if definition.created_by then state.pending_records[id] = definition end
    else
      state.records[id] = record_proxy(definition)
    end
  end

  io.open = function(path, mode)
    local resolved = translate(path)
    if state.open_failures[path] then return nil, "refused by scenario" end
    local handle, err = real_io_open(resolved, mode)
    if handle == nil then return nil, err end
    if state.write_failures[path] then
      -- A handle that opens and then fails mid-write is what a full disk or an
      -- I/O error actually looks like; the bridge must not publish that file.
      handle:close()
      return {
        write = function() return nil, "no space left on device" end,
        flush = function() return nil, "no space left on device" end,
        close = function() return true end,
      }
    end
    return handle
  end
  os.remove = function(path) return real_os_remove(translate(path)) end
  os.rename = function(from, to)
    -- Keyed on the source, so a scenario can refuse to promote one staged file
    -- without also refusing the restore that puts the displaced one back.
    if state.rename_failures and state.rename_failures[from] then return nil, "refused by scenario" end
    return real_os_rename(translate(from), translate(to))
  end

  _G.print = function(...)
    local parts = {}
    for index = 1, select("#", ...) do
      parts[#parts + 1] = tostring((select(index, ...)))
    end
    state.output[#state.output + 1] = table.concat(parts, "\t")
  end

  _G.md5file = function(path)
    state.md5_calls = state.md5_calls + 1
    local queued = state.md5_queue[state.md5_calls]
    if queued ~= nil then
      if queued == false then error("md5file failed", 0) end
      return queued
    end
    if state.md5_default ~= nil then return state.md5_default end
    error("md5file has no scenario value for call " .. state.md5_calls, 0)
  end

  _G.getTickCount = function()
    state.clock = state.clock + 250
    return state.clock
  end

  _G.getProcessIDFromProcessName = function(name)
    if scenario.attach_error then error("attach lookup failed", 0) end
    if name == scenario.target_process then return state.target_pid end
    return 0
  end

  _G.openProcess = function(pid)
    if scenario.open_process_error then error("openProcess failed", 0) end
    state.opened_pid = scenario.open_process_mismatch and (pid + 1) or pid
  end

  _G.getOpenedProcessID = function() return state.opened_pid end

  _G.getProcesslist = function()
    if scenario.processlist_error then error("getProcesslist failed", 0) end
    local processes = {}
    for pid, name in pairs(state.processes) do processes[pid] = name end
    if scenario.target_pid and scenario.target_pid > 0 and scenario.target_process and processes[scenario.target_pid] == nil then
      processes[scenario.target_pid] = scenario.target_process
    end
    return processes
  end

  -- The constant Cheat Engine's Lua environment defines for an Auto Assembler
  -- record's type. Its number is not in the shipped documentation, so the
  -- bridge compares against the global rather than against a literal.
  _G.vtAutoAssembler = 11

  _G.AddressList = {
    getMemoryRecordByID = function(id)
      if scenario.address_list_error then error("AddressList unavailable", 0) end
      return state.records[id]
    end,
    -- The top level of the list, in the order the scenario names it. Children
    -- are reached through their parent, exactly as Cheat Engine does it.
    -- Flat, in the order the scenario lists it, exactly as Cheat Engine's own
    -- `Count` and `Index` describe: every record in the table, nested included.
    getMemoryRecord = function(index)
      if scenario.address_list_error then error("AddressList unavailable", 0) end
      local id = (scenario.record_order or {})[index + 1]
      return id and state.records[id] or nil
    end,
    getCount = function()
      if scenario.address_list_error then error("AddressList unavailable", 0) end
      if scenario.record_order then return #scenario.record_order end
      local count = 0
      for _ in pairs(state.records) do count = count + 1 end
      return count
    end,
  }
  -- CE Decky loads the session's exact table itself, because Cheat Engine only
  -- opens one named on its command line once its main window is shown. A Cheat
  -- Engine the user imported themselves may not expose it at all.
  -- `x and nil or f` is always `f` in Lua, so this has to be an explicit branch.
  -- Cheat Engine's stream overload of `loadTable` takes the decision to run the
  -- table's own Lua script as a parameter, and the path form asks instead. A
  -- Cheat Engine the user imported themselves may expose neither the stream
  -- constructor nor that overload.
  _G.createFileStream = nil
  if not scenario.no_file_stream then _G.createFileStream = function(path, mode)
    state.file_stream_calls = state.file_stream_calls + 1
    state.file_stream_path = path
    state.file_stream_mode = mode
    if scenario.file_stream_error then error("createFileStream failed", 0) end
    if scenario.file_stream_nil then return nil end
    return {
      path = path,
      destroy = function() state.file_stream_destroyed = state.file_stream_destroyed + 1 end,
    }
  end end

  _G.loadTable = nil
  if not scenario.no_load_table then _G.loadTable = function(path, merge, ignoreLuaScriptDialog)
    state.load_table_calls = state.load_table_calls + 1
    -- Opening a table runs code Cheat Engine and the table brought with them,
    -- and one of Cheat Engine's own modal forms can stop it there for as long
    -- as it likes. Whether a heartbeat already existed at this moment is the
    -- difference between a stopped bridge that can be reported and five minutes
    -- of silence, so it is recorded rather than inferred.
    if state.status_before_load == nil then
      local existing = io.open(state.root .. "/status.txt", "rb")
      state.status_before_load = existing ~= nil
      if existing then existing:close() end
    end
    if type(path) == "table" then
      state.load_table_route = "stream"
      state.load_table_path = path.path
      state.load_table_ignores_prompt = ignoreLuaScriptDialog == true
      if scenario.no_stream_overload then
        -- A build with the stream constructor and without the overload that
        -- takes one. The stream is still the caller's to release.
        error("loadTable does not accept a stream", 0)
      end
    else
      state.load_table_route = "path"
      state.load_table_path = path
      state.load_table_ignores_prompt = false
    end
    state.load_table_merge = merge
    if scenario.load_table_error then error("loadTable failed", 0) end
    -- Cheat Engine runs the table's own hooks and its Lua script when it opens
    -- one, and either may attach to a process. That ends the only moment
    -- `enumModules()` describes Cheat Engine rather than the game.
    if scenario.load_table_opens_process then
      state.opened_pid = scenario.target_pid or 4321
    end
    if scenario.load_table_refuses then return false end
    if scenario.load_table_records then
      for id, definition in pairs(scenario.load_table_records) do
        definition.id = id
        state.records[id] = record_proxy(definition)
      end
    end
    return true
  end end

  -- `WinControl.Handle` and `Control.Caption` are both documented on the exact
  -- Cheat Engine this bridge ships against; the scenarios that withhold them
  -- model a build where reading one of them fails.
  local main_form_handle = (not scenario.main_form_without_handle) and (scenario.main_form_handle or 1024) or nil
  -- The main form is a real top-level window even while it is hidden, so it is
  -- a way into the system's window chain.
  state.seed_window = main_form_handle
  -- A main form whose handle is not there yet, the way one that has not
  -- materialized its window answers, and appears after a few ticks.
  local deferred_handle_ticks = scenario.main_form_handle_after_ticks

  _G.MainForm = setmetatable({
    SaveDialog1 = { FileName = scenario.loaded_table_path or "" },
    Caption = (not scenario.main_form_without_caption) and (scenario.main_form_caption or "Cheat Engine 7.7") or nil,
    Visible = false,
    OnShow = function()
      state.main_form_original_show_calls = state.main_form_original_show_calls + 1
    end,
  }, {
    __index = function(_, key)
      if key ~= "Handle" then return nil end
      if deferred_handle_ticks and state.tick_count < deferred_handle_ticks then return nil end
      return main_form_handle
    end,
  })

  _G.getMethodProperty = function(object, name)
    return object[name]
  end

  _G.setMethodProperty = function(object, name, callback)
    object[name] = callback
  end

  _G.hideAllCEWindows = function()
    state.hidden_windows_calls = state.hidden_windows_calls + 1
    -- A main form that refuses to go down models the one case where a
    -- foreground window this process owns might be the main form itself.
    if not scenario.unhidable_main_form then MainForm.Visible = false end
    for _, form in ipairs(state.forms) do
      -- Cheat Engine's own helper reaches the windows Cheat Engine made. It
      -- does not reach a form a table's Lua script created, which is what was
      -- measured on the device and is why the sweep hides what it enumerates.
      if rawget(form, "hidden_by_helper") ~= false then
        pcall(function() form.Visible = false end)
      end
    end
  end

  -- CE exposes its application's own forms, which is how a window it maps after
  -- the first show can be found at all.
  _G.getFormCount = function()
    if scenario.form_enumeration_error then error("form enumeration failed", 0) end
    return #state.forms
  end

  _G.getForm = function(index)
    if scenario.form_enumeration_error then error("form enumeration failed", 0) end
    return state.forms[index + 1]
  end

  _G.closeCE = function()
    state.closed = true
  end

  -- A window that is not one of the application's forms: what Cheat Engine puts
  -- up for a message dialog, and what the form enumeration above cannot see.
  _G.getCheatEngineProcessID = function()
    return scenario.ce_process_id or 4242
  end

  _G.getForegroundWindow = function()
    return state.foreground_window or 0
  end

  -- Cheat Engine calls a function in its own process by symbol name; its own
  -- shipped autorun scripts use this for `DrawIconEx`, `ExtractIconA` and
  -- `ntdll.RtlGetVersion`. The bridge uses it for `IsIconic`, which is the only
  -- thing that answers whether a window is minimized.
  -- `user32.dll` as Cheat Engine's own process has it loaded, and the two
  -- exports in it the bridge needs. The bridge can reach these two ways: the
  -- symbol table, and reading the module's own image. On the device only the
  -- second works while a game is attached, so the stub provides a real image to
  -- parse rather than a table of answers.
  local USER32_BASE = 0xB0000000
  local USER32_SIZE = 0x10000
  local POST_MESSAGE_ADDRESS = USER32_BASE + 0x3000
  local ICONIC_ADDRESS = USER32_BASE + 0x2000

  -- One minimal 64-bit image, laid out where the bridge will look for it.
  local image = {}
  local function put8(offset, value) image[offset] = value % 0x100 end
  local function put16(offset, value)
    put8(offset, value % 0x100)
    put8(offset + 1, math.floor(value / 0x100) % 0x100)
  end
  local function put32(offset, value)
    for index = 0, 3 do
      put8(offset + index, math.floor(value / (0x100 ^ index)) % 0x100)
    end
  end
  local function putString(offset, text)
    for index = 1, #text do put8(offset + index - 1, string.byte(text, index)) end
    put8(offset + #text, 0)
  end
  local EXPORT_RVA = scenario.image_export_rva or 0x1000
  local EXPORT_SIZE = scenario.image_export_size or 0x200
  local PE_OFFSET = 0x80
  local OPTIONAL = PE_OFFSET + 0x18
  put16(0x00, 0x5A4D)
  put32(0x3C, PE_OFFSET)
  put32(PE_OFFSET, 0x00004550)
  put16(OPTIONAL, 0x20B)
  -- The file header's optional-header size and the optional header's own count
  -- of data directories are what bound reading the export directory out of it,
  -- so a real image carries both and this one has to as well.
  put16(PE_OFFSET + 0x14, scenario.image_optional_header_size or 0xF0)
  put32(OPTIONAL + 0x6C, scenario.image_directory_count or 16)
  put32(OPTIONAL + 0x38, USER32_SIZE)
  put32(OPTIONAL + 0x70, EXPORT_RVA)
  put32(OPTIONAL + 0x74, EXPORT_SIZE)
  -- Two exports, in the sorted order an export directory keeps them in.
  local NAMES_RVA = scenario.image_names_rva or 0x1100
  local ORDINALS_RVA = scenario.image_ordinals_rva or 0x1200
  local FUNCTIONS_RVA = scenario.image_functions_rva or 0x1300
  put32(EXPORT_RVA + 0x14, 2)
  put32(EXPORT_RVA + 0x18, 2)
  put32(EXPORT_RVA + 0x1C, FUNCTIONS_RVA)
  put32(EXPORT_RVA + 0x20, NAMES_RVA)
  put32(EXPORT_RVA + 0x24, ORDINALS_RVA)
  put32(NAMES_RVA, 0x1400)
  put32(NAMES_RVA + 4, 0x1420)
  putString(0x1400, "IsIconic")
  putString(0x1420, "PostMessageW")
  put16(ORDINALS_RVA, 0)
  put16(ORDINALS_RVA + 2, 1)
  put32(FUNCTIONS_RVA, ICONIC_ADDRESS - USER32_BASE)
  put32(FUNCTIONS_RVA + 4, POST_MESSAGE_ADDRESS - USER32_BASE)

  if scenario.focus_available then
    -- Sorted exports, including the route needed when CE's symbol table is empty.
    local exports = {{"IsIconic", 0x2000}, {"IsWindowVisible", 0x4000}, {"PostMessageW", 0x3000}, {"SetForegroundWindow", 0x5000}}
    put32(EXPORT_RVA + 0x14, #exports)
    put32(EXPORT_RVA + 0x18, #exports)
    for index, entry in ipairs(exports) do
      put32(NAMES_RVA + 4 * (index - 1), 0x1400 + 0x40 * (index - 1))
      putString(0x1400 + 0x40 * (index - 1), entry[1])
      put16(ORDINALS_RVA + 2 * (index - 1), index - 1)
      put32(FUNCTIONS_RVA + 4 * (index - 1), entry[2])
    end
  end

  local function imageByte(address)
    if type(address) ~= "number" then return nil end
    local offset = address - USER32_BASE
    if offset < 0 or offset >= USER32_SIZE then return nil end
    if scenario.focus_exports_after_tick and state.tick_count < scenario.focus_exports_after_tick
        and ((offset >= 0x1440 and offset < 0x1480) or (offset >= 0x14c0 and offset < 0x1500)) then
      return nil
    end
    return image[offset] or 0
  end

  -- Cheat Engine has two lookups into its own symbol table and they are not the
  -- same one: `getAddressSafe(name, true)` and `getAddress(name, true)` reach
  -- different resolvers, and on the target the first answers nothing for
  -- `user32` while the second is what Cheat Engine's own scripts rely on. The
  -- stub models both so a bridge that asked the wrong one would fail here.
  local function lookup(symbol, isLocal)
    if scenario.get_address_error then error("symbol lookup failed", 0) end
    if not isLocal then return nil end
    -- The device's own condition once a game is attached: the symbol table
    -- answers nothing at all, for anything, and never recovers.
    if scenario.symbol_table_blind then return 0 end
    -- Cheat Engine builds its own symbol table in a thread while it starts, so
    -- for the first moments of a session it answers nothing at all. That is the
    -- device's exact condition, and the bridge asks its first question inside
    -- that window.
    if scenario.symbols_blind_until_tick
        and state.tick_count < scenario.symbols_blind_until_tick then
      return nil
    end
    if scenario.symbols_need_reload and not state.symbols_reloaded then return nil end
    if scenario.no_post_message and (symbol == "user32.PostMessageW" or symbol == "PostMessageW") then
      return nil
    end
    if scenario.focus_exports_after_tick and state.tick_count < scenario.focus_exports_after_tick
        and (symbol == "SetForegroundWindow" or symbol == "IsWindowVisible") then return nil end
    if scenario.focus_available and symbol == "SetForegroundWindow" then return 0x76540001 end
    if scenario.focus_available and symbol == "IsWindowVisible" then return 0x76540002 end
    if symbol == "user32.PostMessageW" then
      return (not scenario.unqualified_post_only) and POST_MESSAGE_ADDRESS or nil
    end
    if symbol == "PostMessageW" then return POST_MESSAGE_ADDRESS end
    if symbol == "user32.IsIconic" then
      return (not scenario.unqualified_iconic_only) and ICONIC_ADDRESS or nil
    end
    if symbol == "IsIconic" then return (not scenario.no_iconic_symbol) and ICONIC_ADDRESS or nil end
    return nil
  end

  _G.getAddress = nil
  if not scenario.no_get_address then _G.getAddress = lookup end
  -- The target's own condition: this one resolves nothing in `user32`. Anything
  -- that reaches for it instead of `getAddress` finds an empty table.
  _G.getAddressSafe = nil
  if not scenario.no_get_address_safe then
    _G.getAddressSafe = scenario.safe_lookup_blind and function() return nil end or lookup
  end

  -- Cheat Engine's own module list, and reads of its own memory. On the device
  -- `enumModules()` answers for the process Cheat Engine has open, so it is
  -- Cheat Engine's own list only while nothing is attached: a bridge that reads
  -- it after attaching would get the game's modules at the game's addresses.
  _G.enumModules = nil
  if not scenario.no_self_module_list then
    _G.enumModules = function(processId)
      if processId ~= nil then return {} end
      if (state.opened_pid or 0) ~= 0 then
        return {{Name = "game.dll", Address = 0x10000000}}
      end
      if scenario.self_module_list_empty_until_tick
          and state.tick_count < scenario.self_module_list_empty_until_tick then
        return {}
      end
      local modules = {
        {Name = "cheatengine-x86_64.exe", Address = 0x400000},
        {Name = "user32.dll", Address = USER32_BASE},
      }
      if scenario.self_module_list_oversized then
        -- Longer than the bridge walks, and with the module it wants past the
        -- end of that walk: a list it cannot read whole says nothing about
        -- what is in it.
        local padded = {}
        for index = 1, 2000 do padded[index] = {Name = "pad" .. index .. ".dll", Address = 0x1000 * index} end
        for _, entry in ipairs(modules) do padded[#padded + 1] = entry end
        return padded
      end
      return modules
    end
  end

  _G.readIntegerLocal = function(address)
    local value = 0
    for index = 0, 3 do
      local byte = imageByte(address + index)
      if byte == nil then return nil end
      value = value + byte * (0x100 ^ index)
    end
    if value >= 0x80000000 then value = value - 0x100000000 end
    return value
  end

  _G.readSmallIntegerLocal = function(address)
    local low, high = imageByte(address), imageByte(address + 1)
    if low == nil or high == nil then return nil end
    return low + high * 0x100
  end

  _G.readStringLocal = function(address, maxLength)
    local out = {}
    for index = 0, (maxLength or 32) - 1 do
      local byte = imageByte(address + index)
      if byte == nil then return nil end
      if byte == 0 then break end
      out[#out + 1] = string.char(byte)
    end
    return table.concat(out)
  end

  -- Reloading the self symbol handler, which is how a table that had nothing
  -- to answer with is corrected.
  _G.reinitializeSelfSymbolhandler = nil
  if not scenario.no_reinitialize_symbols then
    _G.reinitializeSelfSymbolhandler = function() state.symbols_reloaded = true end
  end

  _G.executeCodeLocalEx = nil
  _G.executeCodeLocal = nil
  local function callLocal(symbol, parameter, message, wparam, lparam)
    if scenario.execute_code_local_error then error("local call failed", 0) end
    if (symbol == 0x76540002 or symbol == USER32_BASE + 0x4000) then return scenario.focus_hidden and 0 or 1 end
    if (symbol == 0x76540001 or symbol == USER32_BASE + 0x5000) then
      if scenario.focus_refused then return 0 end
      if scenario.focus_error then error("focus failed", 0) end
      if scenario.focus_delay_ticks then
        state.pending_focus = state.pending_focus or {handle = parameter, tick = state.tick_count + scenario.focus_delay_ticks}
      else
        state.foreground_window = parameter
      end
      return 1
    end
    if symbol == POST_MESSAGE_ADDRESS then
      -- `PostMessageW` queues the message and returns without waiting for the
      -- receiving thread, which is the whole reason it is used here.
      if scenario.post_message_refused then return 0 end
      state.sent_messages[#state.sent_messages + 1] = {
        handle = parameter, message = message, wparam = wparam, lparam = lparam, posted = true,
      }
      return 1
    end
    if symbol ~= ICONIC_ADDRESS then
      error("unknown symbol " .. tostring(symbol), 0)
    end
    -- A call that answers something other than what the caller thinks it does:
    -- the bridge proves the mechanism against a window it knows the answer for.
    if scenario.lying_iconic then return 1 end
    -- The upper half of the return register is not part of a 32-bit BOOL.
    local iconic = (state.iconic_windows or {})[parameter] and 1 or 0
    if scenario.dirty_return_register then return 0x1234500000000 + iconic end
    return iconic
  end

  -- Cheat Engine offers two forms of the local call and the bridge tries both:
  -- `executeCodeLocalEx` takes any number of parameters, `executeCodeLocal`
  -- takes one, and Cheat Engine's own `ceshare_publish.lua` uses the latter for
  -- exactly this shape of call. `no_execute_code_local` removes both, which is
  -- a Cheat Engine that cannot be asked at all; `no_execute_code_local_ex`
  -- removes only the many-parameter form.
  if not scenario.no_execute_code_local then
    if not scenario.no_execute_code_local_ex then
      _G.executeCodeLocalEx = function(symbol, ...)
        if symbol == 0x76540001 and scenario.focus_ex_refused then return 0 end
        if symbol == 0x76540001 or symbol == 0x76540002 then
          if scenario.focus_ex_error then error("Ex focus invocation failed", 0) end
          if scenario.focus_ex_bad_type then return "invalid BOOL" end
        end
        return callLocal(symbol, ...)
      end
    end
    _G.executeCodeLocal = function(symbol, parameter) return callLocal(symbol, parameter) end
  end

  -- Cheat Engine's wrapper around Win32 `GetWindow`. The chain is every
  -- top-level window in the system, in order, which is what lets a window be
  -- found by its owner rather than by its caption.
  _G.getWindow = nil
  if not scenario.no_get_window then _G.getWindow = function(handle, command)
    if scenario.get_window_error then error("getWindow failed", 0) end
    local chain = state.window_chain or {}
    if command == 0 then
      -- Win32 walks the chain a real window is in, so a handle that is not a
      -- window is not a way into it. Cheat Engine's own main form is, even
      -- while it is hidden, which is the whole point of using it as the seed.
      local known = handle == state.seed_window
      for _, entry in ipairs(chain) do
        if entry == handle then known = true break end
      end
      if not known then return 0 end
      return chain[1] or 0
    end
    if command ~= 2 then return 0 end
    for index, entry in ipairs(chain) do
      if entry == handle then return chain[index + 1] or 0 end
    end
    return 0
  end end

  _G.getWindowProcessID = function(handle)
    if handle == 0 then return 0 end
    local owner = (state.window_owners or {})[handle]
    if owner ~= nil then return owner end
    return state.foreground_window_pid or (scenario.ce_process_id or 4242)
  end

  _G.getWindowCaption = function(handle)
    if handle == 0 then return "" end
    return state.foreground_window_caption or ""
  end

  -- What the shipped Cheat Engine actually answers with, measured on the
  -- device: a table keyed by process ID whose value is a list of captions. No
  -- window handle appears in it anywhere, which is why the handle has to be
  -- resolved separately. `window_list_shape = "pairs"` makes each entry the
  -- {id, caption} pair the manual describes, so a build that answers the way
  -- the manual says is read too.
  _G.getWindowlist = function()
    if scenario.window_list_error then error("window list unavailable", 0) end
    local windows = {}
    for index, caption in ipairs(state.game_window_captions or {}) do
      if scenario.window_list_shape == "pairs" then
        windows[#windows + 1] = { index, caption }
      else
        windows[#windows + 1] = caption
      end
    end
    return { [state.target_pid] = windows, [scenario.ce_process_id or 4242] = { "Cheat Engine" } }
  end

  -- A caption is not an identity: on the device the game's own "Default IME"
  -- caption resolved to a window owned by an entirely different process.
  _G.findWindow = function(_class, caption)
    return (state.window_handles or {})[caption] or 0
  end

  _G.sendMessage = function(handle, message, wparam, lparam)
    state.sent_messages[#state.sent_messages + 1] = {
      handle = handle, message = message, wparam = wparam, lparam = lparam,
    }
    if state.foreground_window == handle and message == 0x0010 and not scenario.ignore_window_close then
      state.foreground_window = nil
      state.foreground_window_caption = nil
    end
    return 0
  end

  local application_main_form_on_taskbar = false
  local application = setmetatable({}, {
    __index = function(_, key)
      if key == "MainFormOnTaskBar" then
        -- CE's LuaApplication wrapper deliberately exposes the inverse of the
        -- underlying LCL property.
        return not application_main_form_on_taskbar
      end
      return nil
    end,
    __newindex = function(_, key, value)
      if key == "MainFormOnTaskBar" then
        application_main_form_on_taskbar = not value
        state.application_window_visible = not application_main_form_on_taskbar
        return
      end
      rawset(object, key, value)
    end,
  })
  _G.getApplication = function() return application end

  _G.createTimer = function()
    state.timer = { Interval = 0, Enabled = false, OnTimer = nil }
    return state.timer
  end

  return state
end

-- Make these records throw on every read from now on.
function stub.set_unreadable(ids)
  for _, id in ipairs(ids or {}) do
    local definition = (state.definitions or {})[id]
    if definition then definition.read_error = true end
  end
end

function stub.tick()
  state.tick_count = state.tick_count + 1
  if state.pending_focus and state.tick_count >= state.pending_focus.tick then
    state.foreground_window = state.pending_focus.handle
    state.pending_focus = nil
  end
  for id, pending in pairs(state.async_records) do
    if pending.ticks then
      pending.ticks = pending.ticks - 1
      if pending.ticks <= 0 then
        pending.settle()
        state.async_records[id] = nil
      end
    end
  end
  if state.timer and state.timer.Enabled and state.timer.OnTimer then
    local ok, err = pcall(state.timer.OnTimer)
    if not ok then
      state.output[#state.output + 1] = "TIMER-ERROR\t" .. tostring(err)
    end
  end
end

function stub.show_main_form()
  MainForm.Visible = true
  if MainForm.OnShow then MainForm.OnShow(MainForm) end
end

-- A window Cheat Engine maps again long after its first show: a form it creates
-- on demand, or a main form something shows a second time.
function stub.show_window(index)
  if index == nil then
    MainForm.Visible = true
  else
    state.forms[index].Visible = true
  end
end

-- Put a window Cheat Engine's form enumeration cannot see into the foreground,
-- the way a message dialog arrives. `pid` overrides its owner so a window that
-- belongs to the game rather than to Cheat Engine can be modelled too.
-- `windows` is a list of {caption, handle, owner}: the caption the window list
-- reports, the handle `findWindow` resolves it to, and the process that handle
-- really belongs to.
function stub.set_game_windows(windows)
  state.game_window_captions = {}
  state.window_handles = {}
  state.window_owners = {}
  state.window_chain = {}
  for _, window in ipairs(windows) do
    state.game_window_captions[#state.game_window_captions + 1] = window[1]
    state.window_handles[window[1]] = window[2]
    state.window_owners[window[2]] = window[3]
    state.window_chain[#state.window_chain + 1] = window[2]
  end
end

-- Top-level windows that no caption resolves to: what the enumeration finds and
-- a caption lookup cannot. Each entry is {handle, owner}.
function stub.set_system_windows(windows)
  for _, window in ipairs(windows) do
    state.window_owners[window[1]] = window[2]
    state.window_chain[#state.window_chain + 1] = window[1]
  end
end

-- The windows Windows itself would call minimized.
function stub.set_iconic_windows(handles)
  state.iconic_windows = {}
  for _, handle in ipairs(handles) do state.iconic_windows[handle] = true end
end

function stub.set_foreground_window(handle, caption, pid)
  state.foreground_window = handle
  state.foreground_window_caption = caption
  state.foreground_window_pid = pid
  if handle ~= nil then
    if pid ~= nil then state.window_owners[handle] = pid end
    local present = false
    for _, entry in ipairs(state.window_chain or {}) do
      if entry == handle then present = true break end
    end
    if not present then
      state.window_chain = state.window_chain or {}
      state.window_chain[#state.window_chain + 1] = handle
    end
  end
end

function stub.replace_control(name)
  local data = read_all(state.root .. "/" .. name)
  if not data then
    state.output[#state.output + 1] = "SCENARIO-ERROR\tmissing control fixture " .. name
    return
  end
  write_all(state.root .. "/control.txt", data)
end

function stub.set(key, value)
  state[key] = value
end

-- A window that stops refusing to be hidden: the table closed it itself, or the
-- assignment finally lands. The sweep has to be able to say that the screen came
-- back, and not only that it was once covered.
function stub.set_unhidable_script_forms(value)
  state.scenario.unhidable_script_forms = value
end

function stub.report()
  print = function() end
  local lines = {
    "#TICKS#\t" .. tostring(state.tick_count),
    "#MD5CALLS#\t" .. tostring(state.md5_calls),
    "#MAINFORM#\t" .. tostring(state.main_form_original_show_calls) .. "\t" ..
      tostring(state.hidden_windows_calls) .. "\t" .. tostring(MainForm.Visible),
    "#APPLICATION#\t" .. tostring(state.application_window_visible),
    "#SENTMESSAGES#\t" .. tostring(#state.sent_messages),
    -- How the session's table was opened: the call count, whether the stream
    -- overload or the path form was used, whether the table's own Lua script
    -- was authorized rather than asked about, and whether the stream handle was
    -- released afterwards.
    "#LOADTABLE#\t" .. tostring(state.load_table_calls) .. "\t" ..
      tostring(state.load_table_route) .. "\t" ..
      tostring(state.load_table_ignores_prompt) .. "\t" ..
      tostring(state.file_stream_calls) .. "\t" ..
      tostring(state.file_stream_mode) .. "\t" ..
      tostring(state.file_stream_destroyed),
    -- Whether a heartbeat was already published when the table was opened.
    "#LOADORDER#\t" .. tostring(state.status_before_load),
  }
  for _, id in ipairs(state.deactivations or {}) do
    lines[#lines + 1] = "deactivated " .. tostring(id)
  end
  -- What each record was left holding. Switching a frozen record off releases
  -- the freeze and leaves the value it was frozen at, so what the game keeps is
  -- the write that came after it rather than the release, and a test about a
  -- cheat really being off has to be able to read that value.
  for _, id in ipairs((state.scenario or {}).record_order or {}) do
    local record = state.records[id]
    if record then
      local ok, value = pcall(function() return record.Value end)
      if ok then
        lines[#lines + 1] = "value " .. tostring(id) .. " " .. tostring(value)
      else
        lines[#lines + 1] = "value " .. tostring(id) .. " unreadable"
      end
      -- Explicit branches: `ok and value or fallback` reports a readable
      -- `false` as the fallback, which is this file's own trap written down.
      local activeOk, active = pcall(function() return record.Active end)
      if activeOk then
        lines[#lines + 1] = "active " .. tostring(id) .. " " .. tostring(active)
      else
        lines[#lines + 1] = "active " .. tostring(id) .. " unreadable"
      end
    end
  end
  for _, message in ipairs(state.sent_messages) do
    lines[#lines + 1] = "#MESSAGE#\t" .. tostring(message.handle) .. "\t" ..
      tostring(message.message) .. "\t" .. tostring(message.wparam) .. "\t" ..
      tostring(message.posted == true)
  end
  for _, line in ipairs(state.output) do
    lines[#lines + 1] = "#OUT#\t" .. line
  end
  return table.concat(lines, "\n")
end

return stub
