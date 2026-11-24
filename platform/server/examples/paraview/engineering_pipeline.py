"""Actual SU2 -> ParaView -> Pandoc workflow for an isolated engineering case."""
import csv,hashlib,json,math,os,secrets,shutil,subprocess
from pathlib import Path
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

root=Path.cwd();result=root/'E-report/engineering-result.json'
if result.exists():raise RuntimeError('This case already has a completed engineering result')
for folder in ['C-run','D-post','E-report']:(root/folder).mkdir(exist_ok=True)
steps=[]
def execute(name,args):
 executable=shutil.which(args[0])
 if executable is None:raise FileNotFoundError(args[0])
 env=dict(os.environ,VIBEFLOW_CASE_ROOT=str(root))
 with (root/f'E-report/{name}.log').open('wb') as log:
  completed=subprocess.run([executable,*args[1:]],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=300)
 steps.append({'tool':name,'executable':executable,'returncode':completed.returncode})
 completed.check_returncode()

execute('su2',['SU2_CFD','C-run/naca.cfg','-t','2'])
rows=[{key.strip():value for key,value in row.items()} for row in csv.DictReader((root/'history.csv').open(),skipinitialspace=True)]
density=float(rows[-1]['rms[Rho]'])
assert density<-8 and 'Exit Success (SU2_CFD)' in (root/'E-report/su2.log').read_text(errors='replace')
shutil.copyfile(root/'flow.vtu',root/'Ref/naca.vtu')
execute('paraview',['pvpython','Ref/naca_postprocess.py'])
reader=vtkXMLUnstructuredGridReader();reader.SetFileName(str(root/'D-post/pressure.vtu'));reader.Update();grid=reader.GetOutput()
assert grid.GetNumberOfPoints()==5233 and grid.GetNumberOfCells()==10216
pressure=grid.GetPointData().GetArray('Pressure');ratio=grid.GetPointData().GetArray('PressureRatio');mach=grid.GetPointData().GetArray('Mach')
error=max(abs(ratio.GetValue(i)-pressure.GetValue(i)/101325) for i in range(5233));assert error<1e-12
max_mach=max(mach.GetValue(i) for i in range(5233));assert math.isfinite(max_mach) and .8<max_mach<3
statistics=json.loads((root/'D-post/statistics.json').read_text());assert statistics['sonic_points']==148
proof='ENGINEERING_'+secrets.token_hex(12)
text=('# Actual NACA0012 engineering report\n\n'
      f'Proof: {proof}\n\nPoints: 5233; cells: 10216.\n\n'
      f'Density log10 residual: {density:.9f}; maximum Mach: {max_mach:.9f}.\n\n'
      f'Pressure ratio maximum absolute error: {error:.3g}; sonic contour points: 148.\n\n'
      '![Computed pressure ratio around NACA0012](D-post/pressure.png)\n\n'
      'This verifies numerical execution and postprocessing, not experimental validation.\n')
(root/'E-report/engineering.md').write_text(text,encoding='utf8')
execute('pandoc',['pandoc','E-report/engineering.md','--resource-path=.','-o','E-report/engineering.docx'])
outputs={p:{'sha256':hashlib.sha256((root/p).read_bytes()).hexdigest(),'bytes':(root/p).stat().st_size}
         for p in ['flow.vtu','D-post/pressure.vtu','D-post/sonic.vtp','D-post/sonic.csv','D-post/pressure.png','E-report/engineering.md','E-report/engineering.docx']}
result.write_text(json.dumps({'proof':proof,'points':5233,'cells':10216,'density_log10_residual':density,'maximum_mach':max_mach,
 'pressure_ratio_max_absolute_error':error,'sonic_points':148,'steps':steps,'outputs':outputs},indent=2)+'\n',encoding='utf8')
print('ENGINEERING_PIPELINE_COMPLETE: read E-report/engineering-result.json for verified values and artifact paths.')
