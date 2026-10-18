"""Zählerstände für die interne Abrechnung (Selbstversorgungsgemeinschaft).

Alle 15 Minuten (UTC-Viertelstunde) werden die Zählerstände aller Verbraucher und
des Netzbezugs in die InfluxDB-Datenbank "meter" geschrieben. Daraus wird je
Intervall der Verbrauch ("inc") und der Netzanteil ("grid") je Verbraucher berechnet:

    inc(m)  = Stand(m, t) - Stand(m, vorheriger vollständiger Zeitpunkt)
    grid(m) = inc(m) / Σ inc(Verbraucher) * inc(gridbuy)

Grundsätze:
- Alles oder nichts pro Zeitpunkt: Fehlt ein Zählerstand, wird für diesen Zeitpunkt
  nichts geschrieben. Das nächste Intervall reicht dann bis zum letzten vollständigen
  Zeitpunkt zurück, es geht keine Energie verloren.
- Fehlt inc/grid für frühere Zeitpunkte (z. B. HA-Neustart direkt nach dem Schreiben),
  rechnet die nächste Berechnung diese automatisch nach (bis zu 7 Tage zurück).
- Springt ein Zähler zurück (Reset, Tausch), zählt er in diesem Intervall 0, der neue
  Stand wird Basis, und es gibt eine Benachrichtigung.
- Ist kein Verbrauch gemessen, aber Netzbezug vorhanden, wird dieser als
  grid(unallocated) geschrieben, damit die Summe aller grid-Werte dem Netzbezug entspricht.
- Probleme werden als Event "meters_alert" gemeldet (Benachrichtigung per Automation),
  Dauerstörungen höchstens stündlich.

Services:
- pyscript.meters_persist             Zählerstände jetzt schreiben (sonst automatisch)
- pyscript.meters_calculate_incs      inc/grid bis zu einem Zeitpunkt (UTC) berechnen
- pyscript.meters_recalculate         inc/grid für einen Zeitraum neu berechnen
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time

from influxdb import InfluxDBClient

CONSUMERS = {
    "flat1": "flat1_consumption",
    "flat2": "flat2_consumption",
    "flat3": "flat3_consumption",
    "flat4": "flat4_consumption",
    "comm": "comm_consumption",
    "wallbox_cp1": "wallbox_cp1_energy",
    "wallbox_cp2": "wallbox_cp2_energy",
    "heatpump": "heatpump_consumption",
}
GRID_METER = "gridbuy"
SENSORS = dict(CONSUMERS, **{GRID_METER: "gridbuyenergy"})
UNALLOCATED = "unallocated"

INTERVAL = 900                    # Sekunden
UNIT_FACTOR = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
READ_ATTEMPTS = 3
READ_RETRY_DELAY = 20             # Sekunden
WRITE_ATTEMPTS = 3
WRITE_RETRY_DELAY = 10            # Sekunden
EPSILON = 1e-6                    # kWh, kleinere Rücksprünge gelten als Rundungsrauschen
AUTO_CATCHUP = 7 * 86400          # so weit rechnet die Live-Berechnung fehlende Intervalle nach
ALERT_REPEAT = 3600               # gleiche Störung höchstens stündlich melden
LOCAL_TZ = ZoneInfo("Europe/Berlin")   # nur für Datumsangaben in meters_recalculate
MEAS = '"meter"."autogen"'

_last_alert = {}                  # Störungsschlüssel -> Zeitpunkt der letzten Meldung


# ------------------------------------------------------------------ reine Funktionen
# Bewusst ohne Zugriff auf Modul-Globale: alle Konstanten kommen als Parameter.
@pyscript_compile
def compute_interval(prev, cur, meters, consumers, grid_meter, unallocated, epsilon):
    """Berechnet inc und grid für ein Intervall.

    prev, cur: {meter: Stand in kWh} für alle Zähler in meters
    Rückgabe: (inc, grid, resets)
      inc:    {meter: kWh} für alle Zähler inkl. grid_meter
      grid:   {meter: kWh} für alle Verbraucher und unallocated
      resets: Liste der Zähler, die zurückgesprungen sind (inc = 0)
    """
    inc, resets = {}, []
    for meter in meters:
        delta = cur[meter] - prev[meter]
        if delta < -epsilon:
            resets.append(meter)
            delta = 0.0
        inc[meter] = max(delta, 0.0)
    total = sum([inc[m] for m in consumers])
    grid_in = inc[grid_meter]
    grid = {}
    for meter in consumers:
        grid[meter] = inc[meter] / total * grid_in if total > 0 else 0.0
    grid[unallocated] = 0.0 if total > 0 else grid_in
    return inc, grid, resets


@pyscript_compile
def interval_points(ts_iso, inc, grid):
    points = []
    for measurement, values in (("inc", inc), ("grid", grid)):
        for meter, value in values.items():
            points.append({"measurement": measurement, "tags": {"meter": meter},
                           "time": ts_iso, "fields": {"value": round(value, 6)}})
    return points


def compute(prev, cur):
    return compute_interval(prev, cur, list(SENSORS), list(CONSUMERS), GRID_METER,
                            UNALLOCATED, EPSILON)


def to_iso(epoch):
    """Epoch-Sekunden -> InfluxDB-Zeitstempel in UTC, z. B. 2026-10-18T14:15:00Z"""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):
    """Zeitstempel -> Epoch-Sekunden. Ohne Zeitzonenangabe gilt UTC."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def quarter(epoch):
    """Beginn der UTC-Viertelstunde, in der epoch liegt"""
    return int(epoch // INTERVAL) * INTERVAL


def parse_day(text, end=False):
    """'2025-01-01' (Ortszeit) oder ISO-Zeitpunkt (ohne Zone: Ortszeit) -> Epoch-Sekunden.
    Bei end=True und reinem Datum: Beginn des Folgetags (Tag inklusive)."""
    if len(text) == 10:
        day = datetime.strptime(text, "%Y-%m-%d")
        if end:
            day = day + timedelta(days=1)
        return int(day.replace(tzinfo=LOCAL_TZ).timestamp())
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return int(dt.timestamp())


# ------------------------------------------------------------------ Hilfsfunktionen
def alert(message, key=None):
    """Meldung an Admin. Mit key wird dieselbe Störung höchstens stündlich gemeldet."""
    now = time.time()
    if key is not None and now - _last_alert.get(key, 0) < ALERT_REPEAT:
        log.warning(f"meters: {message} (Meldung unterdrückt, schon gemeldet)")
        return
    if key is not None:
        _last_alert[key] = now
    log.warning(f"meters: {message}")
    event.fire("meters_alert", message=message)


def clear_alert(key):
    """Störung behoben: beim nächsten Auftreten wieder sofort melden"""
    _last_alert.pop(key, None)


def influx():
    cfg = pyscript.config["apps"]["meters_persist"][0]
    return InfluxDBClient(host=cfg["dbhost"], port=8086, username=cfg["dbuser"],
                          password=cfg["dbpassword"], database="meter", timeout=10)


def query(client, q):
    rs = await hass.async_add_executor_job(client.query, q)
    return list(rs.get_points())


def meter_rows(client, meter, where, order="ASC", limit=None):
    q = (f'SELECT "value" FROM {MEAS}."meter" WHERE "meter" = \'{meter}\' AND {where} '
         f"ORDER BY time {order}")
    if limit:
        q += f" LIMIT {limit}"
    return [(from_iso(r["time"]), float(r["value"])) for r in query(client, q)]


def last_complete(client, before):
    """Letzter Zeitpunkt < before, an dem alle Zähler einen Stand haben: (epoch, {meter: kWh})"""
    rows = {m: dict(meter_rows(client, m, f"time < '{to_iso(before)}'", "DESC", 8)) for m in SENSORS}
    common = set(rows[GRID_METER])
    for m in SENSORS:
        common &= set(rows[m])
    if not common:
        return None, None
    ts = max(common)
    return ts, {m: rows[m][ts] for m in SENSORS}


def write(client, points):
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            await hass.async_add_executor_job(client.write_points, points)
            return True
        except Exception as exc:
            log.warning(f"meters: Schreiben fehlgeschlagen (Versuch {attempt}): {exc}")
            if attempt < WRITE_ATTEMPTS:
                task.sleep(WRITE_RETRY_DELAY)
    return False


def read_meters():
    """{meter: kWh} und Liste der Probleme"""
    values, problems = {}, []
    for meter, sensor in SENSORS.items():
        entity = f"sensor.{sensor}"
        try:
            raw = state.get(entity)
            unit = state.getattr(entity).get("unit_of_measurement")
        except Exception:
            problems.append(f"{entity} existiert nicht")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{entity} = {raw}")
            continue
        if unit not in UNIT_FACTOR:
            problems.append(f"{entity}: unbekannte Einheit {unit}")
            continue
        values[meter] = value * UNIT_FACTOR[unit]
    return values, problems


def calculate_range(client, a, b):
    """Berechnet inc/grid für alle vollständigen Zeitpunkte t mit a <= t <= b neu,
    jeweils gegenüber dem vorherigen vollständigen Zeitpunkt (auch vor a).
    Rückgabe: dict mit Zählern und Hinweisen; bei Schreibfehler ok=False."""
    stats = {"ok": True, "done": 0, "gaps": 0, "incomplete": [], "resets": [], "no_base": None}
    prev_ts, prev = last_complete(client, a)
    series = {m: dict(meter_rows(client, m, f"time >= '{to_iso(a)}' AND time <= '{to_iso(b)}'"))
              for m in SENSORS}
    times = sorted(set([t for s in series.values() for t in s]))
    points = []
    for ts in times:
        cur = {m: series[m][ts] for m in SENSORS if ts in series[m]}
        if len(cur) != len(SENSORS):
            stats["incomplete"].append(to_iso(ts))
            continue
        if prev is None:
            stats["no_base"] = to_iso(ts)  # erster Zeitpunkt dient nur als Basis
        else:
            inc, grid, resets = compute(prev, cur)
            points.extend(interval_points(to_iso(ts), inc, grid))
            stats["resets"].extend([(to_iso(ts), m, prev[m], cur[m]) for m in resets])
            stats["done"] += 1
            if ts - prev_ts != INTERVAL:
                stats["gaps"] += 1
        prev_ts, prev = ts, cur
        if len(points) >= 5000:
            if not write(client, points):
                stats["ok"] = False
                return stats
            points = []
    if points and not write(client, points):
        stats["ok"] = False
    return stats


def report_resets(stats):
    if stats["resets"]:
        alert("Zähler zurückgesprungen, Verbrauch im Intervall mit 0 gezählt: " +
              ", ".join([f"{t} {m} {p:.3f} → {c:.3f} kWh" for t, m, p, c in stats["resets"][:10]]))


# ------------------------------------------------------------------ Zählerstände schreiben
@service
def meters_persist():
    """Liest alle Zählerstände und schreibt sie für die aktuelle UTC-Viertelstunde."""
    ts = quarter(time.time())
    ts_iso = to_iso(ts)
    client = influx()
    prev_ts, prev = last_complete(client, ts)

    for attempt in range(1, READ_ATTEMPTS + 1):
        values, problems = read_meters()
        if not problems:
            break
        log.info(f"meters: Versuch {attempt}, nicht lesbar: {', '.join(problems)}")
        if attempt < READ_ATTEMPTS:
            task.sleep(READ_RETRY_DELAY)
    if problems:
        alert(f"{ts_iso}: Zählerstände nicht geschrieben, nicht lesbar: {', '.join(problems)}",
              key="read")
        return
    clear_alert("read")

    # Eingefrorene Werte (Modbus liefert alte Werte): nichts schreiben, das nächste
    # Intervall umfasst dann die Energie beider Viertelstunden.
    if prev is not None and all([prev[m] == values[m] for m in SENSORS]):
        msg = f"{ts_iso}: alle Zähler unverändert seit {to_iso(prev_ts)}, nichts geschrieben"
        if ts - prev_ts >= 3600:
            alert(msg, key="frozen")
        else:
            log.info(f"meters: {msg}")
        return
    clear_alert("frozen")

    points = [{"measurement": "meter", "tags": {"meter": m}, "time": ts_iso,
               "fields": {"value": v}} for m, v in values.items()]
    if not write(client, points):
        alert(f"{ts_iso}: Zählerstände konnten nicht in InfluxDB geschrieben werden", key="write")
        return
    clear_alert("write")
    log.info(f"meters: Zählerstände {ts_iso} geschrieben")
    event.fire("meters_updated", timestring=ts_iso)


@time_trigger("startup")
def meters_loop():
    """Taktgeber: läuft jeweils kurz nach jeder UTC-Viertelstunde.
    Ersetzt einen Cron-Trigger, weil der nach Ortszeit läuft und bei der
    Umstellung auf Winterzeit die doppelte Stunde überspringt."""
    task.unique("meters_loop")
    while True:
        now = time.time()
        task.sleep(quarter(now) + INTERVAL + 2 - now)
        try:
            meters_persist()
            clear_alert("loop")
        except Exception as exc:
            alert(f"Fehler beim Schreiben der Zählerstände: {exc}", key="loop")


# ------------------------------------------------------------------ inc/grid berechnen
@event_trigger("meters_updated")
@service
def meters_calculate_incs(timestring=None):
    """Berechnet inc und grid für den Zeitpunkt timestring (UTC, z. B. 2026-10-18T14:15:00Z)
    und für alle Zeitpunkte davor, denen inc/grid noch fehlt (bis 7 Tage zurück)."""
    if not timestring:
        alert("meters_calculate_incs: timestring angeben, z. B. 2026-10-18T14:15:00Z")
        return
    ts = from_iso(timestring)
    ts_iso = to_iso(ts)
    client = influx()
    last_inc = meter_rows_inc(client, ts)
    start = ts if last_inc is None else max(last_inc + 1, ts - AUTO_CATCHUP)
    stats = calculate_range(client, start, ts)
    if not stats["ok"]:
        alert(f"{ts_iso}: inc/grid konnten nicht in InfluxDB geschrieben werden", key="calc_write")
        return
    clear_alert("calc_write")
    report_resets(stats)
    if stats["incomplete"]:
        alert(f"{ts_iso}: unvollständige Zählerstände übersprungen: {', '.join(stats['incomplete'][:5])}")
    if stats["done"] > 1:
        log.info(f"meters: {stats['done']} Intervalle bis {ts_iso} berechnet (fehlende nachgeholt)")
    elif stats["gaps"]:
        log.info(f"meters: {ts_iso} umfasst mehr als 15 min (Lücke davor)")
    log.info(f"meters: inc/grid bis {ts_iso} geschrieben")


def meter_rows_inc(client, before):
    """Zeitpunkt des letzten inc-Punkts vor before (epoch) oder None"""
    rows = query(client, f'SELECT "value" FROM {MEAS}."inc" WHERE "meter" = \'{GRID_METER}\' '
                         f"AND time < '{to_iso(before)}' ORDER BY time DESC LIMIT 1")
    return from_iso(rows[0]["time"]) if rows else None


@service
def meters_recalculate(start=None, end=None):
    """Berechnet inc und grid für alle Zählerstände im Zeitraum neu.

    start, end: Datum in Ortszeit ('2025-01-01', Ende inklusive) oder ISO-Zeitpunkt.
    Vorhandene inc/grid-Werte im Zeitraum werden überschrieben."""
    if not start or not end:
        alert("meters_recalculate: start und end angeben, z. B. start: 2025-01-01, end: 2025-12-31")
        return
    a, b = parse_day(start), parse_day(end, end=True) - 1
    client = influx()
    stats = calculate_range(client, a, b)
    msg = f"meters_recalculate {to_iso(a)} – {to_iso(b + 1)}: "
    if not stats["ok"]:
        alert(msg + "Schreiben in InfluxDB fehlgeschlagen, abgebrochen")
        return
    msg += f"{stats['done']} Intervalle neu berechnet, davon {stats['gaps']} nach Lücken"
    if stats["no_base"]:
        msg += f"; kein Zählerstand vor dem Zeitraum, {stats['no_base']} nur als Basis genutzt"
    if stats["incomplete"]:
        msg += (f"; {len(stats['incomplete'])} unvollständige Zeitpunkte übersprungen "
                f"({', '.join(stats['incomplete'][:5])})")
    alert(msg)
    report_resets(stats)
