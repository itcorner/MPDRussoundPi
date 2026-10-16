local hooks, flags = {}, {}
package.preload["linking-utils"] = function()
  local utils = {}
  function utils:get_flags(id)
    flags[id] = flags[id] or {}
    return flags[id]
  end
  function utils:unwrap_select_target_event(event)
    return event.source, event.source.items, event.subject, event.subject.properties,
      self:get_flags(event.subject.id), event.data.target
  end
  return utils
end
Conf = {get_section_as_json = function()
  return {parse = function()
    return {{node = "playback.stereo_front", vendor = "0d8c", product = "0102"}}
  end}
end}
Json = {Array = function(value) return value end}
Log = {open_topic = function() return {info = function() end} end}
Constraint = function(value) return value end
EventInterest = function(value) return value end
SimpleEventHook = function(value)
  function value:register() hooks[self.name] = self end
  return value
end
local function manager(values)
  local om = {values = values}
  function om:iterate(filter)
    local index = 0
    return function()
      repeat
        index = index + 1
        local item = self.values[index]
        if not item then return nil end
        if not filter or item.kind == filter.type then return item end
      until false
    end
  end
  return om
end
local card = {["bound-id"] = 10, properties = {
  ["device.api"] = "alsa", ["device.bus"] = "usb",
  ["device.vendor.id"] = "0d8c", ["device.product.id"] = "0102",
}}
local target = {id = 20, kind = "SiLinkable", properties = {
  ["item.node.type"] = "device", ["item.node.direction"] = "input",
}}
function target:get_associated_proxy()
  return {properties = {
    ["device.id"] = 10, ["media.class"] = "Audio/Sink",
    ["api.alsa.pcm.stream"] = "playback",
    ["device.profile.name"] = "analog-surround-51", ["audio.channels"] = 6,
  }}
end
local stream = {id = 30, kind = "SiLinkable", properties = {
  ["node.name"] = "playback.stereo_front", ["item.node.type"] = "stream",
  ["item.node.direction"] = "output", ["mpdrussoundpi.managed"] = "true",
}}
function stream:get_associated_proxy() return {properties = self.properties} end
local link = {id = 40, kind = "SiLink", removed = false, properties = {
  ["out.item.id"] = "30", ["in.item.id"] = "20",
}}
function link:remove() self.removed = true end
local source = {devices = manager({card}), items = manager({stream, target, link}), rescans = 0}
function source:call(name, kind)
  if name == "get-object-manager" then
    return kind == "device" and self.devices or self.items
  end
  assert(name == "schedule-rescan")
  self.rescans = self.rescans + 1
end
local event = {source = source, subject = stream, data = {}, stopped = false}
function event:get_source() return self.source end
function event:get_subject() return self.subject end
function event:get_data(key) return self.data[key] end
function event:set_data(key, value) self.data[key] = value end
function event:stop_processing() self.stopped = true end
require("usb-target-hook")
local function run(name) hooks["mpdrussoundpi/" .. name].execute(event) end
local assertions = 0
local function check(value)
  assertions = assertions + 1
  assert(value, "hook assertion " .. assertions .. " failed")
end
run("find-usb-target")
check(event.data.target == target and not event.stopped)
check(flags[30].has_defined_target and flags[30].can_passthrough == false)
run("guard-usb-target")
check(not event.stopped)
flags[30].peer_id = 20
flags[20] = {peer_id = 30}
source.devices.values = {card, card}
run("usb-device-change")
check(link.removed and flags[30].peer_id == nil and flags[20].peer_id == nil)
check(source.rescans == 1)
run("find-usb-target")
check(event.stopped and event.data.target == nil)
source.devices.values = {card}
event.stopped = false
run("find-usb-target")
check(event.data.target == target and not event.stopped)
event.data.target = {id = 999}
link.removed = false
run("guard-usb-target")
check(event.stopped and event.data.target == nil and link.removed)
event.stopped = false
event.data.target = nil
flags[30].peer_id = 20
run("guard-usb-target")
check(not event.stopped and event.data.target == nil)
source.devices.values = {}
run("usb-device-change")
check(flags[30].peer_id == nil)
stream.properties["node.name"] = "unrelated"
event.stopped = false
event.data.target = "unchanged"
run("find-usb-target")
run("guard-usb-target")
check(event.data.target == "unchanged" and not event.stopped)
stream.properties["node.name"] = "playback.stereo_front"
stream.properties["mpdrussoundpi.managed"] = nil
run("find-usb-target")
check(event.data.target == "unchanged" and not event.stopped)
stream.properties["mpdrussoundpi.managed"] = "true"
stream.properties["item.node.direction"] = "input"
run("find-usb-target")
check(event.data.target == "unchanged" and not event.stopped)
stream.properties["item.node.direction"] = "output"
source.devices.values = {card}
target.properties["item.node.direction"] = "output"
run("find-usb-target")
check(event.stopped and event.data.target == nil)
target.properties["item.node.direction"] = "input"
run("usb-stream-removed")
check(hooks["mpdrussoundpi/find-usb-target"].before == "linking/find-defined-target")
check(hooks["mpdrussoundpi/guard-usb-target"].after == "linking/prepare-link")
check(hooks["mpdrussoundpi/guard-usb-target"].before == "linking/link-target")
print("USB target hook adapter: " .. assertions .. " assertions passed")
