import lib.readconfig as readconfig
from lib.mpdinstance import MPDInstance, config2mpd
from lib.systeminfo import is_system_on
import lib.sink as sink
import os
import sys
import logging


from mpdsetup import SYSTEMD_MPD_METASERVICE

import argparse
import time

LOCKFILE = '/tmp/mpd_control.lock'
SYSTEMD_USER_CONFIG_DIR = os.path.expanduser('~/.config/systemd/user/')
SYSTEMD_AUTOMPD_SERVICE = 'auto-mpd-control.service'



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
                    logging.info("System turned on. Starting streams...")
                    play(mpd_instances=mpd_instances)
                else:
                    logging.debug("System is on. Streams should be playing.")
            else:
                if old_status != "off":
                    old_status = "off"
                    logging.info("System is off. Stopping streams.")
                    stop(mpd_instances=mpd_instances)
                else:
                    logging.debug("System is off. Streams should be stopped.")
            time.sleep(1)  # Check every second

    except KeyboardInterrupt:
        logging.info("Exiting auto control mode.")


def install_systemd_service(json_config_path: str):
    """Create systemd service file for auto-MPD control."""
    if not os.path.exists(SYSTEMD_USER_CONFIG_DIR):
        os.makedirs(SYSTEMD_USER_CONFIG_DIR)

    service_file = os.path.join(SYSTEMD_USER_CONFIG_DIR, SYSTEMD_AUTOMPD_SERVICE)
    with open(service_file, 'w') as f:
        f.write(f"[Unit]\nDescription=Auto-MPD Control based on Russound system state\nAfter=network.target sound.target {SYSTEMD_MPD_METASERVICE}\n\n")
        f.write(f"[Service]\nExecStart={sys.executable} {__file__} --auto {json_config_path}\n")
        f.write("\n[Install]\nWantedBy=default.target\n")
    print(f"Created systemd service: {service_file}")

def enable_systemd_service():
    """Enable the auto-MPD control systemd service."""
    os.system(f"systemctl --user enable {SYSTEMD_AUTOMPD_SERVICE}")
    print(f"Enabled systemd service: {SYSTEMD_AUTOMPD_SERVICE}")

def disable_systemd_service():
    """Disable the auto-MPD control systemd service."""
    os.system(f"systemctl --user disable {SYSTEMD_AUTOMPD_SERVICE}")
    print(f"Disabled systemd service: {SYSTEMD_AUTOMPD_SERVICE}")

def cleanup_systemd_service():
    """Remove the auto-MPD control systemd service file."""
    service_file = os.path.join(SYSTEMD_USER_CONFIG_DIR, SYSTEMD_AUTOMPD_SERVICE)
    if os.path.exists(service_file):
        os.remove(service_file)
        print(f"Removed systemd service file: {service_file}")
    else:
        print(f"No systemd service file found at {service_file}. Nothing to clean up.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Control multiple MPD instances')
    parser.add_argument('--start', action='store_true', help='Start all configured streams')
    parser.add_argument('--status', action='store_true', help='Get status of all configured streams')
    parser.add_argument('--stop', action='store_true', help='Stop all configured streams')
    parser.add_argument('--auto', action='store_true', help='Automatically start streams if system is on')
    parser.add_argument('--install', action='store_true', help='Install systemd service for auto control')
    parser.add_argument('--cleanup', action='store_true', help='Stop service and cleanup systemd service files')
    parser.add_argument('--enable', action='store_true', help='Enable systemd service for auto control')
    parser.add_argument('--disable', action='store_true', help='Disable systemd service for auto control')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('json', help='Path to JSON configuration file')
    args = parser.parse_args()

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S')
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    root.addHandler(handler)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        handler.setLevel(logging.DEBUG)
        logging.debug("Debug logging enabled.")

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

    if args.install:
        acquire_lock()
        install_systemd_service(os.path.abspath(args.json))
        release_lock()
        exit(0)
    elif args.enable:
        acquire_lock()
        enable_systemd_service()
        release_lock()
        exit(0)
    elif args.disable:
        acquire_lock()
        disable_systemd_service()
        release_lock()
        exit(0)
    elif args.cleanup:
        acquire_lock()
        stop(mpd_instances)
        time.sleep(2)  # Give some time for MPD instances to stop before cleaning up systemd service
        disable_systemd_service() # Disable service to prevent it from starting on boot after cleanup
        cleanup_systemd_service() # Remove systemd service file
        release_lock()
        exit(0)

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
