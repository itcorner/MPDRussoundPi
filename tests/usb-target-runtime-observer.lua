local log = Log.open_topic("usb-runtime-fixture")
for _, event_type in ipairs({"session-item-added", "session-item-removed"}) do
  SimpleEventHook {
    name = "usb-runtime-fixture/" .. event_type,
    interests = {EventInterest {Constraint {"event.type", "=", event_type}}},
    execute = function(event)
      local item = event:get_subject()
      if item.properties["out.item.id"] then
        log:warning("FIXTURE SiLink " .. event_type .. " " .. item.id
          .. " out=" .. item.properties["out.item.id"]
          .. " in=" .. item.properties["in.item.id"])
      end
    end,
  }:register()
end
log:warning("FIXTURE observer loaded")
