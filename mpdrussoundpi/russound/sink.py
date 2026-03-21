


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
    