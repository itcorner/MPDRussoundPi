


def config2sink(config):
    """Convert config dict to Sink object."""
    return Sink(
        name=config.get('name'),
        master_channel_map=config.get('master_channel_map'),
        master_channel_map_pw=config.get('master_channel_map_pw'),
        channel_map=config.get('channel_map'),
        channel_map_pw=config.get('channel_map_pw'),
        remix=config.get('remix'),
        channels=config.get('channels'),
        sink_properties=config.get('sink_properties'),
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



class Sink:
    def __init__(self, name: str, master_channel_map: str, master_channel_map_pw: str, channel_map: str, channel_map_pw: str, remix: str, channels: int, sink_properties:str, sink_description:str = None):
        self.name = name
        self.master_channel_map = master_channel_map
        self.master_channel_map_pw = master_channel_map_pw
        self.channel_map = channel_map
        self.channel_map_pw = channel_map_pw
        self.remix = remix # Note: not used in current implementation
        self.channels = channels
        self.sink_properties = sink_properties
        self.sink_description = sink_description

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
        config += f"channels={self.channels} remix=no "
        
        if self.sink_properties:
            config += f"sink_properties=\"{self.sink_properties}\" "
        config += "\n\n"
        
        return config
    
    def get_pipewire_config(self, master_sink: str) -> str:
        """Generate PipeWire configuration for this sink."""
        # Note: The config uses a loopback module to create a new sink that maps to the master sink with the specified channel mapping. The 'stream.dont-remix' property is set to true to prevent automatic remixing of channels, ensuring that the channel mapping is preserved as defined in the configuration.
        config = f"    # Remap sink for {self.name}\n"
        config += "    {\n"
        config += "        name = libpipewire-module-loopback\n"
        config += "        args = {\n"
        config += f"            node.description = \"PipeWire {self.sink_description}\"\n"
        config += f"            audio.position = [ FL FR ]\n"
        config += "            capture.props = {\n"
        config += f"                node.name = \"sink.{self.name}\"\n"
        config += "                media.class = \"Audio/Sink\"\n"
        config += "            }\n"
        config += "            playback.props = {\n"
        config += f"                node.name = \"playback.{self.name}\"\n"
        config += f"                audio.position = {self.master_channel_map_pw}\n"
        config += f"                target.object = \"{master_sink}\"\n"
        config += "                stream.dont-remix = true\n"
        config += "                node.passive = true\n"
        config += "            }\n"
        config += "        }\n"
        config += "    },\n"

        #args = f"module-remap-sink master={master_sink} sink_name={self.name} channel_map={self.channel_map} master_channel_map='{self.master_channel_map}' channels={self.channels} sink_properties='{self.sink_properties}'"
        #config = f"# Remap sink for {self.name}\n"
        #config += f"    {{ cmd = \"load-module\" args = \"{args}\"  }}\n"

        return config