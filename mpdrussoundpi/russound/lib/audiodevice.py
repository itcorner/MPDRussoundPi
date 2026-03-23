import logging
from lib.sink import Sink, config2sink

logging.basicConfig(level=logging.DEBUG)

class AudioDevice:
    def __init__(self, name: str, card_profile: str, sinks: list[Sink]):
        self.name = name
        self.card_profile = card_profile
        self.sinks = sinks

    def get_pulseaudio_card_config(self) -> str:
        """Generate PulseAudio card configuration for this audio device."""
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
            config += sink.get_pipewire_config(master_sink)
        return config

def config2audiodevice(config):
    """Convert config dict to AudioDevice object."""
    sinks = [config2sink(sink_config) for sink_config in config.get('sinks', [])]
    print(f"Converted {len(sinks)} sinks for audio device '{config.get('name')}'")
    return AudioDevice(
        name=config.get('name'),
        card_profile=config.get('card-profile'),
        sinks=sinks
    )

