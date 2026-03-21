


def config2sink(config):
    """Convert config dict to Sink object."""
    return Sink(
        name=config.get('name'),
        master_channel_map=config.get('master_channel_map'),
        channel_map=config.get('channel_map'),
        remix=config.get('remix'),
        channels=config.get('channels'),
        sink_properties=config.get('sink_properties')
    )


class Sink:
    def __init__(self, name: str, master_channel_map: str, channel_map: str, remix: str, channels: int, sink_properties:str):
        self.name = name
        self.master_channel_map = master_channel_map
        self.channel_map = channel_map
        self.remix = remix
        self.channels = channels
        self.sink_properties = sink_properties

    def get_name(self) -> str:
        """Get the name of the sink."""
        return self.name
    
    def get_pulseaudio_config(self, master_sink: str) -> str:
        """Generate PulseAudio configuration for this sink."""
        config = f"# Remap sink for {self.name}\n"
        config += "load-module module-remap-sink "
        config += f"master={master_sink} "
        config += f"sink_name=\"{self.name}\" "
        config += f"channel_map={self.channel_map} "
        config += f"master_channel_map={self.master_channel_map} "
        config += f"channels={self.channels} "
        
        if self.sink_properties:
            config += f"sink_properties=\"{self.sink_properties}\" "
        config += "\n\n"
        
        return config
    