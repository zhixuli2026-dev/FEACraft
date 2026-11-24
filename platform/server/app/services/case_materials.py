"""Validate caseDict material entries and normalize constant properties to SI."""

import math


ENUMS = {
    "role": {"fluid", "solid", "porous", "conductor", "dielectric", "acousticMedium"},
    "phase": {"gas", "liquid", "solid", "plasma", "mixture"},
    "source": {"user", "paper", "database", "tutorial", "assumption"},
}

# Unit scales are relative to the first (SI) unit in each entry.
UNITS = {
    "density": {"kg/m3": 1, "kg/m^3": 1, "g/cm3": 1000, "g/cm^3": 1000},
    "viscosity": {"Pa*s": 1, "Pa s": 1, "mPa*s": 1e-3, "cP": 1e-3},
    "heatCapacity": {"J/(kg*K)": 1, "J/(kg K)": 1, "kJ/(kg*K)": 1000},
    "molarMass": {"kg/kmol": 1, "g/mol": 1, "kg/mol": 1000},
    "formationEnthalpy": {"J/kg": 1, "kJ/kg": 1000},
    "prandtlNumber": {"1": 1},
    "surfaceTension": {"N/m": 1, "mN/m": 1e-3},
    "thermalConductivity": {"W/(m*K)": 1, "W/(m K)": 1},
    "elasticModulus": {"Pa": 1, "kPa": 1e3, "MPa": 1e6, "GPa": 1e9},
    "poissonRatio": {"1": 1},
    "yieldStress": {"Pa": 1, "kPa": 1e3, "MPa": 1e6, "GPa": 1e9},
    "electricConductivity": {"S/m": 1, "mS/m": 1e-3, "MS/m": 1e6},
    "relativePermittivity": {"1": 1},
    "magneticPermeability": {"H/m": 1, "mH/m": 1e-3, "uH/m": 1e-6},
}


def quantity_si(name: str, value) -> float:
    """Interpret a legacy SI scalar or a declared value and unit."""
    units = UNITS[name]
    scale = 1
    if isinstance(value, dict):
        unit = value.get("unit")
        if not isinstance(unit, str) or unit not in units:
            raise ValueError(f"单位不合法，可选 {', '.join(units)}")
        scale = units[unit]
        value = value.get("value")
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value) * scale
    except (TypeError, ValueError, OverflowError):
        raise ValueError("必须提供有限标量") from None
    if not math.isfinite(number):
        raise ValueError("必须提供有限标量")
    if name == "poissonRatio":
        if not -1 < number < 0.5:
            raise ValueError("泊松比必须大于 -1 且小于 0.5")
    elif name in {"electricConductivity", "viscosity", "surfaceTension"}:
        if number < 0:
            raise ValueError("必须为非负数")
    elif name not in {"relativePermittivity", "magneticPermeability", "formationEnthalpy"} and number <= 0:
        raise ValueError("必须为正数")
    return number
