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
- pyscript.meters_report              Abrechnungsbericht für einen Zeitraum (mit Prüfungen)
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
LOCAL_TZ = ZoneInfo("Europe/Berlin")   # für Datumsangaben in meters_recalculate/meters_report
REPORT_DIR = "reports"            # unter /config, nicht www/ (dort ohne Login abrufbar)
TOLERANCE = 0.001                 # kWh, Toleranz der Bilanzprüfungen im Bericht
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


@pyscript_compile
def compute_report(meter, inc, grid, a, b, now, meters, consumers, grid_meter, unallocated,
                   interval, prices, tolerance):
    """Abrechnungsbericht für alle Intervalle mit a < Zeitstempel <= b.

    meter: {zähler: {epoch: Stand}} inkl. des letzten Stands <= a (Basis)
    inc, grid: {zähler: {epoch: kWh}} für den Zeitraum
    prices: {"grid": ct, "heatpump": ct, "pv": ct} oder None-Werte
    Rückgabe: dict mit rows (je Verbraucher), totals, checks [(status, text)], coverage
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def kwh(x):
        return f"{x:.3f}".replace(".", ",") + " kWh"

    def when(epoch):
        return datetime.fromtimestamp(epoch, ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")

    complete = set(meter[grid_meter])
    for m in meters:
        complete &= set(meter[m])
    complete = sorted(complete)
    base = max([t for t in complete if t <= a], default=None)
    in_range = [t for t in complete if a < t <= b]
    seq = ([base] if base is not None else []) + in_range

    # Zählerdifferenzen: Summe der positiven Schritte, Rücksprünge separat
    delta, resets = {m: 0.0 for m in meters}, []
    for p, c in zip(seq, seq[1:]):
        for m in meters:
            d = meter[m][c] - meter[m][p]
            if d < 0:
                resets.append((c, m, meter[m][p], meter[m][c]))
            else:
                delta[m] += d

    def total(series, m):
        return sum([v for t, v in series.get(m, {}).items() if a < t <= b])

    rows = []
    for m in consumers:
        cons, net = total(inc, m), total(grid, m)
        price_net = prices.get("heatpump") if m == "heatpump" else prices.get("grid")
        row = {"meter": m, "consumption": cons, "grid": net, "pv": cons - net,
               "pv_share": (cons - net) / cons if cons > 0 else None,
               "cost_grid": net * price_net / 100 if price_net is not None else None,
               "cost_pv": (cons - net) * prices["pv"] / 100 if prices.get("pv") is not None else None}
        costs = [x for x in (row["cost_grid"], row["cost_pv"]) if x is not None]
        row["cost"] = sum(costs) if costs else None
        rows.append(row)
    grid_in = total(inc, grid_meter)
    unalloc = total(grid, unallocated)
    totals = {"consumption": sum([r["consumption"] for r in rows]),
              "grid": sum([r["grid"] for r in rows]), "unallocated": unalloc,
              "grid_meter": grid_in, "grid_meter_delta": delta[grid_meter]}
    totals["pv"] = totals["consumption"] - totals["grid"]
    costs = [r["cost"] for r in rows if r["cost"] is not None]
    totals["cost"] = sum(costs) if costs else None

    # Vollständigkeit (bei laufendem Zeitraum nur bis jetzt)
    end = min(b, now)
    expected = max(0, (end - a) // interval)
    present = len([t for t in in_range if t <= end])
    longest = max([c - p for p, c in zip(seq, seq[1:])], default=interval)
    uncomputed = [t for t in in_range if t not in inc.get(grid_meter, {})]
    coverage = {"expected": expected, "present": present, "longest_gap": longest,
                "uncomputed": uncomputed, "base": base}

    checks = []
    if base is None:
        checks.append(("warn", "Kein Zählerstand vor dem Zeitraum: das erste Intervall fehlt im Bericht"))
    if uncomputed:
        checks.append(("warn", f"{len(uncomputed)} Zeitpunkte ohne Berechnung, erst meters_recalculate ausführen"))
    else:
        checks.append(("ok", "Alle Zeitpunkte berechnet"))
    off = [m for m in meters if abs(total(inc, m) - delta[m]) > tolerance]
    if off:
        checks.append(("warn", "Verbrauch weicht von der Zählerdifferenz ab: " + ", ".join(
            [f"{m} {kwh(total(inc, m))} statt {kwh(delta[m])}" for m in off])))
    else:
        checks.append(("ok", "Verbrauch je Zähler = Zählerdifferenz"))
    grid_all = totals["grid"] + unalloc
    if abs(grid_all - grid_in) > tolerance or abs(grid_in - delta[grid_meter]) > tolerance:
        checks.append(("warn", f"Netzbilanz stimmt nicht: Summe Netzanteile {kwh(grid_all)}, "
                               f"Netzbezug berechnet {kwh(grid_in)}, laut Zähler {kwh(delta[grid_meter])}"))
    else:
        checks.append(("ok", f"Summe Netzanteile = Netzbezug laut Zähler ({kwh(grid_in)})"))
    if present < expected:
        checks.append(("info", f"{expected - present} von {expected} Viertelstunden ohne eigenen "
                               f"Zählerstand, Energie im Folgeintervall enthalten "
                               f"(längste Lücke {longest // 60} min)"))
    else:
        checks.append(("ok", f"Alle {expected} Viertelstunden mit Zählerstand"))
    for t, m, p, c in resets:
        checks.append(("warn", f"Rücksprung {m} am {when(t)}: {kwh(p)} → {kwh(c)}, Intervall mit 0 gezählt"))
    if unalloc > tolerance:
        checks.append(("info", f"{kwh(unalloc)} Netzbezug ohne gemessenen Verbrauch (unallocated)"))
    return {"rows": rows, "totals": totals, "checks": checks, "coverage": coverage,
            "resets": resets}


@pyscript_compile
def format_report(report, title, prices):
    """-> (Markdown für die Benachrichtigung, CSV-Text mit ; und Dezimalkomma)"""
    def num(x, digits=2):
        return "" if x is None else f"{x:.{digits}f}".replace(".", ",")

    def pct(x):
        return "" if x is None else f"{x * 100:.1f}".replace(".", ",")

    with_cost = report["totals"]["cost"] is not None
    head = ["Partei", "Verbrauch kWh", "Netz kWh", "PV kWh", "PV-Anteil %"]
    if with_cost:
        head += ["Kosten Netz €", "Kosten PV €", "Kosten gesamt €"]
    lines = []
    for r in report["rows"]:
        line = [r["meter"], num(r["consumption"]), num(r["grid"]), num(r["pv"]), pct(r["pv_share"])]
        if with_cost:
            line += [num(r["cost_grid"]), num(r["cost_pv"]), num(r["cost"])]
        lines.append(line)
    t = report["totals"]
    share = t["pv"] / t["consumption"] if t["consumption"] > 0 else None
    total_line = ["Summe", num(t["consumption"]), num(t["grid"]), num(t["pv"]), pct(share)]
    if with_cost:
        total_line += ["", "", num(t["cost"])]
    lines.append(total_line)

    symbols = {"ok": "✓", "warn": "⚠", "info": "ℹ"}
    price_text = ", ".join([f"{k} {v} ct/kWh" for k, v in prices.items() if v is not None])
    md = [f"**{title}**", ""]
    md.append("| " + " | ".join(head) + " |")
    md.append("|" + "---|" * len(head))
    md += ["| " + " | ".join(line) + " |" for line in lines]
    md.append("")
    md.append(f"Netzbezug laut Zähler: {num(t['grid_meter'])} kWh, davon ohne Verbraucher "
              f"(unallocated): {num(t['unallocated'], 3)} kWh")
    if price_text:
        md.append(f"Preise: {price_text}")
    md.append("")
    md += [f"{symbols[s]} {text}" for s, text in report["checks"]]

    csv = [f"{title}"]
    if price_text:
        csv.append(f"Preise;{price_text}")
    csv.append(";".join(head))
    csv += [";".join(line) for line in lines]
    csv.append(f"Netzbezug laut Zähler kWh;{num(t['grid_meter'])}")
    csv.append(f"unallocated kWh;{num(t['unallocated'], 3)}")
    csv.append("")
    csv.append("Prüfung;Ergebnis")
    csv += [f"{s};{text}" for s, text in report["checks"]]
    return "\n".join(md), "\n".join(csv) + "\n"


@pyscript_compile
def write_text(path, text):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# pyscript führt normale Funktionen Anweisung für Anweisung im Event-Loop von HA aus.
# Alles, was über viele Datenpunkte läuft, steht deshalb in @pyscript_compile-Funktionen
# und wird per hass.async_add_executor_job in einem eigenen Thread ausgeführt.
@pyscript_compile
def fetch_rows(client, q):
    """InfluxDB-Abfrage ausführen -> [(epoch, value)] (blockierend, im Executor aufrufen)"""
    from datetime import datetime
    rows = []
    for r in client.query(q).get_points():
        t = int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp())
        rows.append((t, float(r["value"])))
    return rows


@pyscript_compile
def build_points(series, prev_ts, prev, meters, consumers, grid_meter, unallocated, epsilon,
                 interval, compute_fn, points_fn):
    """inc/grid-Punkte für alle vollständigen Zeitpunkte in series (im Executor aufrufen).
    series: {zähler: {epoch: Stand}}, prev_ts/prev: Basis davor oder None.
    Rückgabe: (points, stats) mit done, gaps, incomplete, resets, no_base"""
    from datetime import datetime, timezone

    def iso(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stats = {"done": 0, "gaps": 0, "incomplete": [], "resets": [], "no_base": None}
    points = []
    for ts in sorted(set([t for s in series.values() for t in s])):
        cur = {m: series[m][ts] for m in meters if ts in series[m]}
        if len(cur) != len(meters):
            stats["incomplete"].append(iso(ts))
            continue
        if prev is None:
            stats["no_base"] = iso(ts)  # erster Zeitpunkt dient nur als Basis
        else:
            inc, grid, resets = compute_fn(prev, cur, meters, consumers, grid_meter, unallocated, epsilon)
            points.extend(points_fn(iso(ts), inc, grid))
            stats["resets"].extend([(iso(ts), m, prev[m], cur[m]) for m in resets])
            stats["done"] += 1
            if ts - prev_ts != interval:
                stats["gaps"] += 1
        prev_ts, prev = ts, cur
    return points, stats


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


def meter_rows(client, meter, where, order="ASC", limit=None, measurement="meter"):
    q = (f'SELECT "value" FROM {MEAS}."{measurement}" WHERE "meter" = \'{meter}\' AND {where} '
         f"ORDER BY time {order}")
    if limit:
        q += f" LIMIT {limit}"
    return await hass.async_add_executor_job(fetch_rows, client, q)


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
    prev_ts, prev = last_complete(client, a)
    series = {m: dict(meter_rows(client, m, f"time >= '{to_iso(a)}' AND time <= '{to_iso(b)}'"))
              for m in SENSORS}
    points, stats = await hass.async_add_executor_job(
        build_points, series, prev_ts, prev, list(SENSORS), list(CONSUMERS), GRID_METER,
        UNALLOCATED, EPSILON, INTERVAL, compute_interval, interval_points)
    stats["ok"] = True
    for i in range(0, len(points), 5000):
        if not write(client, points[i:i + 5000]):
            stats["ok"] = False
            return stats
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
    Wie bei meters_report gilt start < Zeitstempel <= end + 1 Tag (Zeitstempel = Ende
    des Intervalls). Vorhandene inc/grid-Werte im Zeitraum werden überschrieben."""
    if not start or not end:
        alert("meters_recalculate: start und end angeben, z. B. start: 2025-01-01, end: 2025-12-31")
        return
    try:
        a, b = parse_day(start) + 1, parse_day(end, end=True)
    except ValueError:
        alert(f"meters_recalculate: ungültiges Datum '{start}' / '{end}', Format 2025-01-01")
        return
    client = influx()
    stats = calculate_range(client, a, b)
    msg = f"meters_recalculate {to_iso(a - 1)} – {to_iso(b)}: "
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


# ------------------------------------------------------------------ Abrechnungsbericht
@service
def meters_report(start=None, end=None, grid_price=None, heatpump_price=None, pv_price=None):
    """Abrechnungsbericht je Partei für den Zeitraum start..end (Ortszeit, Ende inklusive).

    Gezählt werden alle Intervalle mit start < Zeitstempel <= end + 1 Tag (Zeitstempel
    = Ende des Intervalls, das Intervall der Silvesternacht gehört zum alten Jahr).
    Preise optional in ct/kWh: grid_price (Netzstrom), heatpump_price (Wärmestrom,
    sonst grid_price), pv_price (interner PV-Preis).
    Ausgabe: persistente Benachrichtigung + CSV unter /config/reports/."""
    if not start or not end:
        alert("meters_report: start und end angeben, z. B. start: 2026-01-01, end: 2026-12-31")
        return
    try:
        prices = {"grid": None if grid_price in (None, "") else float(grid_price),
                  "heatpump": None if heatpump_price in (None, "") else float(heatpump_price),
                  "pv": None if pv_price in (None, "") else float(pv_price)}
    except (TypeError, ValueError):
        alert("meters_report: Preise als Zahl in ct/kWh angeben, z. B. grid_price: 32.5")
        return
    if prices["heatpump"] is None:
        prices["heatpump"] = prices["grid"]
    try:
        a, b = parse_day(start), parse_day(end, end=True)
    except ValueError:
        alert(f"meters_report: ungültiges Datum '{start}' / '{end}', Format 2026-01-01")
        return
    client = influx()

    where = f"time > '{to_iso(a)}' AND time <= '{to_iso(b)}'"
    base_ts, base = last_complete(client, a + 1)
    meter, inc, grid = {}, {}, {}
    for m in SENSORS:
        meter[m] = dict(meter_rows(client, m, where))
        if base_ts is not None:
            meter[m][base_ts] = base[m]
        inc[m] = dict(meter_rows(client, m, where, measurement="inc"))
    for m in list(CONSUMERS) + [UNALLOCATED]:
        grid[m] = dict(meter_rows(client, m, where, measurement="grid"))

    report = await hass.async_add_executor_job(
        compute_report, meter, inc, grid, a, b, quarter(time.time()), list(SENSORS),
        list(CONSUMERS), GRID_METER, UNALLOCATED, INTERVAL, prices, TOLERANCE)
    title = f"Abrechnung {start} bis {end}"
    md, csv = await hass.async_add_executor_job(format_report, report, title, prices)
    created = datetime.now(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
    csv = f"Erstellt;{created}\n" + csv
    # Dateiname nur aus harmlosen Zeichen (start/end sind Benutzereingaben)
    safe = "".join([ch if ch.isalnum() or ch in "-_" else "_" for ch in f"{start}_{end}"])
    path = hass.config.path(REPORT_DIR, f"abrechnung_{safe}.csv")
    await hass.async_add_executor_job(write_text, path, csv)
    persistent_notification.create(title=title, message=md + f"\n\nCSV: {path}",
                                   notification_id=f"meters_report_{start}_{end}")
    warnings = len([c for c in report["checks"] if c[0] == "warn"])
    alert(f"{title} erstellt ({path}), " +
          ("alle Prüfungen ok" if not warnings else f"{warnings} Warnung(en), siehe Benachrichtigung"))
