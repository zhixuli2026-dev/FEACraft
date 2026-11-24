"""Check the official heat_conduction_a.fds device output against the slab solution."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


def eigenvalue(index, biot):
    lo = index * math.pi
    hi = (index + 0.5) * math.pi
    for _ in range(55):
        mid = (lo + hi) / 2
        if mid * math.tan(mid) < biot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def analytic_temperature(position, time, roots):
    length = 0.10
    thermal_diffusivity = 1e-6
    dimensionless_time = thermal_diffusivity * time / length**2
    terms = []
    for root in roots:
        coefficient = 4 * math.sin(root) / (2 * root + math.sin(2 * root))
        terms.append(coefficient * math.cos(root * position / length) *
                     math.exp(-root**2 * dimensionless_time))
    return 120 - 100 * math.fsum(terms)


def verify(device_csv):
    with device_csv.open(encoding="ascii", newline="") as handle:
        rows = csv.reader(handle)
        assert next(rows) == ["s", *(["C"] * 6)]
        labels = next(rows)
        assert labels == ["Time", "Back", "8 cm", "6 cm", "4 cm", "2 cm", "Front"]
        samples = [tuple(map(float, row)) for row in rows]
    assert len(samples) == 1001
    assert all(sample[0] == 2 * index for index, sample in enumerate(samples))
    assert samples[0] == (0, 20, 20, 20, 20, 20, 20)
    assert all(math.isfinite(value) and 19.999 <= value <= 120.001
               for sample in samples for value in sample[1:])
    final_time, *native = samples[-1]
    roots = [eigenvalue(mode, biot=100) for mode in range(100)]
    expected = [analytic_temperature(index * 0.02, final_time, roots) for index in range(6)]
    error = max(abs(value - target) for value, target in zip(native, expected, strict=True))
    assert error < 0.5 and all(a < b for a, b in zip(native, native[1:]))
    return {"samples": len(samples), "elapsed_physical_time_s": final_time,
            "depth_columns": labels[1:], "native_final_c": native, "analytic_final_c": expected,
            "max_final_abs_error_c": error}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device_csv", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.device_csv)
    with args.device_csv.open("rb") as handle:
        result["device_csv_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
    args.receipt.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
