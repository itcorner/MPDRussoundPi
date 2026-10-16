import logging
from collections.abc import Mapping
from typing import cast
from lib.sink import Sink, config2sink
from lib.audioidentity import HardwareSelector, hardware_configuration, object_mapping

logging.basicConfig(level=logging.DEBUG)

class AudioDevice:
    def __init__(self, name: str, card_profile: str, sinks: list[Sink],
                 hardware_selector: HardwareSelector | None = None):
        self.name = name
        self.card_profile = card_profile
        self.sinks = sinks
        self.hardware_selector = hardware_selector

    def get_pulseaudio_card_config(self) -> str:
        """Generate PulseAudio card configuration for this audio device."""
        if self.hardware_selector is not None:
            raise ValueError("USB hardware selectors require PipeWire/WirePlumber")
        card_name = "alsa_card." + self.name
        card_mode = "output:" + self.card_profile
        master_sink = "alsa_output." + self.name + "." + self.card_profile
        config = f"# Config for audio device: {self.name}\n"
        config += f"# Set {card_name} to {card_mode} mode\n"
        config += f"set-card-profile {card_name} {card_mode}\n\n"
        
        for sink in self.sinks:
            config += sink.get_pulseaudio_config(master_sink)
        return config
    
    def get_wireplumber_card_config(self) -> str:
        """Generate WirePlumber card configuration for this audio device."""
        if self.hardware_selector is not None:
            # Profile selection must be uniqueness-gated in recovery, not a model-wide rule.
            return ""
        card_name = "alsa_card." + self.name
        card_mode = "output:" + self.card_profile
        config = f"# Config for audio device: {self.name}\n"
        config += f"# Set {card_name} to {card_mode} mode\n"
        config += "    {\n"
        config += "        matches = [\n"
        config += f"            {{ device.name = \"{card_name}\" }}\n"
        config += "        ]\n"
        config += "        actions = {\n"
        config += f"            update-props = {{ device.profile = \"{card_mode}\" }}\n"
        config += "        }\n"
        config += "    },\n"
        return config

    def get_pipewire_sink_config(self) -> str:
        """Generate PipeWire configuration for this audio device."""
        master_sink = "alsa_output." + self.name + "." + self.card_profile
        config = f"    # PipeWire config for audio device: {self.name}\n"
        for sink in self.sinks:
            config += sink.get_pipewire_config(master_sink, managed_usb=self.hardware_selector is not None)
        return config

def config2audiodevice(config: Mapping[str, object]) -> AudioDevice:
    """Convert config dict to AudioDevice object."""
    selection = hardware_configuration(config)
    entries = config.get("sinks", [])
    name, profile = config.get("name"), config.get("card-profile")
    if not isinstance(entries, list) or not isinstance(name, str) or not isinstance(profile, str):
        raise ValueError("Audio device requires name, profile and sinks")
    sinks = [config2sink(object_mapping(entry)) for entry in cast(list[object], entries)]
    print(f"Converted {len(sinks)} sinks for audio device '{config.get('name')}'")
    return AudioDevice(
        name=name,
        card_profile=profile,
        sinks=sinks,
        hardware_selector=selection[0] if selection else None,
    )
