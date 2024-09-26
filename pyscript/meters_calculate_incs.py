from influxdb import InfluxDBClient
from time import strftime
from datetime import datetime
from zoneinfo import ZoneInfo

@event_trigger("meters_updated")
@service
def meters_calculate_incs(timestring=None):
    """Update all increment timeseries"""
    
    app_config = pyscript.config['apps']['meters_calculate_incs'][0]
    client = InfluxDBClient(host=app_config['dbhost'], port=8086, username="homeassistant", password=app_config['dbpassword'], database="meter")

    meters = [
        'flat1',
        'flat2',
        'flat3',
        'flat4',
        'comm',
        'wallbox_cp1',
        'wallbox_cp2',
        'heatpump',
        'gridbuy'
    ]

    last_epoch = datetime.strptime(timestring, "%Y-%m-%dT%H:%M:00Z").timestamp() - 900;
    last_timestring = datetime.fromtimestamp(last_epoch).strftime("%Y-%m-%dT%H:%M:00Z")
    log.info(f"Calculating all increments: {timestring} compared to {last_timestring}")

    inc = {}
    sum = 0
    meter_points = []
    points = []
    for meter in meters:
        query = f'SELECT "value" FROM "meter"."autogen"."meter" WHERE meter = \'{meter}\' and time <= \'{timestring}\' and time >= \'{last_timestring}\' ORDER BY time DESC;'
        log.debug(f"Calculating all increments: {query}")
        rs = await hass.async_add_executor_job(client.query, query)
        meter_points = list(rs.get_points())
        log.debug(f"Points: {meter_points}")
        inc[meter] = float(meter_points[0]['value']) - float(meter_points[1]['value'])
        log.debug(f"Inc: {meter}:{inc[meter]}")
        if meter != 'gridbuy':
            sum += inc[meter]
        points.append({
            "measurement" : "inc",
            "tags" : {
                "meter" : f"{meter}"
            },
            "time" : timestring,
            "fields" : {
                "value" : inc[meter]
            }
        })

    grid = {}
    for meter in meters:
        if meter != 'gridbuy':
            grid[meter] = inc[meter] / sum * inc['gridbuy']
            log.debug(f"Net: {meter}:{grid[meter]}")
            points.append({
                "measurement" : "grid",
                "tags" : {
                    "meter" : f"{meter}"
                },
                "time" : timestring,
                "fields" : {
                    "value" : grid[meter]
                }
            })

    log.info(f"Writing data to influxdb: {points}")
    await hass.async_add_executor_job(client.write_points, points)
