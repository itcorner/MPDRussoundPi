import json
import logging
import russound
#from russound import Russound
#import Russound.russound as russound
#import time

def is_system_on() -> (bool):
    system_on = False

    try:
        #IP_ADDRESS = '192.168.1.198'
        # connect to local host running ser2net service connected to the russound system via serial cable
        IP_ADDRESS = '127.0.0.1'
        PORT = 6666
        logging.basicConfig(filename='russound_debugging.log', level=logging.DEBUG,
                            format='%(asctime)s:%(name)s:%(levelname)s:%(funcName)s():%(message)s')
        _LOGGER = logging.getLogger(__name__)
        _LOGGER.level = logging.DEBUG

        x = russound.Russound(IP_ADDRESS, PORT)
        x.connect()
        x.is_connected()

        #we only want to know if system is on. for this we can focus on controller 1, zone 1
        controller=1
        zone=1
        if x.get_power(controller, zone) == 1:
            system_on = True
    except:
        pass

    return system_on


def main():
    system_info = {}
    if is_system_on():
        system_info['system_on'] = 1
    else:
        system_info['system_on'] = 0

    print(json.dumps(system_info))

if __name__ == "__main__":
    main()