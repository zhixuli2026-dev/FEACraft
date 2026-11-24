"""Pass a changing heat field in memory to the Catalyst 1 pipeline."""

import os

from paraview.modules.vtkPVCatalyst import vtkCPDataDescription, vtkCPProcessor
from paraview.modules.vtkPVPythonCatalyst import vtkCPPythonScriptPipeline
from vtkmodules.vtkCommonCore import vtkDoubleArray
from vtkmodules.vtkCommonDataModel import vtkImageData


def run():
    processor = vtkCPProcessor()
    if not processor.Initialize():
        raise RuntimeError('Could not initialize Catalyst 1')
    try:
        pipeline = vtkCPPythonScriptPipeline()
        if not pipeline.Initialize(os.environ['VIBEFLOW_CATALYST_STATE']) or not processor.AddPipeline(pipeline):
            raise RuntimeError('Could not register Catalyst 1 state')
        description = vtkCPDataDescription()
        description.AddInput('heat')
        mesh = vtkImageData()
        mesh.SetDimensions(3, 3, 1)
        field = vtkDoubleArray()
        field.SetName('T')
        field.SetNumberOfValues(9)
        field.Fill(300.0)
        mesh.GetPointData().AddArray(field)
        for step in range(5):
            description.SetTimeData(step*.1, step)
            field.SetValue(4,300.0+100.0*.4**step)
            field.Modified()
            mesh.Modified()
            if processor.RequestDataDescription(description):
                description.GetInputDescriptionByName('heat').SetGrid(mesh)
                if not processor.CoProcess(description):
                    raise RuntimeError(f'Catalyst 1 failed at step {step}')
            print(f'Catalyst 1 step={step} center_K={field.GetValue(4):.6f}',flush=True)
    finally:
        if not processor.Finalize():
            raise RuntimeError('Could not finalize Catalyst 1')


if __name__ == '__main__':
    run()
