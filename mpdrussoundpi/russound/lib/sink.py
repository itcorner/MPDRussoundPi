


def config2sink(config):
    """Convert config dict to Sink object."""
    return Sink(
        name=config.get('name'),
        master_channel_map=config.get('master_channel_map'),
        channel_map=config.get('channel_map', '[ FL FR ]'), # Default to stereo if not specified
        remix=config.get('remix'),
        sink_description=config.get('sink_description')
    )


#use in future to get rid of one channel map in config
channel_map_dict = {
    'FL': 'front-left',
    'FR': 'front-right',
    'FC': 'front-center',
    'LFE': 'lfe',
    'RL': 'rear-left',
    'RR': 'rear-right',
    'SL': 'side-left',
    'SR': 'side-right',
    # Add more mappings as needed
}

def convert_channel_map(channel_map: str) -> str:
    """Convert PipeWire channel map to PulseAudio format."""
    channels = channel_map.strip('[]').split()
    converted_channels = [channel_map_dict.get(ch, ch) for ch in channels]
    return ','.join(converted_channels)

def get_numchannels_from_channel_map(channel_map: str) -> int:
    """Get the number of channels from a PipeWire channel map string."""
    return len(channel_map.strip('[]').split())

class Sink:
    def __init__(self, name: str, master_channel_map: str, channel_map: str, remix: str, sink_description:str = ""):
        self.name = name
        self.master_channel_map = master_channel_map
        self.channel_map = channel_map
        self.remix = remix # Note: not used in current implementation
        self.sink_description = sink_description

    def get_name(self) -> str:
        """Get the name of the sink."""
        return self.name
    
    def get_pulseaudio_config(self, master_sink: str) -> str:
        """Generate PulseAudio configuration for this sink."""
        config = f"# Remap sink for {self.name}\n"
        config += "load-module module-remap-sink "
        config += f"master={master_sink} "
        config += f"sink_name=\"sink.{self.name}\" "
        config += f"channel_map={convert_channel_map(self.channel_map)} "
        config += f"master_channel_map={convert_channel_map(self.master_channel_map)} "
        config += f"channels={get_numchannels_from_channel_map(self.channel_map)} remix=no "
        
        if self.sink_description and self.sink_description.strip() != "":
            config += f"sink_properties=\"device.description={self.sink_description}\" "
        config += "\n\n"
        return config
    
    def get_pipewire_config(self, master_sink: str) -> str:
        """Generate PipeWire configuration for this sink."""
        # Note: The config uses a loopback module to create a new sink that maps to the master sink with the specified channel mapping. The 'stream.dont-remix' property is set to true to prevent automatic remixing of channels, ensuring that the channel mapping is preserved as defined in the configuration.
        config = f"    # Remap sink for {self.name}\n"
        config += "    {\n"
        config += "        name = libpipewire-module-loopback\n"
        config += "        args = {\n"
        if self.sink_description and self.sink_description.strip() != "":
            config += f"            node.description = \"PipeWire {self.sink_description}\"\n"
        config += f"            audio.position = {self.channel_map}\n"
        config += "            capture.props = {\n"
        config += f"                node.name = \"sink.{self.name}\"\n"
        config += "                media.class = \"Audio/Sink\"\n"
        config += "            }\n"
        config += "            playback.props = {\n"
        config += f"                node.name = \"playback.{self.name}\"\n"
        config += f"                audio.position = {self.master_channel_map}\n"
        config += f"                target.object = \"{master_sink}\"\n"
        config += "                stream.dont-remix = true\n"
        config += "                node.passive = true\n"
        config += "            }\n"
        config += "        }\n"
        config += "    },\n"
        return config