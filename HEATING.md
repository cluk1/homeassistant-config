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
* The message definitions (CSV files) are generated from the TypeSpec sources in https://github.com/cluk1/ebusd-configuration (fork of the ebusd configuration, own changes are checked in there). The generated variant in use is `1755c15-fixes` in `/addon_configs/2ad9b828_ebusd/ebusd/csvs/`.
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
* Generate the ebusd message definitions (CSV files) and copy them into the ebusd addon config folder (see below); the own files from [ebusd/](ebusd) are copied there by `deploy.sh`
* Configure the ebusd addon to use the generated csv files (`--configpath`, see addon config above).
* Start the ebusd addon, check if it scans the bus (ebusctl info) and submits messages to mqtt (listen to ebusd/#)

## Generating the ebusd CSV files
The CSV files are compiled from the TypeSpec sources with the ebus TypeSpec compiler, running in a docker image:
```
FROM node

RUN echo 'export PATH=$PATH:/node/node_modules/.bin' >> /root/.bashrc

RUN mkdir /node && cd /node && npm install @ebusd/ebus-typespec
WORKDIR /node/src
```
* Build the image: `docker build -t ebusd-typelib-compiler:1 .`
* Compile (in the folder containing the `ebusd-configuration` checkout): `docker run -it --rm -v $(pwd)/ebusd-configuration:/node/src ebusd-typelib-compiler:1 /node/node_modules/.bin/tsp compile ./src`
* Install (from the `ebusd-configuration` folder, the compiler writes to `dst/`): `rsync -rv --delete dst/ hassio@homeassistant:/addon_configs/2ad9b828_ebusd/ebusd/csvs/1755c15-fixes/`
* Restart the ebusd addon afterwards.

## Custom sensors, templates and inputs
* Flat temperatures are read from fritz!Dect thermometers, a sensor for the minimal and average flat temp is defined in [packages/heating/heating_groups.yaml](packages/heating/heating_groups.yaml)
* Several config parameters for the heating automations (flat setpoint and tolerance, limits of the automatic heating power control, heating curve) are stored in input elements defined in [packages/heating/heating_input_number.yaml](packages/heating/heating_input_number.yaml). The global heating switch is `input_boolean.heating_on_off` ([heating_input_boolean.yaml](packages/heating/heating_input_boolean.yaml)).
* [packages/heating/heating_sensors.yaml](packages/heating/heating_sensors.yaml): flats heating demand (setpoint minus tolerance minus average flat temperature), heat pump output power and momentary COP (only while running stably, from 0.8 kW consumption) and humidity alarms per flat.

## Homeassistant Heating and Hotwater Device Config
* Heating and Hotwater devices should be created by the ebus daemon through Mqtt discovery. You should find them in Settings | Devices | MQTT. The Homeassistant Heating (climate) and Hotwater devices are defined in [packages/heating/heating_device.yaml](packages/heating/heating_device.yaml) and [packages/hotwater/hotwater_device.yaml](packages/hotwater/hotwater_device.yaml), they provide current temperatures, setpoint temperatures, heating and hotwater mode setting. Using the devices in a dashboard enables you to switch heating or hotwater to off, time controlled or manual mode. It also lets you define the temperature setpoint for both. 
<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2024/05/ha-heating-device.png" width="200">

## Heating Automations
* [Global heating on/off](packages/heating/heating_automation_global_onoff.yaml): switches the heating circuit with `input_boolean.heating_on_off`; the hotwater mode follows automatically (summer settings while heating is off).
* [Heating curve](packages/heating/heating_automation_heatcurve.yaml) and [flat temperature](packages/heating/heating_automation_publish_flattemp.yaml): publish the heating curve from the input element and the average flat temperature to ebusd/MQTT.
* Monitoring: [COP below 3](packages/heating/heating_automation_cop.yaml), [flats below 19.5 °C](packages/heating/heating_automation_flats_cold.yaml) (push to admin) and [heat pump overrun](packages/heating/heating_automation_overrun.yaml) (logbook).
* [Bath window](packages/heating/heating_automation_bath_window.yaml): remembers the bath thermostat state when the window opens during the heating season, turns it off and restores it when the window closes. The [alert](packages/heating/heating_bath_window.yaml) is shown on the tablet dashboard immediately and pushed after 15 minutes, then every 30 minutes.
* [Automatic heating power control](packages/heating/heating_automation_power_auto.yaml): proportional control of the Vaillant room setpoint (Z1DayTemp): new setpoint = flat setpoint + offset + heating demand, limited to the min/max input values and rounded to 0.5 °C, written only if it differs from the current day setpoint. Runs when the average flat temperature has been stable for 45 minutes, every 30 minutes and right after a PV overheating ends. It pauses while `input_boolean.heating_overheating_active` is on (set by the [PV overheating](EMS.md)); switching the heating globally on or off also ends an overheating. Tests: [tests/test_heating_control.py](tests/test_heating_control.py).
* [Automatic heating power off](packages/heating/heating_automation_power_off.yaml): Triggered by heating overruns from the heat pump. If the flats are clearly too warm (heating demand below -1.4 K), it is mild outside (> 5 °C) and daytime, the heating is turned off and the conditions are checked again every 30 minutes. As soon as one condition no longer holds, heating is turned on again, unless it was switched off globally meanwhile. After a HA restart a recovery automation turns the heating back on, since the waiting loop does not survive a restart.

## Hotwater Automations
* [Hotwater Mode](packages/hotwater/hotwater_automation_mode.yaml): Sets hotwater setpoint, charge hysteresis, max charge time and HMU hotwater mode depending on the season. If heating is globally off, summer settings are used. Otherwise the outside temperature band from [sensor.hotwater_outside_band](packages/hotwater/hotwater_sensors.yaml) (icy < -5 °C, winter -5…5 °C, transition_low 5 °C…summer limit, transition_high above the summer limit) selects the settings. The band only changes when the temperature crosses a limit by more than 0.5 K, and a new band has to be stable for 30 minutes, so an outside temperature oscillating around a limit does not toggle the settings. Higher setpoints and hysteresis in warm weather reduce heatpump on / off cycles.
* [Legionella Prevention](packages/hotwater/hotwater_automation_legio.yaml): Runs daily at 13:00 if the last successful run is more than 21 days ago, cooling is off, it is warmer than 8 °C outside and either warmer than 15 °C or the battery is above 20 % with more than 5 kWh PV forecast for the next 2 hours, and the flats need no heating. Then the hotwater setpoint is raised to 60 °C for up to 150 minutes, afterwards the previous settings are restored. [Last run](packages/hotwater/hotwater_automation_legio_lastrun.yaml) is recorded whenever the storage reaches 59 °C, also by PV surplus.
* [Circulation pump](packages/hotwater/hotwater_script_circulation.yaml): script started from a tablet button; sets hotwater to "heat" for 5 minutes so the circulation pump runs (weekdays 5:30–21:00, weekends 7:00–21:00) and restores the previous hotwater mode afterwards.
* All hotwater changes go through [script.warmwater_settings](scripts.yaml), which can restore the previous settings after a temporary change.

## Cooling
The heat pump also cools the flats via the radiators in summer ([packages/cooling](packages/cooling)).
* [Cooling on/off](packages/cooling/cooling_automation_onoff.yaml): on at 8:32/10:32 if the daily temperature forecast (Swiss Weather, [cooling_sensors.yaml](packages/cooling/cooling_sensors.yaml)) is above 27 °C; off one hour before sunset or when it is below 25 °C outside for 30 minutes (after 16:00). Hotwater is off while cooling, since the heat pump cannot do both.
* [Early hotwater](packages/cooling/cooling_automation_hotwater_early.yaml): at 5:35 a hotwater charge is forced if the storage is below 35 °C, so there is hot water before a cooling day starts.
* [Flow temperature](packages/cooling/cooling_automation_setpoint.yaml): the minimum cooling flow temperature follows the highest dew point in the flats and the house ([cooling_groups.yaml](packages/cooling/cooling_groups.yaml)) to avoid condensation.
* [Thermostats and radiator fans](packages/cooling/cooling_automation_thermostats.yaml): FRITZ! radiator thermostats are opened and the radiator fans switched on as soon as the flow is cool, and closed during hotwater charging or when cooling ends.

## Energy Management
* Combining the pv system with the heating system allows various ways to do intelligent [energy management](EMS.md) using HA automations.
