import lib.readconfig as readconfig
from lib.mpdinstance import MPDInstance, config2mpd
import argparse
import os
import subprocess
import lib.sink as sink
import logging
import time

LOCKFILE = '/tmp/mpd_setup.lock'

MPD_CONFIG_DIR = os.path.expanduser('~/.config/mpd/')
PULSE_CONFIG = os.path.expanduser('~/.config/pulse/default.pa')

PIPEWIRE_CONFIG = os.path.expanduser('~/.config/pipewire/pipewire-pulse.conf.d/remapped-sinks.conf')
WIREPLUMBER_CONFIG = os.path.expanduser('~/.config/pipewire/wireplumber.conf.d/card-modes.conf')
SYSTEMD_USER_CONFIG_DIR = os.path.expanduser('~/.config/systemd/user/')
SYSTEMD_MPD_METASERVICE = 'mpd_meta_control.service'


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

def mpd_config_filename(instance_name: str) -> str:
    """Generate a safe filename for the MPD config based on the instance name."""
    instance_name_safe = instance_name.replace(" ", "_").lower()
    return f"mpd_{instance_name_safe}.conf"

def mpd_service_filename(instance_name: str) -> str:
    """Generate a safe filename for the systemd service based on the instance name."""
    instance_name_safe = instance_name.replace(" ", "_").lower()
    return f"mpd_{instance_name_safe}.service"

def create_mpd_config(mpd_instance: MPDInstance):
    """Create MPD config file for instance."""
    if not os.path.exists(MPD_CONFIG_DIR):
        os.makedirs(MPD_CONFIG_DIR)

    config_filename = mpd_config_filename(mpd_instance.name)
    config_file = os.path.join(MPD_CONFIG_DIR, config_filename)
    with open(config_file, 'w') as f:
        # Write basic MPD config settings
        f.write(mpd_instance.to_mpd_config())
        logging.info(f"Created MPD config: {config_file}")

def create_mpd_metaservice():
    """Create a systemd service that can be used as a target for propagating stop/reload signals to all MPD instance services."""
    if not os.path.exists(SYSTEMD_USER_CONFIG_DIR):
        os.makedirs(SYSTEMD_USER_CONFIG_DIR)

    service_file = os.path.join(SYSTEMD_USER_CONFIG_DIR, SYSTEMD_MPD_METASERVICE)
    with open(service_file, 'w') as f:
        f.write(f"[Unit]\nDescription=MPD Meta Control Service\nAfter=network.target sound.target\n\n")
        f.write(f"Wants=mpd_*.service\n")
        f.write(f"[Service]\nType=oneshot\nExecStart=/bin/true\nExecStop=/bin/true\nRemainAfterExit=yes\n")
        f.write("\n[Install]\nWantedBy=default.target\n")
    logging.info(f"Created MPD meta-control systemd service: {service_file}")
    print(f"Created MPD meta-control systemd service: {service_file}")

def enable_mpd_metaservice():
    pass

def cleanup_mpd_metaservice():
    """Remove the MPD meta-control systemd service file."""
    service_file = os.path.join(SYSTEMD_USER_CONFIG_DIR, SYSTEMD_MPD_METASERVICE)
    if os.path.exists(service_file):
        os.remove(service_file)
        logging.info(f"Removed MPD meta-control systemd service: {service_file}")

def create_mpd_systemd_service(mpd_instance: MPDInstance):
    """Create systemd service file for MPD instance."""
    if not os.path.exists(SYSTEMD_USER_CONFIG_DIR):
        os.makedirs(SYSTEMD_USER_CONFIG_DIR)

    service_filename = mpd_service_filename(mpd_instance.name)
    service_file = os.path.join(SYSTEMD_USER_CONFIG_DIR, service_filename)
    config_filename = mpd_config_filename(mpd_instance.name)
    config_file = os.path.join(MPD_CONFIG_DIR, config_filename)
    with open(service_file, 'w') as f:
        f.write(f"[Unit]\nDescription=MPD instance for {mpd_instance.name}\nAfter=network.target sound.target\n")
        f.write(f"StopPropagatedFrom={SYSTEMD_MPD_METASERVICE}\nReloadPropagatedFrom={SYSTEMD_MPD_METASERVICE}\n\n")
        f.write(f"[Service]\nExecStart=/usr/bin/mpd --no-daemon {config_file}\n")
        #f.write("LimitRTPRIO=50\nLimitRTTIME=-1\nControlGroup=cpu:/mpd\nControlGroupAttribute=cpu.rt_runtime_us 500000\n")
        #f.write("\n[Install]\nWantedBy=default.target\n")
    print(f"Created systemd service: {service_file}")


def kill_all_mpd_instances():
    """Kill all running MPD instances."""
    try:
        subprocess.run(['pkill', '-f', 'mpd'], check=True)
        logging.info("Killed all running MPD instances.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error killing MPD instances: {e}")

def get_available_systemd_mpd_services() -> list[str]:
    """Get a list of available systemd services for MPD instances."""
    services: list[str] = []

    if not os.path.exists(SYSTEMD_USER_CONFIG_DIR):
        logging.info(f"No systemd user config directory found at {SYSTEMD_USER_CONFIG_DIR}.")
    else:    
        for filename in os.listdir(SYSTEMD_USER_CONFIG_DIR):
            if filename.startswith('mpd_') and filename.endswith('.service'):
                services.append(filename)
    return services

def start_all_mpd_services():
    """Start all available systemd services for MPD instances."""
    services = get_available_systemd_mpd_services()
    if not services:
        logging.info("No systemd services found for MPD instances.")
        return

    for service in services:
        try:
            subprocess.run(['systemctl', '--user', 'start', service], check=True)
            logging.info(f"Started systemd service: {service}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Error starting systemd service {service}: {e}")

def enable_all_mpd_services():
    """Enable all available systemd services for MPD instances by meta-service to start on boot."""
    try:
        subprocess.run(['systemctl', '--user', 'enable', SYSTEMD_MPD_METASERVICE], check=True)
        logging.info(f"Enabled systemd meta-service for MPD instances: {SYSTEMD_MPD_METASERVICE}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error enabling systemd meta-service {SYSTEMD_MPD_METASERVICE}: {e}")

def disable_all_mpd_services():
    """Disable all available systemd services for MPD instances by meta-service to prevent starting on boot."""
    try:
        subprocess.run(['systemctl', '--user', 'disable', SYSTEMD_MPD_METASERVICE], check=True)
        logging.info(f"Disabled systemd meta-service for MPD instances: {SYSTEMD_MPD_METASERVICE}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error disabling systemd meta-service {SYSTEMD_MPD_METASERVICE}: {e}")

def enable_all_mpd_services_individually():
    """Enable all available systemd services for MPD instances to start on boot."""
    services = get_available_systemd_mpd_services()
    if not services:
        logging.info("No systemd services found for MPD instances.")
        print("No systemd services found for MPD instances.")
    else:
        for service in services:
            try:
                subprocess.run(['systemctl', '--user', 'enable', service], check=True)
                logging.info(f"Enabled systemd service: {service}")
            except subprocess.CalledProcessError as e:
                logging.error(f"Error enabling systemd service {service}: {e}")
                print(f"Error enabling systemd service {service}: {e}")

def stop_all_mpd_services():
    """Stop all available systemd services for MPD instances."""
    services = get_available_systemd_mpd_services()
    if not services:
        logging.info("No systemd services found for MPD instances.")
        return

    for service in services:
        try:
            subprocess.run(['systemctl', '--user', 'stop', service], check=True)
            logging.info(f"Stopped systemd service: {service}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Error stopping systemd service {service}: {e}")

def start_mpd_service_by_instance(mpd_instance: MPDInstance):
    """Start the systemd service for the given MPD instance."""
    service_filename = mpd_service_filename(mpd_instance.name)
    service_file = os.path.join(SYSTEMD_USER_CONFIG_DIR, service_filename)
    if os.path.exists(service_file):
        try:
            subprocess.run(['systemctl', '--user', 'start', service_filename], check=True)
            logging.info(f"Started systemd service: {service_filename}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Error starting systemd service {service_filename}: {e}")
    else:
        logging.warning(f"No systemd service file found for instance '{mpd_instance.name}' at {service_file}. Cannot start service.")

def cleanup_mpd_configs():
    """Remove all auto-generated MPD config files."""
    if not os.path.exists(MPD_CONFIG_DIR):
        logging.info(f"No MPD config directory found at {MPD_CONFIG_DIR}. Nothing to clean up.")
        return
    for filename in os.listdir(MPD_CONFIG_DIR):
        if filename.startswith('mpd_') and filename.endswith('.conf'):
            os.remove(os.path.join(MPD_CONFIG_DIR, filename))
            logging.info(f"Removed MPD config file: {filename}")

def cleanup_systemd_services():
    """Remove all auto-generated systemd service files."""
    if not os.path.exists(SYSTEMD_USER_CONFIG_DIR):
        logging.info(f"No systemd user config directory found at {SYSTEMD_USER_CONFIG_DIR}. Nothing to clean up.")
    else:
        for filename in os.listdir(SYSTEMD_USER_CONFIG_DIR):
            if filename.startswith('mpd_') and filename.endswith('.service'):
                os.remove(os.path.join(SYSTEMD_USER_CONFIG_DIR, filename))
                logging.info(f"Removed systemd service file: {filename}")
    # Also remove the MPD meta-control service file
    # Should already be removed by cleanup_systemd_services(), but just in case, call the specific cleanup for the meta-control service as well
    cleanup_mpd_metaservice()

def cleanup_all():
    """Remove all auto-generated config files."""
    cleanup_mpd_configs()
    cleanup_systemd_services()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Setup multiple MPD instances')
    parser.add_argument('--install', action='store_true', help='Install MPD configuration')
    parser.add_argument('--start', action='store_true', help='Start all MPD instances')
    parser.add_argument('--stop', action='store_true', help='Stop all MPD instances')
    parser.add_argument('--enable', action='store_true', help='Enable all MPD systemd services to start on boot')
    parser.add_argument('--disable', action='store_true', help='Disable all MPD systemd services from starting on boot')
    parser.add_argument('--killmpd', action='store_true', help='Stop and kill all running MPD instances')
    parser.add_argument('--cleanup', action='store_true', help='Stop all MPD instances and remove all auto-generated config files')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('json', help='Path to JSON configuration file')    
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug("Debug logging enabled.")

    if args.killmpd:
        # Stop all running MPD instances (in case any are still running)
        stop_all_mpd_services()
        # Give some time for services to stop before killing any remaining MPD processes
        time.sleep(2)
        # Kill any remaining MPD processes that may not have been stopped by systemd services
        kill_all_mpd_instances()
        exit(0)
    elif args.cleanup:
        # Stop all running MPD instances (in case any are still running) and remove all auto-generated config files
        stop_all_mpd_services()
        time.sleep(2)  # Give some time for services to stop before cleaning up files
        disable_all_mpd_services()  # Disable services to prevent them from starting on boot after cleanup
        # Remove all auto-generated MPD config files and systemd service files
        cleanup_all()
        exit(0)
    elif args.install:
        # Create MPD config files for each instance and start MPD with those configs
        acquire_lock()

        config = readconfig.read_and_parse_json(args.json)
        if not config:
            logging.error("Failed to read configuration. Exiting.")
            parser.print_help()
            exit(1)

        sink_dict: dict[str, sink.Sink] = {}
        for audio_device in config.get('outputmappers', {}).get('audio_devices', []):
            for sink_element in audio_device.get('sinks', []):
                logging.debug(f"Audio Device: {audio_device.get('name')}, Sink: {sink_element.get('name')}")
                sink_dict[sink_element.get('name')] = sink.config2sink(sink_element)

        mpd_instances: list[MPDInstance] = []
        for stream_config in config.get('streams', []):
            inst = config2mpd(stream_config, sink_dict, ignore_missing_sinks=False)
            if inst:            
                mpd_instances.append(inst)
                logging.debug(f"Found valid configuration for MPD instance: {inst.name} on port {inst.port} with stream URL: {inst.stream.get_url()}")


        for instance in mpd_instances:
            create_mpd_config(instance)
            create_mpd_systemd_service(instance)
        create_mpd_metaservice()

        release_lock()

    if args.start:
        # Start all MPD instances using systemd services
        start_all_mpd_services()
        exit(0)
    elif args.stop:
        # Stop all MPD instances using systemd services
        stop_all_mpd_services()
        exit(0)

    if args.enable:
        # Enable all MPD systemd services to start on boot
        enable_all_mpd_services()
    elif args.disable:
        # Disable all MPD systemd services from starting on boot
        disable_all_mpd_services()

