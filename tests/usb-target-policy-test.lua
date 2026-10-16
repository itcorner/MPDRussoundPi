-- Run with scripts/lib on package.path.
local policy = require("mpdrussoundpi-usb-target")
local selector = {vendor = "0d8c", product = "0102"}
local function card(id, suffix)
  return {id = id, properties = {
    ["device.api"] = "alsa", ["device.bus"] = "usb",
    ["device.vendor.id"] = "0x0D8C", ["device.product.id"] = "0102",
    ["device.name"] = "alsa_card.example" .. suffix,
  }}
end
local a = card(10, ".3")
local node = {id = 20, properties = {
  ["device.id"] = "10", ["media.class"] = "Audio/Sink",
  ["api.alsa.pcm.stream"] = "playback",
  ["device.profile.name"] = "analog-surround-51", ["audio.channels"] = "6",
}}
local assertions = 0
local function check(condition)
  assertions = assertions + 1
  assert(condition, "assertion " .. assertions .. " failed")
end
for _, suffix in ipairs({"", ".2", ".3", ".99"}) do
  a.properties["device.name"] = "alsa_card.example" .. suffix
  check(policy.select({a}, {node}, selector) == node)
end
local event = {data = {}, stopped = false}
function event:set_data(key, value) self.data[key] = value end
function event:stop_processing() self.stopped = true end
local links = {true}
local function unlink() links = {} end
check(policy.apply(event, {a}, {node}, selector, true, unlink) == "ready")
check(event.data.target == node and not event.stopped)
check(policy.apply(event, {a, card(11, ".4")}, {node}, selector, true, unlink)
  == "hardware_ambiguous")
check(#links == 0 and event.stopped and event.data.target == nil)
event.stopped = false
check(policy.apply(event, {a}, {node}, selector, true, unlink) == "ready")
check(event.data.target == node and not event.stopped)
check(policy.apply(event, {}, {}, selector, true, unlink) == "hardware_missing")
check(event.stopped and event.data.target == nil)
node.properties["audio.channels"] = 2
check(select(2, policy.select({a}, {node}, selector)) == "hardware_output_unavailable")
node.properties["audio.channels"] = 6
node.properties["device.profile.name"] = "analog-stereo"
check(select(2, policy.select({a}, {node}, selector)) == "hardware_output_unavailable")
node.properties["device.profile.name"] = "analog-surround-51"
check(select(2, policy.select({a}, {node, node}, selector)) == "hardware_output_unavailable")
event.stopped = false
event.data.target = "unrelated"
check(policy.apply(event, {}, {}, selector, false, unlink) == "unmanaged")
check(event.data.target == "unrelated" and not event.stopped)
check(not pcall(policy.select, {a}, {node}, {vendor = "invalid", product = "0102"}))
local unknown = card(nil, ".3")
local previous = node.properties["device.id"]
node.properties["device.id"] = nil
check(select(2, policy.select({unknown}, {node}, selector)) == "hardware_output_unavailable")
node.properties["device.id"] = previous
unknown = card(10, ".3")
unknown.properties["device.product.id"] = "0103"
check(select(2, policy.select({unknown}, {node}, selector)) == "hardware_missing")
print("USB target policy core: " .. assertions .. " assertions passed")
