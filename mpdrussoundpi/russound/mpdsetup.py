import lib.readconfig as readconfig
from lib.mpdinstance import MPDInstance, config2mpd
import argparse
import os
import subprocess
import lib.sink as sink
import logging

LOCKFILE = '/tmp/mpd_setup.lock'

MPD_CONFIG_DIR = os.path.expanduser('~/.config/mpd/')
PULSE_CONFIG = os.path.expanduser('~/.config/pulse/default.pa')

PIPEWIRE_CONFIG = os.path.expanduser('~/.config/pipewire/pipewire-pulse.conf.d/remapped-sinks.conf')
WIREPLUMBER_CONFIG = os.path.expanduser('~/.config/pipewire/wireplumber.conf.d/card-modes.conf')

logging.basicConfig(filename='russound_debugging.log', level=logging.DEBUG,
                    format='%(asctime)s:%(name)s:%(levelname)s:%(funcName)s():%(message)s')

def acquire_lock():
    """Acquire a lock to prevent multiple instances of the script from running simultaneously."""
    import os
    import sys

    if os.path.exists(LOCKFILE):
        print("Another instance of the script is already running. Exiting.")
        print(f"If you are sure no other instance is running, delete the lock file: {LOCKFILE}")
        sys.exit(1)

    with open(LOCKFILE, 'w') as f:
        f.write(str(os.getpid()))

def release_lock():
    """Release the lock when the script finishes."""
    import os

    if os.path.exists(LOCKFILE):
        # Check if the PID in the lock file matches the current process before removing it
        with open(LOCKFILE, 'r') as f:
            pid = f.read()
            if pid == str(os.getpid()):
                os.remove(LOCKFILE)

def create_mpd_config(mpd_instance: MPDInstance, config_filename: str):
    """Create MPD config file for instance."""
    if not os.path.exists(MPD_CONFIG_DIR):
        os.makedirs(MPD_CONFIG_DIR)

    config_file = os.path.join(MPD_CONFIG_DIR, config_filename)
    with open(config_file, 'w') as f:
        # Write basic MPD config settings
        f.write(mpd_instance.to_mpd_config())
        print(mpd_instance.to_mpd_config())

        print(f"Created MPD config: {config_file}")

        #Start MPD with config.
        try:
            subprocess.Popen(['mpd', config_file])
        except Exception as e:
            print(f"Error starting MPD with config {config_file}: {e}")

def kill_all_mpd_instances():
    """Kill all running MPD instances."""
    try:
        subprocess.run(['pkill', '-f', 'mpd'], check=True)
        print("Killed all running MPD instances.")
    except subprocess.CalledProcessError as e:
        print(f"Error killing MPD instances: {e}")

def cleanup_configs():
    """Remove all auto-generated MPD config files."""
    if not os.path.exists(MPD_CONFIG_DIR):
        print(f"No MPD config directory found at {MPD_CONFIG_DIR}. Nothing to clean up.")
        return
    for filename in os.listdir(MPD_CONFIG_DIR):
        if filename.startswith('mpd_') and filename.endswith('.conf'):
            os.remove(os.path.join(MPD_CONFIG_DIR, filename))
            print(f"Removed config file: {filename}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Setup multiple MPD instances')
    parser.add_argument('--initialize', action='store_true', help='Initialize MPD configuration')
    parser.add_argument('--killmpd', action='store_true', help='Kill all running MPD instances')
    parser.add_argument('--cleanup', action='store_true', help='Remove all auto-generated config files')
    parser.add_argument('json', help='Path to JSON configuration file')    
    args = parser.parse_args()


    if args.killmpd:
        # Kill all running MPD instances
        kill_all_mpd_instances()
    elif args.cleanup:
        # Remove all auto-generated MPD config files
        cleanup_configs()
    elif args.initialize:
        # Create MPD config files for each instance and start MPD with those configs
        acquire_lock()

        config = readconfig.read_and_parse_json(args.json)
        if not config:
            print("Failed to read configuration. Exiting.")
            parser.print_help()
            exit(1)

        sink_dict: dict[str, sink.Sink] = {}
        for audio_device in config.get('outputmappers', {}).get('audio_devices', []):
            for sink_element in audio_device.get('sinks', []):
                print(f"Audio Device: {audio_device.get('name')}, Sink: {sink_element.get('name')}")
                sink_dict[sink_element.get('name')] = sink.config2sink(sink_element)

        mpd_instances: list[MPDInstance] = []
        for stream_config in config.get('streams', []):
            inst = config2mpd(stream_config, sink_dict, ignore_missing_sinks=False)
            if inst:            
                mpd_instances.append(inst)
                print(f"Found valid configuration for MPD instance: {inst.name} on port {inst.port} with stream URL: {inst.stream.get_url()}")


        for instance in mpd_instances:
            instance_name_safe = instance.name.replace(" ", "_").lower()
            config_filename = f"mpd_{instance_name_safe}.conf"
            create_mpd_config(instance, config_filename)

        release_lock()
