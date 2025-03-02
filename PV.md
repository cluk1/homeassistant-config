# Homeassistant Config for multi flat pv supply

<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2024/11/Hohenegg-Aufsicht.jpeg" width="250">

## Photovoltaic and Storage Device
Fenecon Home 30, 15 kWh storage

## General Information
* The photovoltaic system has 22 kWp panels installed in Est-West orientation. It is used to supply pv energy to 5 flats in 2 buildings, one heatpump and one wallbox.
* It uses internal Socomec meters to measure the energy usage of all flats, heatpump and wallbox. Those can be read by HA using the modbus protocol.

## Prerequisites
* Modbus
* [Pyscript Addon](https://github.com/custom-components/pyscript)
* [InfluxDB Addon](https://www.home-assistant.io/integrations/influxdb)

## Energy Measuerment
<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2024/03/PV-MFH-Selbstversorgung.png" width="250">

* The operational concept being used is 'Selbstversorgungsgemeinschaft' which corresponds to the measurement concepts 'D1' from VBEW. More information (in german) can be found [here](https://energiewende-einfach-machen.net/2024/06/20/jetzt-wird-es-technisch-messkonzepte-fuer-pv-im-mehrfamilienhaus/)
* This concept implies only one offical meter (Z0) for the whole building. Internal billing between the involved parties is done using the internal Socomec meters (Z1-Z3).
* To make the internal measurement most accurate and also honour the usage percentage of PV and grid usage, we read all meters every 15 minutes and write those data to an influxdb.
* For every 15 minutes we then calculate the grid usage of the whole building and the shares for every flat, heatpump and wallbox.
* This then allows us to run a yearly billing where every party only pays for grid usage.

<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2025/03/Netzbezug-20250323-20250302.png" width="500">

### Reading the internal meters using modbus
* The installed FEMS system has an internal Modbus-TCP Gateway which can be used to access the meters. If you are using different hardware your config will be a little different. The modbus sensors are defined in [fems.yaml](packages/photovoltaik/fems.yaml)

### Wrting the meter data and grid usage to influxdb
This is done by two pyscripts:
* Read all the meaters every 15 mins and write the data to influxdb: [meters_persist.py](pyscript/meters_persist.py)
* Calculate the usages for the last 15 min interval for all flats, heatpump, wallbox and the share of the grid usage: [meters_calculate_incs.py](pyscript/meters_calculate_incs.py)

## Energy Management
* Combining the pv system with the heating system allows various ways to do intelligent energy management using HA automations.
