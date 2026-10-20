# Homeassistant Config for Photovoltaik, Vaillant Heatpump and OpenWB Wallbox in a Multi Party House

<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2025/03/overview.png" width="500">

## Photovoltaik System
* 22 kWp oriented East - West
* Fenecon Home 30
* The PV-System is using the measuerment & operating concept (Meßkonzept) of 'Selbstversorgungsgemeinschaft' to supply 5 flats in 2 buildings, one heatpump and one wallbox with pv energy.
* Details about the [Photovoltaik system and grid usage measurement](PV.md)

## Heating System
* Vaillant aroTHERM plus VWL 125
* The heating system is installed in one of the two buildings as a heating supply for 3 flats.
* Details about the [Heating system](HEATING.md)

## Energy Management
* Combining the heatpump control and the pv system allows for intelligent usage of photovoltaic surplus using HA automations.
* Details about the [Energy Management System](EMS.md)

## Wallbox
* OpenWB Pro+

## General Information
* All important configuration is done in yaml files, not the ui editor to be able to store the config in git.
* Private information (like flat names or notification devices) are stored in secrets.yaml and not part of this repo. A [secrets.yaml.example](secrets.yaml.example) is included which can be easily adapted to your needs.

## Naming conventions
* **Entity IDs** of own entities (templates, groups, modbus, MQTT devices, helpers) are English snake_case: `<area>_<object>[_<quantity>]`, e.g. `heatpump_cop`, `flats_avg_temperature`, `wallbox_cp1_power`. Areas: `flat1`–`flat4`, `comm` (common electricity), `heatpump`, `heating`, `hotwater`, `cooling`, `wallbox`, `pv`, `grid`.
* **Display names** (`name:`) are German. Every own entity has a `unique_id`, so changing a display name never changes the entity ID that automations and dashboards use.
* **Automations**: `id` like an entity ID (`heating_power_auto`), English alias; one file per topic named `<area>_automation_<topic>.yaml`.
* Entities created by integrations (ebusd, FRITZ!, Shelly, zigbee2mqtt, openWB, myPyllant) keep their names; they are maintained in the respective system.
