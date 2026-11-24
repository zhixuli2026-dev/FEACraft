"""Read actual SU2 output, calculate pressure ratio, extract sonic contour and render."""
import json,os
from pathlib import Path
from paraview.simple import (XMLUnstructuredGridReader,Calculator,Contour,CreateView,
                            Show,ColorBy,Render,SaveData,SaveScreenshot,GetParaViewVersion)
from paraview.simple import _DisableFirstRenderCameraReset
_DisableFirstRenderCameraReset()
root=Path(os.environ['VIBEFLOW_CASE_ROOT']);out=root/'D-post';out.mkdir(exist_ok=True)
settings=json.loads((root/'Ref/render.json').read_text())
reader=XMLUnstructuredGridReader(FileName=[str(root/'Ref/naca.vtu')]);reader.UpdatePipeline()
assert reader.GetDataInformation().GetNumberOfPoints()==5233
calc=Calculator(Input=reader);calc.ResultArrayName='PressureRatio';calc.Function=str(settings['factor'])+' * Pressure / 101325';calc.UpdatePipeline()
SaveData(str(out/'pressure.vtu'),proxy=calc)
contour=Contour(Input=calc);contour.ContourBy=['POINTS','Mach'];contour.Isosurfaces=[1.0];contour.UpdatePipeline()
assert contour.GetDataInformation().GetNumberOfPoints()>0
SaveData(str(out/'sonic.vtp'),proxy=contour);SaveData(str(out/'sonic.csv'),proxy=contour)
view=CreateView('RenderView');view.ViewSize=[640,360];view.OrientationAxesVisibility=0
display=Show(calc,view);ColorBy(display,('POINTS','PressureRatio'));display.RescaleTransferFunctionToDataRange(True,False)
view.CameraPosition=[.5,0,5];view.CameraFocalPoint=[.5,0,0];view.CameraViewUp=[0,1,0]
view.CameraParallelProjection=1;view.CameraParallelScale=.65
Render(view);SaveScreenshot(str(out/'pressure.png'),view=view,ImageResolution=[640,360])
(out/'remove-me.txt').unlink(missing_ok=True)
(out/'statistics.json').write_text(json.dumps({'version':str(GetParaViewVersion()),'points':calc.GetDataInformation().GetNumberOfPoints(),
 'cells':calc.GetDataInformation().GetNumberOfCells(),'sonic_points':contour.GetDataInformation().GetNumberOfPoints(),'factor':settings['factor']}))
print('ACTUAL_PARAVIEW_NACA_COMPLETE',settings['factor'])
