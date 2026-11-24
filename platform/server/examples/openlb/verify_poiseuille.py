"""Verify the published OpenLB Poiseuille series and its final velocity profile."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from vtkmodules.vtkIOXML import vtkXMLImageDataReader


def verify_series(directory):
    series = directory / "poiseuille2d.pvd"
    frames = ET.parse(series).findall("./Collection/DataSet")
    steps = [int(frame.attrib["timestep"]) for frame in frames]
    if not steps or steps[0] != 0 or steps != sorted(set(steps)):
        raise ValueError("Invalid OpenLB timeline")

    checked = {series}
    final_blocks = []
    for frame in frames:
        index = (directory / frame.attrib["file"]).resolve()
        checked.add(index)
        index.relative_to(directory)
        blocks = ET.parse(index).findall(".//DataSet")
        if not blocks:
            raise ValueError(f"Empty VTK multiblock file {index}")
        final_blocks = [(index.parent / block.attrib["file"]).resolve() for block in blocks]
        for block in final_blocks:
            block.relative_to(directory)
            if not block.is_file() or not block.stat().st_size:
                raise ValueError(f"Missing VTK image data {block}")
            checked.add(block)

    if len(final_blocks) != 1:
        raise ValueError("Expected the sequential reference case to have one final grid")
    return steps, final_blocks[0], checked


def verify_final(path):
    reader = vtkXMLImageDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    velocity = grid.GetPointData().GetArray("physVelocity")
    if velocity is None or velocity.GetNumberOfComponents() != 2:
        raise ValueError("The final grid has no 2-D velocity field")
    points = []
    for index in range(grid.GetNumberOfPoints()):
        x, y, _ = grid.GetPoint(index)
        if abs(x - 1) < 1e-8 and 0 <= y <= 1:
            axial, transverse = velocity.GetTuple2(index)
            if not math.isfinite(axial) or not math.isfinite(transverse):
                raise ValueError("The velocity field contains a nonfinite value")
            points.append((y, axial, transverse))

    if len(points) != 51 or [round(y, 2) for y, _, _ in points] != [i / 50 for i in range(51)]:
        raise ValueError("The centerline does not cover the expected 51 points")
    axial_error = max(abs(axial - 4 * y * (1 - y)) for y, axial, _ in points)
    transverse = max(abs(value) for _, _, value in points)
    if axial_error >= .003 or transverse >= 1e-4:
        raise ValueError(f"Poiseuille profile exceeded tolerance: {axial_error}, {transverse}")
    return {"grid_points": grid.GetNumberOfPoints(), "profile_points": len(points),
            "axial_max_abs_error_m_s": axial_error, "transverse_max_speed_m_s": transverse,
            "center_speed_m_s": points[25][1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="OpenLB vtkData directory")
    directory = parser.parse_args().directory.resolve()
    steps, final, checked = verify_series(directory)
    hashes = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(checked)}
    print(json.dumps({"passed": True, "frames": len(steps), "final_lattice_step": steps[-1],
                      **verify_final(final), "sha256": hashes}, indent=2))


if __name__ == "__main__":
    main()
