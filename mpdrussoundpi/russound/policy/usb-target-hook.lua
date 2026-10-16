-- Prototype: requires an explicit component and mpdrussoundpi.usb-targets config.
local policy = require("mpdrussoundpi-usb-target")
local lutils = require("linking-utils")
local log = Log.open_topic("mpdrussoundpi-usb-target")
local configured = Conf.get_section_as_json("mpdrussoundpi.usb-targets", Json.Array {})
local entries = configured:parse()
local selectors = {}
local states = {}
for _, entry in ipairs(entries) do
  assert(type(entry.node) == "string" and entry.node:match("^playback%.[%w_-]+$"),
    "invalid managed playback node")
  assert(not selectors[entry.node], "duplicate managed playback node")
  selectors[entry.node] = {vendor = entry.vendor, product = entry.product}
  policy.select({}, {}, selectors[entry.node])
end

local function selector_for(si)
  local node = si:get_associated_proxy("node")
  local marker = node and node.properties["mpdrussoundpi.managed"]
  if (marker == true or marker == "true")
      and si.properties["item.node.direction"] == "output" then
    return selectors[node.properties["node.name"]]
  end
end

local function inventories(source, om)
  local cards, nodes = {}, {}
  local devices = source:call("get-object-manager", "device")
  for device in devices:iterate() do
    cards[#cards + 1] = {id = device["bound-id"], properties = device.properties}
  end
  for item in om:iterate {type = "SiLinkable"} do
    if item.properties["item.node.type"] == "device"
        and item.properties["item.node.direction"] == "input" then
      local node = item:get_associated_proxy("node")
      if node then
        nodes[#nodes + 1] = {id = item.id, properties = node.properties, item = item}
      end
    end
  end
  return cards, nodes
end

local function unlink(si, om)
  for link in om:iterate {type = "SiLink"} do
    local output = tonumber(link.properties["out.item.id"])
    local input = tonumber(link.properties["in.item.id"])
    if output == si.id or input == si.id then
      local own = lutils:get_flags(si.id)
      local other_id = output == si.id and input or output
      local other = lutils:get_flags(other_id)
      if other.peer_id == si.id then other.peer_id = nil end
      own.peer_id = nil
      link:remove()
    end
  end
  local flags = lutils:get_flags(si.id)
  flags.peer_id = nil
  flags.failed_peer_id = nil
  flags.failed_count = nil
end

local function choose(event)
  local source, om, si, props, flags = lutils:unwrap_select_target_event(event)
  local selector = selector_for(si)
  if not selector then return end
  local cards, nodes = inventories(source, om)
  local target, reason = policy.select(cards, nodes, selector)
  if states[si.id] ~= reason then
    states[si.id] = reason
    log:info(si, "managed USB target: " .. reason)
  end
  if not target then
    unlink(si, om)
    event:set_data("target", nil)
    event:stop_processing()
    return
  end
  flags.can_passthrough = false
  flags.has_defined_target = true
  flags.has_node_defined_target = true
  event:set_data("target", target.item)
end

SimpleEventHook {
  name = "mpdrussoundpi/find-usb-target",
  before = "linking/find-defined-target",
  interests = {EventInterest {Constraint {"event.type", "=", "select-target"}}},
  execute = choose,
}:register()

-- Recheck after all stock selection/prepare hooks, including restored user targets.
SimpleEventHook {
  name = "mpdrussoundpi/guard-usb-target",
  after = "linking/prepare-link",
  before = "linking/link-target",
  interests = {EventInterest {Constraint {"event.type", "=", "select-target"}}},
  execute = function(event)
    local source, om, si, props, flags = lutils:unwrap_select_target_event(event)
    local selector = selector_for(si)
    if not selector then return end
    local cards, nodes = inventories(source, om)
    local expected = policy.select(cards, nodes, selector)
    local current = event:get_data("target")
    if expected and (current == expected.item
        or (current == nil and flags.peer_id == expected.item.id)) then
      return
    end
    if expected and current then
      log:info(si, "managed USB target: rejected stock target override")
    end
    unlink(si, om)
    event:set_data("target", nil)
    event:stop_processing()
  end,
}:register()

log:info("managed USB target hooks loaded")

SimpleEventHook {
  name = "mpdrussoundpi/usb-device-change",
  interests = {
    EventInterest {Constraint {"event.type", "=", "device-added"}},
    EventInterest {Constraint {"event.type", "=", "device-removed"}},
  },
  execute = function(event)
    local source = event:get_source()
    local om = source:call("get-object-manager", "session-item")
    local cards, nodes = inventories(source, om)
    for si in om:iterate {type = "SiLinkable"} do
      local selector = selector_for(si)
      if selector and not policy.select(cards, nodes, selector) then
        unlink(si, om)
      end
    end
    source:call("schedule-rescan", "linking")
  end,
}:register()

SimpleEventHook {
  name = "mpdrussoundpi/usb-stream-removed",
  interests = {
    EventInterest {Constraint {"event.type", "=", "session-item-removed"}},
  },
  execute = function(event)
    states[event:get_subject().id] = nil
  end,
}:register()
