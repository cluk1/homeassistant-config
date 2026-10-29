"""Tests für die Heizleistungsregelung und ihre Kopplung an die PV-Überheizung (Backlog E6).

Ausführen im Repo-Root (config/):  python3 -m unittest discover -s tests -v
Benötigt PyYAML und Jinja2.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from ha_automation import HA, load_automations
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyYAML/Jinja2 fehlen: {exc}")

POWER_AUTO = load_automations("heating/heating_automation_power_auto.yaml")["heating_power_auto"]
SURPLUS_ON = load_automations("photovoltaik/photovoltaik_automation_surplus_heating_on.yaml")["photovoltaik_surplus_heating_on"]
SURPLUS_OFF = load_automations("photovoltaik/photovoltaik_automation_surplus_heating_off.yaml")["photovoltaik_surplus_heating_off"]
GLOBAL = load_automations("heating/heating_automation_global_onoff.yaml")
POWER_OFF = load_automations("heating/heating_automation_power_off.yaml")["heating_power_off"]
FLAG = "input_boolean.heating_overheating_active"


def actions(automation, kind=None):
    """alle Aktionen (auch in if/then und repeat) als Liste von dicts"""
    out = []

    def walk(steps):
        for step in steps or []:
            if "action" in step:
                out.append(step)
            walk(step.get("then"))
            walk(step.get("else"))
            walk((step.get("repeat") or {}).get("sequence"))
    walk(automation["actions"])
    return [a for a in out if kind is None or a["action"] == kind]


def targets(step):
    t = (step.get("target") or {}).get("entity_id")
    return t if isinstance(t, list) else [t]


def power_auto(demand, current=19.0, setpoint=20.0, heating="on", flag="off",
               offset=0.0, tmin=18.0, tmax=22.0):
    ha = HA(states={
        "input_boolean.heating_on_off": heating, FLAG: flag,
        "sensor.flats_heating_demand": demand,
        "input_number.flat_temperature_setpoint": setpoint,
        "input_number.heating_power_auto_offset_temp": offset,
        "input_number.heating_power_auto_min_temp": tmin,
        "input_number.heating_power_auto_max_temp": tmax,
    }, attrs={"climate.ctlv2_heating_vaillant": {"temperature": current}})
    calls = ha.run(POWER_AUTO)
    if calls is None:
        return None
    sets = [d["temperature"] for a, d in calls if a == "climate.set_temperature"]
    return sets[0] if sets else "unverändert"


class PowerAutoCalculation(unittest.TestCase):
    def test_follows_heating_demand(self):
        """1 K Heizbedarf hebt den Raumsollwert um 1 K"""
        self.assertEqual(power_auto(1.0), 21.0)
        self.assertEqual(power_auto(-1.0, current=20.0), 19.0)

    def test_upper_limit(self):
        """Raumsollwert höchstens Max (22 °C)"""
        self.assertEqual(power_auto(2.5), 22.0)

    def test_lower_limit(self):
        """R2: Raumsollwert mindestens Min (18 °C), auch bei 3 K Übertemperatur"""
        self.assertEqual(power_auto(-3.0), 18.0)
        self.assertEqual(power_auto(-3.0, tmin=17.0), 17.0)

    def test_rounded_to_half_degree(self):
        """R4: gerundet auf 0,5 °C (Schrittweite der Vaillant)"""
        self.assertEqual(power_auto(-1.3, current=20.0), 18.5)
        self.assertEqual(power_auto(-1.2, current=20.0), 19.0)
        self.assertEqual(power_auto(0.3, current=19.0), 20.5)

    def test_no_write_if_unchanged(self):
        """R4/R5: gleicher Wert wie der gesetzte Tages-Sollwert → nichts schreiben, nichts melden"""
        self.assertEqual(power_auto(-1.1, current=19.0), "unverändert")
        ha = HA(states={"input_boolean.heating_on_off": "on", FLAG: "off", "sensor.flats_heating_demand": -1.0,
                        "input_number.flat_temperature_setpoint": 20, "input_number.heating_power_auto_offset_temp": 0,
                        "input_number.heating_power_auto_min_temp": 18, "input_number.heating_power_auto_max_temp": 22},
                attrs={"climate.ctlv2_heating_vaillant": {"temperature": 19.0}})
        self.assertEqual(ha.run(POWER_AUTO), [])

    def test_offset(self):
        """Offset verschiebt den Raumsollwert"""
        self.assertEqual(power_auto(0.0, offset=1.0), 21.0)


class PowerAutoConditions(unittest.TestCase):
    def test_paused_during_overheating(self):
        """R1: während der PV-Überheizung keine Regelung"""
        self.assertIsNone(power_auto(-2.0, flag="on"))

    def test_heating_off(self):
        """Heizung global aus: keine Regelung"""
        self.assertIsNone(power_auto(-2.0, heating="off"))

    def test_missing_demand(self):
        """R6: ohne gültigen Heizbedarf keine Regelung (statt Template-Fehler)"""
        self.assertIsNone(power_auto("unavailable"))
        self.assertIsNone(power_auto("unknown"))

    def test_climate_setpoint_unknown(self):
        """Raumsollwert der Vaillant unbekannt (z. B. nach Neustart): nichts schreiben"""
        self.assertIsNone(power_auto(-1.0, current=None))

    def test_triggers(self):
        """R3: Mittelwert 45 min stabil, alle 30 min und sofort nach Ende der Überheizung"""
        kinds = [(t["trigger"], t.get("entity_id"), t.get("to"), t.get("minutes")) for t in POWER_AUTO["triggers"]]
        self.assertIn(("state", "sensor.flats_avg_temperature", None, None), kinds)
        self.assertIn(("time_pattern", None, None, "/30"), kinds)
        self.assertIn(("state", FLAG, "off", None), kinds)


class PowerOffThreshold(unittest.TestCase):
    """Heizung aus bei Übertemperatur: Schwelle hängt davon ab, ob die PV-Überheizung läuft"""

    TOO_WARM = [c for c in POWER_OFF["conditions"] if c["condition"] == "template"][0]

    def too_warm(self, demand, overheating):
        ha = HA(states={"sensor.flats_heating_demand": demand, FLAG: "on" if overheating else "off"})
        return ha.condition(self.TOO_WARM, {})

    def test_without_overheating(self):
        """ohne Überheizung erst bei Heizbedarf < -1,4 K"""
        self.assertFalse(self.too_warm(-1.4, overheating=False))
        self.assertTrue(self.too_warm(-1.5, overheating=False))
        self.assertFalse(self.too_warm(0.0, overheating=False))

    def test_during_overheating(self):
        """bei laufender Überheizung schon ab Heizbedarf <= 0 (Wohnungen haben 22 °C erreicht)"""
        self.assertTrue(self.too_warm(0.0, overheating=True))
        self.assertTrue(self.too_warm(-0.5, overheating=True))
        self.assertFalse(self.too_warm(0.1, overheating=True))

    def test_missing_demand(self):
        """ohne gültigen Heizbedarf nie ausschalten"""
        self.assertFalse(self.too_warm("unavailable", overheating=True))
        self.assertFalse(self.too_warm("unknown", overheating=False))

    def test_waiting_loop_uses_same_condition(self):
        """die 30-min-Warteschleife prüft dieselbe Bedingung (YAML-Anker)"""
        loop = [s for s in POWER_OFF["actions"] if "repeat" in s][0]["repeat"]["while"]
        self.assertIn(self.TOO_WARM, loop)


class OverheatingCoupling(unittest.TestCase):
    """R1: die Überheizung steuert die Regelung nur noch über den Merker"""

    def test_no_automation_switching(self):
        for a in (SURPLUS_ON, SURPLUS_OFF):
            self.assertEqual(actions(a, "automation.turn_off"), [], a["id"])
            self.assertEqual(actions(a, "automation.trigger"), [], a["id"])

    def test_on_sets_flag_and_ramp_stops_when_flag_off(self):
        self.assertTrue(any(FLAG in targets(s) for s in actions(SURPLUS_ON, "input_boolean.turn_on")))
        until = [s for s in SURPLUS_ON["actions"] if "repeat" in s][0]["repeat"]["until"][0]["conditions"]
        self.assertIn({"condition": "state", "entity_id": FLAG, "state": "off"}, until)

    def test_off_resets_without_heating_condition(self):
        """Überheizung endet auch bei global ausgeschalteter Heizung"""
        ha = HA(states={"input_boolean.heating_on_off": "off", FLAG: "on",
                        "input_number.flat_temperature_setpoint": 22})
        calls = ha.run(SURPLUS_OFF)
        self.assertIsNotNone(calls, "Bedingungen verhindern das Ende der Überheizung")
        self.assertIn(("input_number.set_value", {"value": 20}), calls)
        self.assertIn(("input_boolean.turn_off", {}), calls)

    def test_off_cleans_up_old_stuck_setpoint(self):
        """Altlast: Sollwert 22 ohne Merker wird ebenfalls zurückgesetzt"""
        ha = HA(states={FLAG: "off", "input_number.flat_temperature_setpoint": 22})
        self.assertIsNotNone(ha.run(SURPLUS_OFF))

    def test_global_on_off_end_overheating(self):
        for aid in ("heating_global_off", "heating_global_on"):
            self.assertTrue(any(FLAG in targets(s) for s in actions(GLOBAL[aid], "input_boolean.turn_off")), aid)
        self.assertTrue(any("automation.heating_power_auto" in targets(s)
                            for s in actions(GLOBAL["heating_global_on"], "automation.turn_on")))


if __name__ == "__main__":
    unittest.main()
