# Homeassistant Config as Energy Management System combining the photovoltaik and heating systems

## General Information
* To increase the rentability of the photovoltaik system it is helpful to increase the internal usage of pv energy instead of supplying this to the grid.
* Using data from the pv system, the pv forecast and controlling the heat pump, as much pv energy as possible is used internally. The battery state of charge (SoC, `sensor.esssoc`) and the Solcast forecast for the next 2 hours (`sensor.solcast_pv_forecast_forecast_next_x_hours`) are the main inputs.

## Prerequisites
* [Solcast PV Forecast Integration](https://github.com/BJReplay/ha-solcast-solar), updated 5 times a day between sunrise and sunset by [photovoltaik_automation_forecast.yaml](packages/photovoltaik/photovoltaik_automation_forecast.yaml) to stay within the API limit.

## Surplus usage
The stages build on each other by battery SoC:

| Stage | Starts when | Stops when | What happens |
|---|---|---|---|
| [Overheating of the building](packages/photovoltaik/photovoltaik_automation_surplus_heating_on.yaml) | SoC > 20 %, forecast > 5.5 kWh, until 3 h before sunset, outside > 0 °C, heating on | [SoC < 10 %, forecast < 3 kWh, 1 h before sunset or outside < 2 °C](packages/photovoltaik/photovoltaik_automation_surplus_heating_off.yaml) | Flat setpoint 20 → 22 °C, the heating setpoint is raised in steps of 0.5 K every 7 minutes (until 21.8 °C or SoC < 18 %); the automatic heating power control is paused meanwhile, the FRITZ! radiator thermostats switch to an overheating template |
| Wallbox | SoC > 30 % | – | Configured in the OpenWB, not in HA; the OpenWB PV charging battery mode can be switched via `select.pv_charging_bat_mode` ([wallbox_sensors.yaml](packages/wallbox/wallbox_sensors.yaml)) |
| [Hotwater overheating](packages/photovoltaik/photovoltaik_automation_surplus_hotwater.yaml) | SoC > 70 %, forecast > 5 kWh, until 3 h before sunset, cooling off, building already overheating or flats warm enough, storage < 45 °C or compressor running | 150 minutes or storage temperature reached | Hotwater setpoint 57.5 °C, previous settings are restored afterwards |

The [legionella prevention](HEATING.md#hotwater-automations) also prefers sunny days with a charged battery.

## Notifications
* Faults and situations that need action (low COP, cold flats, aborted legionella or surplus charge, meter/billing problems) are pushed to the admin.
* Status messages (overheating on/off, heating setpoints, hotwater mode, cooling) go through [script.notify_info](packages/system/notify.yaml): they are written to the logbook and only pushed while `input_boolean.notify_debug` is on.

## Planned
Planned improvements (e.g. switching the FEMS grid-friendly battery charging depending on forecasts, a better PV forecast for fog situations at Lake Constance) are tracked outside this repo.
