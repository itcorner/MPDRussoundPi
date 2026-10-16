-- Isolated policy core. Not installed or registered in the live audio session.
local policy = {}

local function usb_id(value)
  if type(value) == "number" then
    if value == math.floor(value) and value >= 0 and value <= 65535 then
      return value
    end
    return nil
  end
  if type(value) ~= "string" then return nil end
  local digits = value:gsub("^0x", "")
  if #digits < 1 or #digits > 4 or not digits:match("^[%da-fA-F]+$") then
    return nil
  end
  return tonumber(digits, 16)
end

function policy.select(cards, nodes, selector)
  local vendor, product = usb_id(selector.vendor), usb_id(selector.product)
  assert(vendor and product, "invalid hardware selector")
  local matches = {}
  for _, card in ipairs(cards) do
    local p = card.properties
    if p["device.api"] == "alsa" and p["device.bus"] == "usb"
        and usb_id(p["device.vendor.id"]) == vendor
        and usb_id(p["device.product.id"]) == product then
      matches[#matches + 1] = card
    end
  end
  if #matches == 0 then return nil, "hardware_missing" end
  if #matches ~= 1 then return nil, "hardware_ambiguous" end
  local card_id = tonumber(matches[1].id)
  if not card_id or card_id < 0 or card_id ~= math.floor(card_id) then
    return nil, "hardware_output_unavailable"
  end
  local targets = {}
  for _, node in ipairs(nodes) do
    local p = node.properties
    if tonumber(p["device.id"]) == card_id
        and p["media.class"] == "Audio/Sink"
        and p["api.alsa.pcm.stream"] == "playback"
        and p["device.profile.name"] == "analog-surround-51"
        and tonumber(p["audio.channels"]) == 6 then
      targets[#targets + 1] = node
    end
  end
  if #targets ~= 1 then return nil, "hardware_output_unavailable" end
  return targets[1], "ready"
end

-- The host adapter must supply complete current inventories on every rescan.
function policy.apply(event, cards, nodes, selector, managed, unlink)
  if not managed then return "unmanaged" end
  local target, reason = policy.select(cards, nodes, selector)
  if not target then
    unlink(event)
    event:set_data("target", nil)
    event:stop_processing()
  else
    event:set_data("target", target)
  end
  return reason
end

return policy
