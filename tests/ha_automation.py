"""Minimal evaluation of Home Assistant automations from the package YAML files.

Supports what the tested automations use: state/numeric_state/template conditions,
`variables` (rendered in order and converted to native types like HA does) and the
actions up to the first service call. Templates are rendered with Jinja2 and the HA
helpers states(), state_attr(), is_state(), has_value() and the float filter.

Needs PyYAML and Jinja2 (pip install pyyaml jinja2).
"""
import ast
from pathlib import Path

import jinja2
import yaml

PACKAGES = Path(__file__).parent.parent / "packages"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!secret", lambda loader, node: f"secret:{loader.construct_scalar(node)}")


def load_automations(relpath):
    """{id: automation} from a package file (relative to packages/)"""
    data = yaml.load((PACKAGES / relpath).read_text(), Loader=_Loader)
    return {a["id"]: a for a in data["automation"]}


class HA:
    """States and attributes for one test, plus the template environment"""

    def __init__(self, states=None, attrs=None):
        self.states = dict(states or {})
        self.attrs = dict(attrs or {})
        env = jinja2.Environment()
        env.filters["float"] = self._float
        env.globals.update(states=self._states, state_attr=self._state_attr,
                           is_state=lambda e, s: self._states(e) == s, has_value=self._has_value,
                           min=min, max=max)
        self.env = env

    _NO_DEFAULT = object()

    @staticmethod
    def _float(value, default=_NO_DEFAULT):
        try:
            return float(value)
        except (TypeError, ValueError):
            if default is HA._NO_DEFAULT:
                raise ValueError(f"float got invalid input '{value}'")
            return default

    def _states(self, entity):
        return str(self.states.get(entity, "unknown"))

    def _state_attr(self, entity, attr):
        return self.attrs.get(entity, {}).get(attr)

    def _has_value(self, entity):
        return self._states(entity) not in ("unknown", "unavailable")

    def render(self, template, variables=None):
        out = self.env.from_string(str(template)).render(**(variables or {})).strip()
        try:  # HA wandelt Ergebnisse in native Typen um
            return ast.literal_eval(out)
        except (ValueError, SyntaxError):
            return out

    @staticmethod
    def as_boolean(value):
        """wie HA result_as_boolean: nur true/yes/on/enable bzw. Zahlen ≠ 0 gelten als wahr"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in ("true", "yes", "on", "enable", "1")

    def condition(self, cond, variables):
        kind = cond["condition"]
        if kind == "state":
            want = cond["state"] if isinstance(cond["state"], list) else [cond["state"]]
            return self._states(cond["entity_id"]) in [str(w) for w in want]
        if kind == "numeric_state":
            value = self._float(self._states(cond["entity_id"]), float("nan"))
            ok = True
            if "above" in cond:
                ok &= value > float(cond["above"])
            if "below" in cond:
                ok &= value < float(cond["below"])
            return ok
        if kind == "template":
            return self.as_boolean(self.render(cond["value_template"], variables))
        if kind == "or":
            return any(self.condition(c, variables) for c in cond["conditions"])
        if kind == "and":
            return all(self.condition(c, variables) for c in cond["conditions"])
        if kind == "not":
            return not any(self.condition(c, variables) for c in cond["conditions"])
        raise NotImplementedError(kind)

    def run(self, automation):
        """Bedingungen prüfen und Aktionen bis zum Ende ausführen.
        Rückgabe: Liste der aufgerufenen Dienste [(action, data)], None wenn Bedingungen nicht erfüllt"""
        variables = {}
        for cond in automation.get("conditions", []):
            if cond.get("enabled", True) and not self.condition(cond, variables):
                return None
        calls = []
        for step in automation.get("actions", []):
            if "variables" in step:
                for key, tpl in step["variables"].items():
                    variables[key] = self.render(tpl, variables)
            elif "delay" in step:
                continue
            elif "condition" in step:
                if not self.condition(step, variables):
                    return calls
            elif "action" in step:
                data = {k: self.render(v, variables) for k, v in (step.get("data") or {}).items()}
                calls.append((step["action"], data))
        return calls
