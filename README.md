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
* Details about the [Heating system](Heating.md)

## Wallbox
* OpenWB Pro+

## General Information
* All important configuration is done in yaml files, not the ui editor to be able to store the config in git.
* Private information (like flat names or notification devices) are stored in secrets.yaml and not part of this repo. A [secrets.yaml.example](secrets.yaml.example) is included which can be easily adapted to your needs.
