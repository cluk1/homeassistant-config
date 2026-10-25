# Homeassistant Config for Photovoltaik, Vaillant Heatpump and OpenWB Wallbox in a Multi Party House

<img src="https://energiewende-einfach-machen.net/wp-content/uploads/2025/03/overview.png" width="500">

## Photovoltaik System
* 22 kWp oriented East - West, plus three balcony PV systems (Shelly measured)
* Fenecon Home 30 with 15 kWh battery
* The PV-System is using the measurement & operating concept (Meßkonzept) of 'Selbstversorgungsgemeinschaft' to supply 5 flats in 2 buildings, one heatpump and one wallbox with pv energy. The internal billing is calculated every 15 minutes and stored in InfluxDB.
* Details about the [Photovoltaik system and grid usage measurement](PV.md)

## Heating System
* Vaillant aroTHERM plus VWL 125, controlled via ebusd
* The heating system is installed in one of the two buildings as a heating supply for 3 flats; in summer it also cools the flats.
* Details about the [Heating system](HEATING.md)

## Energy Management
* Combining the heatpump control and the pv system allows for intelligent usage of photovoltaic surplus using HA automations.
* Details about the [Energy Management System](EMS.md)

## Wallbox
* OpenWB Pro+ with two charge points, integrated via MQTT ([packages/wallbox](packages/wallbox))

## General Information
* All important configuration is done in yaml files, not the ui editor to be able to store the config in git. Only a few personal automations (appliances, cars) stay in the UI (`automations.yaml`, not part of this repo).
* Private information (like flat names or notification devices) are stored in secrets.yaml and not part of this repo. A [secrets.yaml.example](secrets.yaml.example) is included which can be easily adapted to your needs.

## Repository structure
* [packages/](packages): one folder per area (`heating`, `hotwater`, `cooling`, `photovoltaik`, `power`, `wallbox`, `system`), loaded with `!include_dir_named`
* [pyscript/meters.py](pyscript/meters.py): meter readings and billing calculation
* [tests/](tests): unit tests for the billing script and the heating power control (`pip install pyyaml jinja2`, then `python3 -m unittest discover -s tests -v`)
* [ebusd/](ebusd): own ebusd files (MQTT integration, read scripts), copied into the ebusd addon config folder on deployment
* [cron.d/](cron.d): cron jobs run by the SSH addon (ebusd reads, InfluxDB backup)
* [examples/](examples): dashboard examples

## Naming conventions
* **Entity IDs** of own entities (templates, groups, modbus, MQTT devices, helpers) are English snake_case: `<area>_<object>[_<quantity>]`, e.g. `heatpump_cop`, `flats_avg_temperature`, `wallbox_cp1_power`. Areas: `flat1`–`flat4`, `comm` (common electricity), `heatpump`, `heating`, `hotwater`, `cooling`, `wallbox`, `pv`, `grid`.
* **Display names** (`name:`) are German. Every own entity has a `unique_id`, so changing a display name never changes the entity ID that automations and dashboards use.
* **Automations**: `id` like an entity ID (`heating_power_auto`), English alias; one file per topic named `<area>_automation_<topic>.yaml`.
* Entities created by integrations (ebusd, FRITZ!, Shelly, zigbee2mqtt, openWB, myPyllant) keep their names; they are maintained in the respective system.
