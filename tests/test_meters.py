"""Tests für pyscript/meters.py (interne Abrechnung).

Ausführen im Repo-Root (config/):  python3 -m unittest discover -s tests -v

Jeder Test simuliert einen Tag mit 15-min-Takten. Die Simulation kennt den
tatsächlichen Verbrauch und Netzbezug und prüft die Energiebilanz:
  - Σ inc je Verbraucher       == tatsächlicher Verbrauch
  - Σ grid (inkl. unallocated) == tatsächlicher Netzbezug
  - keine negativen inc/grid-Werte
Das Skript läuft dabei unverändert im Testgerüst pyscript_harness.py, InfluxDB
und Home Assistant sind nachgebildet.
"""
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from pyscript_harness import Env  # noqa: E402

SCRIPT = HERE.parent / "pyscript" / "meters.py"
CFG = {"meters_persist": [{"dbhost": "x", "dbpassword": "x"}]}
CONSUMERS = ["flat1", "flat2", "flat3", "flat4", "comm", "wallbox_cp1", "wallbox_cp2", "heatpump"]
SENSORS = {
    "flat1": "flat1_consumption", "flat2": "flat2_consumption", "flat3": "flat3_consumption",
    "flat4": "flat4_consumption", "comm": "comm_consumption", "wallbox_cp1": "wallbox_cp1_energy",
    "wallbox_cp2": "wallbox_cp2_energy", "heatpump": "heatpump_consumption", "gridbuy": "gridbuyenergy",
}
DAY = datetime(2026, 1, 13, tzinfo=timezone.utc)


class Sim:
    """Simulierte Zähler. true = tatsächlicher Verbrauch seit dem ersten Zählerstand."""

    def __init__(self, env, seed=1, grid_ratio=0.4, unmetered=0.0):
        self.env, self.rnd = env, random.Random(seed)
        self.grid_ratio, self.unmetered = grid_ratio, unmetered
        self.values = {m: 1000.0 + i * 100 for i, m in enumerate(SENSORS)}
        self.true = {m: 0.0 for m in SENSORS}
        self.offset = {m: 0.0 for m in SENSORS}  # Anzeige = values + offset (Rücksprung)
        self.scale = {m: 1.0 for m in SENSORS}   # 1000 = Anzeige in Wh
        self.frozen = False

    def advance(self):
        used = 0.0
        for m in CONSUMERS:
            inc = round(self.rnd.uniform(0.01, 0.3), 3)
            self.values[m] += inc
            self.true[m] += inc
            used += inc
        grid = round(used * self.grid_ratio + self.unmetered, 3)
        self.values["gridbuy"] += grid
        self.true["gridbuy"] += grid

    def publish(self):
        if self.frozen:
            return
        for m, sensor in SENSORS.items():
            entity = f"sensor.{sensor}"
            self.env.states[entity] = f"{(self.values[m] + self.offset[m]) * self.scale[m]:.3f}"
            self.env.units[entity] = "Wh" if self.scale[m] == 1000 else "kWh"


# ---------------------------------------------------------------- Störungen
def unavailable(meter, recover_on_retry=False):
    def hook(env, sim):
        env.states[f"sensor.{SENSORS[meter]}"] = "unavailable"
        if recover_on_retry:
            def on_sleep(e, sec):
                sim.publish()
                e.on_sleep = None
            env.on_sleep = on_sleep
    return hook


def influx_fails(n):
    def hook(env, sim):
        env.influx.fail_writes = n
    return hook


def freeze(env, sim):
    sim.frozen = True


def unfreeze(env, sim):
    sim.frozen = False
    sim.publish()


def jump_to(meter, value):
    """Zähler springt dauerhaft auf value (z. B. Reset der openWB)"""
    def hook(env, sim):
        sim.offset[meter] = value - sim.values[meter]
        sim.publish()
    return hook


def report_in_wh(meter):
    def hook(env, sim):
        sim.scale[meter] = 1000
        sim.publish()
    return hook


def skip(env, sim):
    return "skip"      # Takt fällt aus (z. B. HA-Neustart)


def lose_calc(env, sim):
    return "nocalc"    # Zählerstände geschrieben, Berechnung danach verloren


class MetersTestCase(unittest.TestCase):
    def run_day(self, start=DAY, ticks=96, hooks=None, tz="Europe/Berlin", calc=True, **simkw):
        env = Env(tz=tz)
        mod = env.load(SCRIPT, CFG)
        sim = Sim(env, **simkw)
        for i in range(ticks + 1):
            env.now = start + timedelta(minutes=15 * i, seconds=2)
            if i > 0:
                sim.advance()
            sim.publish()
            if i == 0:
                sim.true = {m: 0.0 for m in SENSORS}
            action = hooks[i](env, sim) if hooks and i in hooks else None
            if action == "skip":
                continue
            env.events = [e for e in env.events if e[0] != "meters_updated"]
            mod["meters_persist"]()
            if action == "nocalc" or not calc:
                continue
            for _, kw in [e for e in env.events if e[0] == "meters_updated"]:
                mod["meters_calculate_incs"](**kw)
        return env, mod, sim

    def inc_sum(self, env, meter):
        return sum(env.influx.series("inc", meter).values())

    def assertBalanced(self, env, sim, inc_correction=None):
        """Energiebilanz stimmt; inc_correction: bewusst akzeptierte Abweichung je Zähler"""
        for m in CONSUMERS:
            want = sim.true[m] + (inc_correction or {}).get(m, 0.0)
            self.assertAlmostEqual(self.inc_sum(env, m), want, places=3, msg=f"inc {m}")
        grid = sum(sum(env.influx.series("grid", m).values()) for m in CONSUMERS + ["unallocated"])
        self.assertAlmostEqual(grid, sim.true["gridbuy"], places=3, msg="Σ grid ≠ Netzbezug")
        negative = [k for k, v in env.influx.points.items() if k[0] in ("inc", "grid") and v < -1e-9]
        self.assertEqual(negative, [], "negative inc/grid-Werte")

    def assertAlert(self, env, text, count=None):
        alerts = [a for a in env.alerts() if text in a]
        self.assertTrue(alerts, f"keine Meldung mit '{text}', Meldungen: {env.alerts()}")
        if count is not None:
            self.assertEqual(len(alerts), count, f"Anzahl Meldungen '{text}': {alerts}")


class NormalOperation(MetersTestCase):
    def test_normal_day(self):
        """Normaler Tag: Bilanz stimmt, keine Meldungen"""
        env, _, sim = self.run_day()
        self.assertBalanced(env, sim)
        self.assertEqual(env.alerts(), [])

    def test_timestamps_on_utc_quarter_hours(self):
        """Zählerstände liegen exakt auf UTC-Viertelstunden"""
        env, _, _ = self.run_day(ticks=8)
        times = list(env.influx.series("meter", "gridbuy"))
        self.assertEqual(times[:3], ["2026-01-13T00:00:00Z", "2026-01-13T00:15:00Z", "2026-01-13T00:30:00Z"])

    def test_grid_is_distributed_proportionally(self):
        """Netzanteil je Verbraucher = Anteil am Verbrauch × Netzbezug"""
        env, _, _ = self.run_day(ticks=2)
        t = "2026-01-13T00:30:00Z"
        inc = {m: env.influx.series("inc", m)[t] for m in CONSUMERS + ["gridbuy"]}
        total = sum(inc[m] for m in CONSUMERS)
        for m in CONSUMERS:
            self.assertAlmostEqual(env.influx.series("grid", m)[t], inc[m] / total * inc["gridbuy"], places=6)

    def test_unmetered_load_still_proportional(self):
        """Nachts mit ungemessener Last: Netzbezug wird trotzdem vollständig proportional verteilt"""
        env, _, sim = self.run_day(grid_ratio=1.0, unmetered=0.0125)
        self.assertBalanced(env, sim)
        self.assertEqual(sum(env.influx.series("grid", "unallocated").values()), 0.0)


class ReadingProblems(MetersTestCase):
    def test_meter_unavailable_for_one_tick(self):
        """Zähler einen Takt nicht lesbar: Zeitpunkt fehlt, nächstes Intervall umfasst 30 min, Bilanz stimmt"""
        env, _, sim = self.run_day(hooks={40: unavailable("flat3")})
        self.assertBalanced(env, sim)
        self.assertNotIn("2026-01-13T10:00:00Z", env.influx.series("meter", "flat1"))
        self.assertAlert(env, "nicht lesbar: sensor.flat3_consumption = unavailable", count=1)

    def test_meter_recovers_on_retry(self):
        """Zähler kurz nicht lesbar, beim zweiten Versuch wieder da: kein Ausfall, keine Meldung"""
        env, _, sim = self.run_day(hooks={40: unavailable("flat3", recover_on_retry=True)})
        self.assertBalanced(env, sim)
        self.assertIn("2026-01-13T10:00:00Z", env.influx.series("meter", "flat1"))
        self.assertEqual(env.alerts(), [])

    def test_long_outage_alerts_hourly(self):
        """Zähler 3 h nicht lesbar: Bilanz stimmt, Meldung nur einmal pro Stunde"""
        env, _, sim = self.run_day(hooks={i: unavailable("flat3") for i in range(40, 52)})
        self.assertBalanced(env, sim)
        self.assertAlert(env, "nicht lesbar", count=3)

    def test_meter_reports_wh(self):
        """Zähler meldet plötzlich Wh statt kWh: wird umgerechnet, Bilanz stimmt"""
        env, _, sim = self.run_day(hooks={45: report_in_wh("wallbox_cp1")})
        self.assertBalanced(env, sim)

    def test_unknown_unit_is_rejected(self):
        """Unbekannte Einheit: Zeitpunkt wird nicht geschrieben, Meldung"""
        def unit_w(env, sim):
            env.units["sensor.flat2_consumption"] = "W"
        env, _, sim = self.run_day(hooks={30: unit_w})
        self.assertBalanced(env, sim)
        self.assertAlert(env, "unbekannte Einheit W")

    def test_frozen_modbus_values(self):
        """Modbus liefert 45 min dieselben Werte: nichts schreiben, Bilanz stimmt, noch keine Meldung"""
        env, _, sim = self.run_day(hooks={30: freeze, 34: unfreeze})
        self.assertBalanced(env, sim)
        self.assertEqual(env.alerts(), [])

    def test_frozen_modbus_values_long(self):
        """Modbus liefert über 1 h dieselben Werte: Meldung"""
        env, _, sim = self.run_day(hooks={30: freeze, 40: unfreeze})
        self.assertBalanced(env, sim)
        self.assertAlert(env, "alle Zähler unverändert")


class MeterJumps(MetersTestCase):
    def test_reset_counts_zero_and_alerts(self):
        """Zähler springt auf 0 (Reset): Intervall zählt 0, neuer Stand wird Basis, Meldung"""
        env, _, sim = self.run_day(hooks={45: jump_to("wallbox_cp2", 0.0)})
        lost = sim.true["wallbox_cp2"] - self.inc_sum(env, "wallbox_cp2")
        self.assertGreater(lost, 0)
        self.assertLess(lost, 0.31)  # höchstens der Verbrauch eines Intervalls
        self.assertBalanced(env, sim, inc_correction={"wallbox_cp2": -lost})
        self.assertAlert(env, "zurückgesprungen", count=1)
        self.assertAlert(env, "wallbox_cp2")


class StorageAndTiming(MetersTestCase):
    def test_influx_write_fails_once(self):
        """InfluxDB-Schreiben scheitert einmal: zweiter Versuch klappt, keine Meldung"""
        env, _, sim = self.run_day(hooks={50: influx_fails(1)})
        self.assertBalanced(env, sim)
        self.assertEqual(env.alerts(), [])

    def test_influx_write_fails_completely(self):
        """InfluxDB-Schreiben scheitert dreimal: Zeitpunkt fehlt, Bilanz stimmt, Meldung"""
        env, _, sim = self.run_day(hooks={50: influx_fails(3)})
        self.assertBalanced(env, sim)
        self.assertAlert(env, "nicht in InfluxDB geschrieben", count=1)

    def test_missed_tick(self):
        """HA-Neustart, ein Takt fällt aus: Bilanz stimmt"""
        env, _, sim = self.run_day(hooks={60: skip})
        self.assertBalanced(env, sim)

    def test_ha_down_two_hours(self):
        """HA 2 h aus: ein langes Intervall, Bilanz stimmt"""
        env, _, sim = self.run_day(hooks={i: skip for i in range(30, 38)})
        self.assertBalanced(env, sim)

    def test_lost_calculation_is_caught_up(self):
        """Berechnung nach dem Schreiben verloren: nächste Berechnung holt sie nach"""
        env, _, sim = self.run_day(hooks={40: lose_calc})
        self.assertBalanced(env, sim)
        self.assertIn("2026-01-13T10:00:00Z", env.influx.series("inc", "flat1"))

    def test_several_lost_calculations_are_caught_up(self):
        """Drei Berechnungen in Folge verloren: alle werden nachgeholt"""
        env, _, sim = self.run_day(hooks={i: lose_calc for i in (40, 41, 42)})
        self.assertBalanced(env, sim)

    def test_dst_spring(self):
        """Umstellung auf Sommerzeit: keine Lücke, Bilanz stimmt"""
        env, _, sim = self.run_day(start=datetime(2026, 3, 29, tzinfo=timezone.utc))
        self.assertBalanced(env, sim)
        self.assertEqual(len(env.influx.series("inc", "flat1")), 96)

    def test_dst_autumn(self):
        """Umstellung auf Winterzeit: keine Lücke, Bilanz stimmt"""
        env, _, sim = self.run_day(start=datetime(2025, 10, 26, tzinfo=timezone.utc))
        self.assertBalanced(env, sim)
        self.assertEqual(len(env.influx.series("inc", "flat1")), 96)


class Recalculation(MetersTestCase):
    def test_recalculate_repairs_history(self):
        """meters_recalculate: fehlende und falsche inc/grid eines Tages werden korrigiert"""
        start = datetime(2026, 3, 29, tzinfo=timezone.utc)
        env, mod, sim = self.run_day(start=start, hooks={20: unavailable("flat3"),
                                                         45: jump_to("wallbox_cp2", 0.0), 70: skip})
        # Historie beschädigen: Werte löschen und verfälschen, wie es die alte Version tat
        pts = env.influx.points
        for key in [k for k in pts if k[0] in ("inc", "grid") and k[2] < "2026-03-29T06:00:00Z"]:
            del pts[key]
        pts[("inc", "wallbox_cp2", "2026-03-29T11:15:00Z")] = -1606.7
        pts[("grid", "heatpump", "2026-03-29T12:00:00Z")] = 99.0
        env.events.clear()
        mod["meters_recalculate"](start="2026-03-29", end="2026-03-30")
        lost = sim.true["wallbox_cp2"] - self.inc_sum(env, "wallbox_cp2")
        self.assertBalanced(env, sim, inc_correction={"wallbox_cp2": -lost})
        self.assertAlert(env, "Intervalle neu berechnet")
        self.assertAlert(env, "zurückgesprungen")

    def test_recalculate_requires_dates(self):
        """meters_recalculate ohne Zeitraum: nur Meldung, nichts geändert"""
        env, mod, _ = self.run_day(ticks=4)
        before = dict(env.influx.points)
        mod["meters_recalculate"]()
        self.assertEqual(env.influx.points, before)
        self.assertAlert(env, "start und end angeben")


class TimeHelpers(MetersTestCase):
    def setUp(self):
        self.mod = Env().load(SCRIPT, CFG)

    def test_timestamp_without_zone_is_utc(self):
        """Zeitstempel ohne Zone gelten als UTC"""
        f = self.mod["from_iso"]
        self.assertEqual(f("2026-10-18T14:15:00"), f("2026-10-18T14:15:00Z"))
        self.assertEqual(f("2026-10-18T16:15:00+02:00"), f("2026-10-18T14:15:00Z"))

    def test_day_boundaries_local_time(self):
        """Datumsangaben für die Reparatur gelten in Ortszeit, auch über die Zeitumstellung"""
        iso, day = self.mod["to_iso"], self.mod["parse_day"]
        self.assertEqual(iso(day("2026-03-29")), "2026-03-28T23:00:00Z")
        self.assertEqual(iso(day("2026-03-29", end=True)), "2026-03-29T22:00:00Z")
        self.assertEqual(iso(day("2026-10-25", end=True)), "2026-10-25T23:00:00Z")

    def test_quarter(self):
        """Zeitpunkt wird auf den Beginn der UTC-Viertelstunde abgerundet"""
        q, f = self.mod["quarter"], self.mod["from_iso"]
        self.assertEqual(q(f("2026-10-18T14:29:59Z")), f("2026-10-18T14:15:00Z"))


if __name__ == "__main__":
    unittest.main()
