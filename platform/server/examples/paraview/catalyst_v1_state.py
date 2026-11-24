"""Write native serial VTI from the live Catalyst 1 data description."""

import os
from pathlib import Path

from vtkmodules.vtkIOXML import vtkXMLImageDataWriter


def RequestDataDescription(description):
    if description.GetTimeStep() % 2 == 0:
        data = description.GetInputDescriptionByName('heat')
        data.AllFieldsOn()
        data.GenerateMeshOn()


def DoCoProcessing(description):
    data = description.GetInputDescriptionByName('heat')
    writer = vtkXMLImageDataWriter()
    writer.SetInputData(data.GetGrid())
    target = Path(os.environ['VIBEFLOW_CATALYST_OUTPUT'])/f'temperature_{description.GetTimeStep():06d}.vti'
    writer.SetFileName(str(target))
    if not writer.Write():
        raise RuntimeError(f'Could not write {target.name}')
