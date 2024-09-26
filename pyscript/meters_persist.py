from influxdb import InfluxDBClient
from time import strftime
from datetime import datetime
from zoneinfo import ZoneInfo

@time_trigger("cron(*/15 * * * *)")
@service
def meters_persist(action=None, id=None):
    """Update all meter timeseries"""
    log.info(f"Reading all meters")
    app_config = pyscript.config['apps']['meters_persist'][0]
    client = InfluxDBClient(host=app_config['dbhost'], port=8086, username="homeassistant", password=app_config['dbpassword'], database="meter")
    # current time with seconds set to 0
    timestring = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%dT%H:%M:00 %z")
    # convert to timestamp
    timestamp = datetime.strptime(timestring, "%Y-%m-%dT%H:%M:00 %z").timestamp()
    # convert to utc
    utimestring = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%dT%H:%M:00Z")
    
    log.debug(f"TS {utimestring}")
    points = []
    sensors = {
        'flat1': 'flat1_consumption',
        'flat2': 'flat2_consumption',
        'flat3': 'flat3_consumption',
        'flat4': 'flat4_consumption',
        'comm': 'comm_consumption',
        'wallbox_cp1': 'wallbox_cp1_energy',
        'wallbox_cp2': 'wallbox_cp2_energy',
        'heatpump': 'heatpump_consumption',
        'gridbuy': 'gridbuyenergy'
    }
    for meter in sensors.keys():
        sensor = sensors[meter]
        value = float(state.get(f"sensor.{sensor}"))
        log.debug(f"Meter {meter}: {value}")
        points.append({
            "measurement" : "meter",
            "tags" : {
                "meter" : f"{meter}"
            },
            "time" : utimestring,
            "fields" : {
                "value" : value
            }
        })
    log.debug(f"Writing data to influxdb")
    await hass.async_add_executor_job(client.write_points, points)
    event.fire("meters_updated", timestring=utimestring)
    log.info(f"Finished")
