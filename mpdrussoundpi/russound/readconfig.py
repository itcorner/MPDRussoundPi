import json


config = None

def read_and_parse_json(filename:str='multimpd.json' ) -> dict | None:
    """Read and parse multimpd.json into a structured format."""
    global config
    try:
        with open(filename, 'r') as f:
            data = json.load(f)
            config = data

        return data
    except FileNotFoundError:
        print(f"Error: {filename} not found")
        return None
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in {filename}")
        return None

def sink_exists_in_config(config, sink_name):
    """Check if sink exists in outputmappers."""
    audio_devices = config.get('outputmappers', {}).get('audio_devices', [])
    for device in audio_devices:
        for sink in device.get('sinks', []):
            if sink.get('name') == sink_name:
                return True
    return False