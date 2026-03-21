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



def config2audiodevice(config):
    """Convert config dict to AudioDevice object."""
    sinks = [config2sink(sink_config) for sink_config in config.get('sinks', [])]
    print(f"Converted {len(sinks)} sinks for audio device '{config.get('name')}'")
    for sink in sinks:
        logging.debug(f"Sink '{sink.get_name()}' details: master_channel_map={sink.master_channel_map}, "
                      f"channel_map={sink.channel_map}, remix={sink.remix}, channels={sink.channels}, "
                      f"sink_properties={sink.sink_properties}")
    return AudioDevice(
        name=config.get('name'),
        card_profile=config.get('card-profile'),
        sinks=sinks
    )

