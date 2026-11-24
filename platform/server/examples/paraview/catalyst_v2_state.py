"""Extract selected timesteps from the live Catalyst 2 heat channel."""

import os

from paraview import catalyst, simple


options = catalyst.Options()
options.ExtractsOutputDirectory = os.environ['VIBEFLOW_CATALYST_OUTPUT']
source = simple.TrivialProducer(registrationName='heat')
merged = simple.MergeBlocks(Input=source)
for kind, suffix in (('VTU','pvtu'),('CSV','csv')):
    extract = simple.CreateExtractor(kind,merged,registrationName=f'temperature-{kind}')
    extract.Writer.FileName = 'temperature_{timestep:06d}.'+suffix
    if kind == 'VTU':
        extract.Writer.UseSubdirectory = 0
    if kind == 'CSV':
        extract.Writer.Precision = 12
    extract.Trigger.Frequency = 2


def catalyst_execute(info):
    source.UpdatePipeline(info.time)
    print(f'Catalyst channel heat points={source.GetDataInformation().GetNumberOfPoints()} '
          f'cycle={info.cycle} time={info.time}',flush=True)
