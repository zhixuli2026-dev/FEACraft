"""Pure actual-input fixture: unit cube, linear steady conduction and formal report.

No providers, process substitutes, result fixtures or imported acceptance mains.
"""
from app.services.casedict import set_block

RUN='C-run/conduction'
GEOMETRY='''import cadquery as cq
def build_geometry(ctx):
    p=ctx.data['geometry']['parameters']
    return cq.Workplane('XY').box(float(p['lx']),float(p['ly']),float(p['lz']),centered=(False,False,False))
'''


def case_dictionary(original):
    text=set_block(original,'caseMeta',{'discipline':'thermal','analysisType':'steady','dimensions':'3D',
        'units':{'length':'m','temperature':'K'}})
    text=set_block(text,'geometry',{'generator':'cadquery','parameters':{'lx':1,'ly':1,'lz':1}})
    text=set_block(text,'mesh',{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'policy':'fail'}})
    text=set_block(text,'numericalMethod',{'solver':'laplacianFoam'})
    post={'runDir':RUN,'fields':{'T':{'unit':'K'}},'dataExtraction':{
        'volumeAverages':{'mean':{'field':'T','time':1}},
        'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
        'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
            'unit':'K','kind':'verification','source':'"analytic linear conduction"','l2_max':1e-6}}},
        'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,'formats':['png'],'title':'"Temperature"'}}}}
    return set_block(text,'postProcessing',post)


def thermal_files(nx=20):
    h='FoamFile\n{ version 2.0; format ascii; class %s; object %s; }\n'
    mesh='''convertToMeters 1;
vertices ((0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 1) (1 0 1) (1 1 1) (0 1 1));
blocks (hex (0 1 2 3 4 5 6 7) (NX 4 4) simpleGrading (1 1 1));
boundary (
 xMin { type wall; faces ((0 4 7 3)); }
 xMax { type wall; faces ((1 2 6 5)); }
 insulated { type wall; faces ((0 1 5 4) (3 7 6 2) (0 3 2 1) (4 5 6 7)); }
);
'''.replace('NX',str(nx))
    return {
        RUN+'/system/blockMeshDict':h%('dictionary','blockMeshDict')+mesh,
        RUN+'/system/controlDict':h%('dictionary','controlDict')+'''application laplacianFoam;
startFrom startTime; startTime 0; stopAt endTime; endTime 1; deltaT 1;
writeControl timeStep; writeInterval 1; writeFormat ascii; writePrecision 12;
writeCompression off; timeFormat general; timePrecision 6; runTimeModifiable false;
''',
        RUN+'/system/fvSchemes':h%('dictionary','fvSchemes')+'''ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear orthogonal; }
interpolationSchemes { default linear; }
snGradSchemes { default orthogonal; }
''',
        RUN+'/system/fvSolution':h%('dictionary','fvSolution')+'''solvers { T { solver PCG; preconditioner DIC; tolerance 1e-12; relTol 0; } }
SIMPLE { nNonOrthogonalCorrectors 0; }
''',
        RUN+'/constant/transportProperties':h%('dictionary','transportProperties')+'DT [0 2 -1 0 0 0 0] 0.01;\n',
        RUN+'/0/T':h%('volScalarField','T')+'''dimensions [0 0 0 1 0 0 0]; internalField uniform 300;
boundaryField {
 xMin { type fixedValue; value uniform 300; }
 xMax { type fixedValue; value uniform 400; }
 insulated { type zeroGradient; }
}
'''}
