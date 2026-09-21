-- Drive the real CE Decky bridge against the stubbed Cheat Engine API.
--
-- Usage (all paths are host paths):
--   lua tests/lua/run_bridge.lua <stub.lua> <scenario.lua> <bridge.lua>
--
-- The scenario table decides the file root that Wine `Z:` maps to, the fake
-- process/MemoryRecord state, the `md5file` answers, and an ordered list of
-- steps. Each step may replace the control file, change process state, and
-- then tick the bridge timer a number of times.

local stub_path, scenario_path, bridge_path = ...
assert(stub_path and scenario_path and bridge_path, "stub, scenario and bridge paths are required")

local stub = dofile(stub_path)
local scenario = dofile(scenario_path)
local state = stub.install(scenario)

local ok, err = pcall(dofile, bridge_path)
if not ok then
  io.stderr:write("bridge load failed: " .. tostring(err) .. "\n")
end

-- The private runtime loads the bridge from `main.lua` and Cheat Engine then
-- enumerates the autorun directory the same file sits in, so it is loaded
-- twice in one Lua state. Model that exactly.
if scenario.load_bridge_twice then
  local againOk, againErr = pcall(dofile, bridge_path)
  if not againOk then
    io.stderr:write("second bridge load failed: " .. tostring(againErr) .. "\n")
  end
end

-- Cheat Engine's HandleParameters runs after autorun and explicitly shows the
-- main form for an ordinary .CT. Model that exact ordering here.
if ok then stub.show_main_form() end

for _, step in ipairs(scenario.steps or {}) do
  if step.control then stub.replace_control(step.control) end
  if step.target_pid ~= nil then stub.set("target_pid", step.target_pid) end
  if step.opened_pid ~= nil then stub.set("opened_pid", step.opened_pid) end
  if step.processes ~= nil then stub.set("processes", step.processes) end
  if step.write_failures ~= nil then stub.set("write_failures", step.write_failures) end
  if step.show_window ~= nil then
    stub.show_window(step.show_window == true and nil or step.show_window)
  end
  if step.rename_failures ~= nil then stub.set("rename_failures", step.rename_failures) end
  if step.unreadable_records ~= nil then stub.set_unreadable(step.unreadable_records) end
  if step.unhidable_script_forms ~= nil then stub.set_unhidable_script_forms(step.unhidable_script_forms) end
  if step.game_windows ~= nil then stub.set_game_windows(step.game_windows) end
  if step.system_windows ~= nil then stub.set_system_windows(step.system_windows) end
  if step.iconic_windows ~= nil then stub.set_iconic_windows(step.iconic_windows) end
  if step.foreground_window ~= nil then
    if step.foreground_window == false then
      stub.set_foreground_window(nil, nil, nil)
    else
      stub.set_foreground_window(
        step.foreground_window.handle,
        step.foreground_window.caption,
        step.foreground_window.pid
      )
    end
  end
  for _ = 1, (step.ticks or 1) do stub.tick() end
end

io.stdout:write(stub.report() .. "\n")
io.stdout:write("#LOADED#\t" .. tostring(ok) .. "\n")
io.stdout:write("#TIMER#\t" .. tostring(state.timer ~= nil and state.timer.Enabled or false) .. "\n")
