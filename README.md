# Homeassistant Config for Vaillant Heating System

<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2024/05/ha-heatpump.png" width="500">

## Heating Device
Vaillant aroTHERM plus VWL 125

## General Information
All important configuration is done in yaml files, not the ui editor to be able to store the config in git.

# Prerequisites
* ebus Adapter: https://ebusd.eu/
* ebus Daemon running as HA Addon (https://github.com/LukasGrebe/ha-addons) or separate docker container
* mqtt Broker running as HA Addon (https://github.com/home-assistant/addons/tree/master/mosquitto) or separate docker container

# Installation
* Install the ebus adapter
* Install the required addons as listed above
* Share one /config folder among all docker containers (ha, ebusd). This happens automatically with HA green.
* Checkout this repo into /config
* Checkout the ebusd config from https://github.com/cluk1/ebusd-configuration into /config/ebusd/csvs
* Configure the ebusd addon to use the csv files from the checkout
* Start the ebusd Addon, check if it scans the bus and submit messages to mqtt

# Homeassistant Device Config
* Heating and Hotwater devices should be created my Mqtt through discovery. They are defined in packages/heating/heating_device.yaml and packages/hotwater/hotwater_device.yaml
