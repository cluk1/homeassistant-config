"""Test harness: runs pyscript files outside Home Assistant.

- Stubs the pyscript globals (decorators, log, state, event, task, hass, pyscript.config)
- pyscript awaits pyscript-function calls automatically; here everything runs
  synchronously instead ("await " is stripped, executor jobs run inline)
- Replaces InfluxDBClient by an in-memory fake that understands the InfluxQL
  subset the scripts use (WHERE meter/time, ORDER BY time, LIMIT)
- Lets the test control "now", the sensor states and what happens on task.sleep
"""
import re
import sys
import types
from datetime import datetime as _dt, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Die Skripte importieren influxdb; das echte Paket wird hier nicht gebraucht
_influx_mod = types.ModuleType("influxdb")
_influx_mod.InfluxDBClient = None
sys.modules.setdefault("influxdb", _influx_mod)


class FakeInflux:
    def __init__(self):
        self.points = {}  # (measurement, meter, time) -> value
        self.fail_writes = 0  # Anzahl der nächsten Schreibversuche, die scheitern
        self.queries = 0

    def write_points(self, points):
        if self.fail_writes:
            self.fail_writes -= 1
            raise ConnectionError("influx down (simulated)")
        for p in points:
            self.points[(p["measurement"], p["tags"]["meter"], p["time"])] = p["fields"]["value"]
        return True

    def query(self, q):
        self.queries += 1
        measurement = re.search(r'FROM\s+"meter"\."autogen"\."(\w+)"', q).group(1)
        meter = re.search(r"meter\"?\s*=\s*'(\w+)'", q).group(1)
        conds = re.findall(r"time\s*(<=|>=|<|>)\s*'([^']+)'", q)
        ops = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
               "<": lambda a, b: a < b, ">": lambda a, b: a > b}
        rows = [(t, v) for (ms, mt, t), v in self.points.items()
                if ms == measurement and mt == meter and all(ops[o](t, r) for o, r in conds)]
        rows.sort(reverse=bool(re.search(r"ORDER BY time DESC", q, re.I)))
        lim = re.search(r"LIMIT\s+(\d+)", q, re.I)
        if lim:
            rows = rows[: int(lim.group(1))]
        return types.SimpleNamespace(get_points=lambda: [{"time": t, "value": v} for t, v in rows])

    def series(self, measurement, meter):
        return {t: v for (ms, mt, t), v in sorted(self.points.items()) if ms == measurement and mt == meter}


class Env:
    def __init__(self, tz="Europe/Berlin"):
        self.influx = FakeInflux()
        self.states = {}
        self.units = {}
        self.now = _dt(2026, 1, 1, tzinfo=timezone.utc)
        self.tz = tz
        self.events = []
        self.logs = []
        self.sleeps = []
        self.on_sleep = None  # callback(env, seconds)
        self.modules = {}
        self.notifications = []  # persistent_notification.create(...)
        self.config_dir = None   # Verzeichnis für hass.config.path(), vom Test gesetzt

    def _state_get(self, name):
        if name not in self.states:
            raise NameError(f"name '{name}' is not defined")
        return self.states[name]

    def _state_getattr(self, name):
        if name not in self.states:
            raise NameError(f"name '{name}' is not defined")
        return {"unit_of_measurement": self.units.get(name, "kWh")}

    def alerts(self):
        return [kw["message"] for name, kw in self.events if name == "meters_alert"]

    def run(self, value):  # Kompatibilität: alles läuft synchron
        return value

    def load(self, path, config):
        env = self
        src = Path(path).read_text().replace("await ", "")

        class FakeDatetime(_dt):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return _dt.fromtimestamp(env.now.timestamp(), tz)
                return _dt.fromtimestamp(env.now.timestamp(), ZoneInfo(env.tz)).replace(tzinfo=None)

            @classmethod
            def fromtimestamp(cls, t, tz=None):
                if tz is None:  # naive Ortszeit des (simulierten) HA-Containers
                    return _dt.fromtimestamp(t, ZoneInfo(env.tz)).replace(tzinfo=None)
                return _dt.fromtimestamp(t, tz)

            @classmethod
            def utcfromtimestamp(cls, t):
                return _dt.fromtimestamp(t, timezone.utc).replace(tzinfo=None)

            @classmethod
            def strptime(cls, s, f):
                d = _dt.strptime(s, f)
                return cls(*d.timetuple()[:6], tzinfo=d.tzinfo)

            def timestamp(self):
                if self.tzinfo is None:  # naive = Ortszeit des Containers
                    return _dt.timestamp(_dt(*self.timetuple()[:6], tzinfo=ZoneInfo(env.tz)))
                return _dt.timestamp(self)

        def deco(*a, **k):
            if len(a) == 1 and callable(a[0]) and not k:
                return a[0]
            return lambda f: f

        class Log:
            def __getattr__(self, lvl):
                return lambda msg: env.logs.append((lvl, msg))

        def task_sleep(sec):
            env.sleeps.append(sec)
            if env.on_sleep:
                env.on_sleep(env, sec)

        g = {
            "__name__": Path(path).stem,
            "event_trigger": deco, "time_trigger": deco, "state_trigger": deco,
            "service": deco, "pyscript_compile": deco,
            "log": Log(),
            "state": types.SimpleNamespace(get=self._state_get, getattr=self._state_getattr),
            "event": types.SimpleNamespace(fire=lambda name, **kw: env.events.append((name, kw))),
            "hass": types.SimpleNamespace(
                async_add_executor_job=lambda fn, *a: fn(*a),
                config=types.SimpleNamespace(path=lambda *p: str(Path(env.config_dir or ".", *p)))),
            "persistent_notification": types.SimpleNamespace(
                create=lambda **kw: env.notifications.append(kw)),
            "task": types.SimpleNamespace(sleep=task_sleep, unique=lambda name: None),
            "pyscript": types.SimpleNamespace(config={"apps": config}),
        }
        exec(compile(src, str(path), "exec"), g)
        # nach dem Import ersetzen: Zeit und Influx kommen aus dem Test
        g["datetime"] = FakeDatetime
        g["time"] = types.SimpleNamespace(time=lambda: env.now.timestamp())
        g["InfluxDBClient"] = lambda **kw: env.influx
        self.modules[Path(path).stem] = g
        return g
