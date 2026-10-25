# Homeassistant Config for Vaillant Heating System

<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2024/05/ha-heatpump.png" width="500">

## Heating Device
Vaillant aroTHERM plus VWL 125

## General Information
* All important configuration is done in yaml files, not the ui editor to be able to store the config in git.
* The heating system is installed in an apartment building containing 3 flats. The temperatures from those 3 flats are used to automate heating control.
* Private information (like flat names or notification devices) are stored in secrets.yaml and not part of this repo. A [secrets.yaml.example](secrets.yaml.example) is included which can be easily adapted for your needs.

## Prerequisites
* ebus Adapter: https://ebusd.eu/
* ebus Daemon running as HA Addon (https://github.com/LukasGrebe/ha-addons) or separate docker container. The addon only mounts its own addon config folder (`/addon_configs/2ad9b828_ebusd/` on the host, `/config` inside the ebusd container), not the HA config folder, so the paths below are container paths. Addon configuration (`commandline_options`):
```
  seed_mqtt_cfg: false
  commandline_options:
    - --mqttjson
    - --scanconfig
    - --log=all:error
    - --mqtttopic=ebusd
    - --configpath=/config/ebusd/csvs/1755c15-fixes
    - --mqttvar=filter-non-circuit=,filter-non-name=^Z2|^Z3|^Hc2|^Hc3,filter-name=status|temp|humidity|yield|count|energy|power|runtime|hours|starts|mode|curve|^load$|^party$|sensor|timer|cop|hwc|hc|cylinder|heating,filter-direction=r|u|^w
    - --mqttint=/config/ebusd/mqtt-hassio.cfg
  network_device: enh:/dev/serial/by-id/<your ebus adapter>
```
* Own ebusd files are kept in this repo in [ebusd/](ebusd): `mqtt-hassio.cfg` (MQTT integration for HA discovery), `ebus-reader.sh` (called by cron in the SSH addon, runs `readimportant.sh` inside the ebusd container), `readimportant.sh` (actively reads important values every 5/15 minutes and daily), `readall.sh` and `readvaillant.sh` (diagnostics). `deploy.sh` copies them to `/addon_configs/2ad9b828_ebusd/ebusd/`; after a change of `mqtt-hassio.cfg` the ebusd addon has to be restarted.
* The message definitions (CSV files) come from https://github.com/cluk1/ebusd-configuration (fork of the ebusd configuration with own fixes) and are located in `/addon_configs/2ad9b828_ebusd/ebusd/csvs/`, the variant in use is `1755c15-fixes`.
* mqtt Broker running as HA Addon (https://github.com/home-assistant/addons/tree/master/mosquitto) or separate docker container, addon configuration:
```
  "logins": [],
  "require_certificate": false,
  "certfile": "fullchain.pem",
  "keyfile": "privkey.pem",
  "customize": {
    "active": true,
    "folder": "mosquitto"
  }
```
* Advanced SSH Addon (https://github.com/hassio-addons/addon-ssh), addon configuration:
```
  "ssh": {
    "username": "hassio",
    "password": "",
    "authorized_keys": [
      "ssh-rsa <your ssh pubkey here>"
    ],
    "sftp": false,
    "compatibility_mode": false,
    "allow_agent_forwarding": true,
    "allow_remote_port_forwarding": false,
    "allow_tcp_forwarding": false
  },
  "zsh": false,
  "share_sessions": false,
  "packages": [],
  "init_commands": [
    "(crontab -l; cat /config/cron.d/*) | crontab -",
    "crond"
  ]
```
* The cron jobs in [cron.d](cron.d) are loaded into the SSH addon's crontab on addon start: [cron.d/ebusd](cron.d/ebusd) periodically triggers ebusd reads via `/addon_configs/2ad9b828_ebusd/ebusd/ebus-reader.sh`, [cron.d/influxdb](cron.d/influxdb) creates a nightly InfluxDB backup. Changes to cron.d only take effect after restarting the SSH addon.

## Installation
* Install the ebus hardware adapter
* Install the required addons as listed above
* Checkout this repo into /config (HA config folder)
* Checkout the ebusd config from https://github.com/cluk1/ebusd-configuration into the ebusd addon config folder (`/addon_configs/2ad9b828_ebusd/ebusd/csvs`); the files from [ebusd/](ebusd) are copied there by `deploy.sh`
* Configure the ebusd addon to use the csv files from the checkout (see addon config above).
* Start the ebusd addon, check if it scans the bus (ebusctl info) and submits messages to mqtt (listen to ebusd/#)

## Custom sensors, templates and inputs
* Flat temperatures are read from fritz!Dect thermometers, a sensor for the minimal and average flat temp is defined in [packages/heating/heating_groups.yaml](packages/heating/heating_groups.yaml)
* Several config parameters for the heating and hotwater automation are stored in input elements defined in [packages/heating/heating_input_number.yaml](packages/heating/heating_input_number.yaml) and [packages/hotwater/hotwater_input_number.yaml](packages/hotwater/hotwater_input_number.yaml).

## Homeassistant Heating and Hotwater Device Config
* Heating and Hotwater devices should be created by the ebus daemon through Mqtt discovery. You should find them in Settings | Devices | MQTT. The Homeassistant Heating (climate) and Hotwater devices are defined in [packages/heating/heating_device.yaml](packages/heating/heating_device.yaml) and [packages/hotwater/hotwater_device.yaml](packages/hotwater/hotwater_device.yaml), they provide current temperatures, setpoint temperatures, heating and hotwater mode setting. Using the devices in a dashboard enables you to switch heating or hotwater to off, time controlled or manual mode. It also lets you define the temperature setpoint for both. 
<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2024/05/ha-heating-device.png" width="200">

## Heating Automations
* [Automatic heating power control](packages/heating/heating_automation_power_auto.yaml): Triggered by changes in average flat temperatures to reduce heating power when flat temperatures rise due to solar gains.
* [Automatic heating power off](packages/heating/heating_automation_power_off.yaml): Triggered by heating overruns from the heat pump. If the flats are clearly too warm (heating demand below -1.4 K), it is mild outside (> 5 °C) and daytime, the heating is turned off and the conditions are checked again every 30 minutes. As soon as one condition no longer holds, heating is turned on again, unless it was switched off globally meanwhile. After a HA restart a recovery automation turns the heating back on, since the waiting loop does not survive a restart.

## Hotwater Automations
* [Hotwater Mode](packages/hotwater/hotwater_automation_mode.yaml): Sets hotwater setpoint, charge hysteresis, max charge time and HMU hotwater mode depending on the season. If heating is globally off, summer settings are used. Otherwise the outside temperature band from [sensor.hotwater_outside_band](packages/hotwater/hotwater_sensors.yaml) (icy < -5 °C, winter -5…5 °C, transition_low 5 °C…summer limit, transition_high above the summer limit) selects the settings. The band only changes when the temperature crosses a limit by more than 0.5 K, and a new band has to be stable for 30 minutes, so an outside temperature oscillating around a limit does not toggle the settings. Higher setpoints and hysteresis in warm weather reduce heatpump on / off cycles.
* [Legionella Prevention](packages/hotwater/hotwater_automation_legio.yaml): Try to run a legionella prevention program everyday, but only if the last successful run is at least 4 weeks ago, the outside temp is high enough and the sun is shining(for the heatpump to work efficiently) and the flats heating demand is low. Then increase the hotwater setpoint to 65 °C and start the circulation pump. Wait for 120 mins for the heatpump to reach at least 60°C in the hotwater storage.

## Energy Management
* Combining the pv system with the heating system allows various ways to do intelligent [energy management](EMS.md) using HA automations.
