"""Generate and verify a parameterized plate in the FreeCAD 1.1 Python runtime."""
import json
import math
import os
from pathlib import Path

import FreeCAD as App
import Mesh
import MeshPart
import Part


def main():
    out = Path(os.environ.get("VIBEFLOW_SAMPLE_OUTPUT", "freecad-result")).resolve()
    out.mkdir(exist_ok=False)
    length, width, thickness = 30.0, 20.0, 4.0
    radius = 2.0
    centres = [(6, 5), (24, 5), (6, 15), (24, 15)]
    expected = thickness * (length * width - len(centres) * math.pi * radius**2)

    plate = Part.makeBox(length, width, thickness)
    for x, y in centres:
        tool = Part.makeCylinder(radius, thickness + 2, App.Vector(x, y, -1))
        plate = plate.cut(tool)
    assert plate.isValid() and len(plate.Solids) == 1
    assert math.isclose(plate.Volume, expected, rel_tol=1e-10)

    model = out / "plate.FCStd"
    step = out / "plate.step"
    stl = out / "plate.stl"
    doc = App.newDocument("DrilledPlate")
    try:
        doc.addObject("Part::Feature", "Plate").Shape = plate
        doc.recompute()
        doc.saveAs(str(model))
        plate.exportStep(str(step))
        mesh = MeshPart.meshFromShape(Shape=plate, LinearDeflection=0.03,
                                      AngularDeflection=0.15, Relative=False)
        mesh.write(str(stl))
    finally:
        App.closeDocument(doc.Name)

    restored = App.openDocument(str(model))
    try:
        volume = restored.getObject("Plate").Shape.Volume
    finally:
        App.closeDocument(restored.Name)
    exchanged = Part.read(str(step))
    surface = Mesh.Mesh(str(stl))
    assert exchanged.isValid() and len(exchanged.Solids) == 1
    assert math.isclose(volume, expected, rel_tol=1e-10)
    assert math.isclose(exchanged.Volume, expected, rel_tol=1e-9)
    assert surface.isSolid() and surface.CountFacets > 50
    assert math.isclose(surface.Volume, expected, rel_tol=2e-3)

    receipt = {"freecad": ".".join(App.Version()[:3]), "hole_count": len(centres),
               "box_mm": [length, width, thickness], "analytic_volume_mm3": expected,
               "native_volume_mm3": volume, "step_volume_mm3": exchanged.Volume,
               "stl_volume_mm3": surface.Volume, "stl_facets": surface.CountFacets}
    (out / "plate.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("FREECAD_PLATE_OK", json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
