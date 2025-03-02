# Homeassistant Config as Energy Management System combining the photovoltaik and heating systems

## General Information
* To increase the rentability of the photovoltaik system it is helpful to increase the internal usage of pv energy instead of supplying this to the grid.
* Using data from the pv system, the pv forecast and the controlling the heating device, it is possible to use as much of the pv energy internally as possible.

## Prerequisites
* [Photovoltaik Forecast Integration](https://github.com/BJReplay/ha-solcast-solar)

## Energy Measuerment
Using various home assistant automations the following algorithm is implemented:
1. When the internal battery storage SoC exceeds 20% an [overheating of the whole building](packages/photovoltaik/photovoltaik_automation_surplus_heating_on.yaml) is started to increase the flat temp from 20° to 22°.
2. If the battery SoC exceeds 30% the OpenWB wallbox is allowed to charge the connected cars.
3. If the battery SoC still exceeds 70% [hot water overheating](packages/photovoltaik/photovoltaik_automation_surplus_hotwater.yaml) is started to increase the HW storage temp from 50° to 65°.
 
