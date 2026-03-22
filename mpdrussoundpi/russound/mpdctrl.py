import lib.readconfig as readconfig
from lib.mpdinstance import MPDInstance, config2mpd
from lib.systeminfo import is_system_on
import lib.sink as sink

import argparse
import time

LOCKFILE = '/tmp/mpd_control.lock'

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

def play(mpd_instances: list[MPDInstance]):
    # Play all streams
    for instance in mpd_instances:
        instance.play()

def stop(mpd_instances: list[MPDInstance]):
    # Stop all streams
    for instance in mpd_instances:
        instance.stop()

def status(mpd_instances: list[MPDInstance]):
    # Print status of all streams
    for instance in mpd_instances:
        s = instance.get_stream_status()
        if s:
            print(f"Stream '{instance.stream.get_name()}' status: {s}")

def auto_control(mpd_instances: list[MPDInstance]):
    
    old_status = None
    try:

        while True:
            if is_system_on():
                if old_status != "on":
                    old_status = "on"
                    print("System is on. Starting streams...")
                    play(mpd_instances=mpd_instances)
                else:
                    print("System is on. Streams should be playing.")
            else:
                if old_status != "off":
                    old_status = "off"
                    print("System is off. Stopping streams.")
                    stop(mpd_instances=mpd_instances)
                else:
                    print("System is off. Streams should be stopped.")
            time.sleep(10)  # Check every second

    except KeyboardInterrupt:
        print("Exiting auto control mode.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Control multiple MPD instances')
    parser.add_argument('--start', action='store_true', help='Start all configured streams')
    parser.add_argument('--status', action='store_true', help='Get status of all configured streams')
    parser.add_argument('--stop', action='store_true', help='Stop all configured streams')
    parser.add_argument('--auto', action='store_true', help='Automatically start streams if system is on')
    parser.add_argument('json', help='Path to JSON configuration file')
    args = parser.parse_args()

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
        
    mpd_instances : list[MPDInstance] = []
    for stream_config in config.get('streams', []):
        mpd_instance_obj = config2mpd(stream_config, sink_dict, ignore_missing_sinks=True)
        if mpd_instance_obj:
            mpd_instances.append(mpd_instance_obj)
            print(f"Configured MPD instance: {mpd_instance_obj.name} on port {mpd_instance_obj.port} with stream URL: {mpd_instance_obj.stream.get_url()}")

    if args.auto:
        # Automatically control streams based on system power state
        acquire_lock()
        auto_control(mpd_instances)
        release_lock()
    elif args.start:
        # Start all streams by connecting to MPD and sending play command
        acquire_lock()
        play(mpd_instances)
        release_lock()
    elif args.stop:
        # Stop all streams by connecting to MPD and sending stop command
        acquire_lock()
        stop(mpd_instances)
        release_lock()
    elif args.status:
        # Get status of all streams by connecting to MPD and sending status command
        status(mpd_instances)
