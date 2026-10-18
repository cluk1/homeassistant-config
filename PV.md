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

### Writing the meter data and grid usage to influxdb
This is done by the pyscript [meters.py](pyscript/meters.py):
* Every 15 minutes (aligned to the UTC quarter hour, independent of daylight saving time) all meter readings are written to the influxdb measurement `meter`. A timestamp is written only if all meters could be read (with retries); frozen modbus readings are skipped.
* For every timestamp the consumption since the previous complete timestamp (`inc`) and the share of the grid usage (`grid`) are calculated for all flats, heatpump and wallbox: `grid = inc / Σ inc(consumers) × inc(gridbuy)`. After a gap the interval simply becomes longer, so no energy is lost. Missing calculations of the last 7 days are caught up automatically.
* If a meter jumps back (reset, replacement), its consumption in that interval counts as 0 and the new reading becomes the base. Grid usage without any metered consumption is stored as `grid` with `meter = unallocated`.
* Problems are fired as event `meters_alert` and sent as notification by [meters_alert.yaml](packages/power/meters_alert.yaml).
* The behaviour is covered by unit tests in [tests/test_meters.py](tests/test_meters.py) (run `python3 -m unittest discover -s tests -v`), which run the unchanged script against a simulated InfluxDB and check the energy balance for gaps, resets, unit changes, daylight saving time and lost calculations.
* Service `pyscript.meters_recalculate` (parameters `start`, `end`, e.g. `2025-01-01`, `2025-12-31` in local time) recalculates `inc` and `grid` for a period, e.g. to close gaps before the yearly billing.

## Energy Management
* Combining the pv system with the heating system allows various ways to do intelligent energy management using HA automations.
