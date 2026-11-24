"""D15 golden：真实 ParaView server-manager 数据源、VTP 与离屏截图。"""
from pathlib import Path
import os

from paraview.simple import CreateView, Render, SaveData, SaveScreenshot, Show, Sphere

root = Path(os.environ["VIBEFLOW_CASE_ROOT"])
out = root / "D-post"
out.mkdir(parents=True, exist_ok=True)
sphere = Sphere(ThetaResolution=16, PhiResolution=16)
sphere.UpdatePipeline()
view = CreateView("RenderView")
Show(sphere, view)
Render(view)
SaveData(str(out / "paraview-sphere.vtp"), proxy=sphere)
SaveScreenshot(str(out / "paraview-sphere.png"), view=view)
print("PARAVIEW_SM_OK", sphere.GetDataInformation().GetNumberOfPoints())
