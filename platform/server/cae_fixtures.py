"""Deterministic OpenFOAM benchmark inputs, independent of the API under test."""

def cavity_2d():
    H = "FoamFile\n{\n    version     2.0;\n    format      ascii;\n    class       %s;\n    object      %s;\n}\n// ***//\n\n"

    FILES = {
    "C-run/case-1/system/blockMeshDict": H % ("dictionary", "blockMeshDict") + """
    convertToMeters 0.1;
    vertices
    (
        (0 0 0) (1 0 0) (1 1 0) (0 1 0)
        (0 0 0.1) (1 0 0.1) (1 1 0.1) (0 1 0.1)
    );
    blocks ( hex (0 1 2 3 4 5 6 7) (20 20 1) simpleGrading (1 1 1) );
    boundary
    (
        movingWall  { type wall;  faces ((3 7 6 2)); }
        fixedWalls  { type wall;  faces ((0 4 7 3) (2 6 5 1) (0 1 5 4)); }
        frontAndBack{ type empty; faces ((0 3 2 1) (4 5 6 7)); }
    );
    """,
    "C-run/case-1/system/controlDict": H % ("dictionary", "controlDict") + """
    application     icoFoam;
    startFrom       startTime;
    startTime       0;
    stopAt          endTime;
    endTime         0.5;
    deltaT          0.005;
    writeControl    timeStep;
    writeInterval   20;
    purgeWrite      0;
    writeFormat     ascii;
    writePrecision  6;
    writeCompression off;
    timeFormat      general;
    timePrecision   6;
    runTimeModifiable true;
    """,
    "C-run/case-1/system/fvSchemes": H % ("dictionary", "fvSchemes") + """
    ddtSchemes          { default Euler; }
    gradSchemes         { default Gauss linear; }
    divSchemes          { default none; div(phi,U) Gauss linear; }
    laplacianSchemes    { default Gauss linear orthogonal; }
    interpolationSchemes{ default linear; }
    snGradSchemes       { default orthogonal; }
    """,
    "C-run/case-1/system/fvSolution": H % ("dictionary", "fvSolution") + """
    solvers
    {
        p
        {
            solver          PCG;
            preconditioner  DIC;
            tolerance       1e-06;
            relTol          0.05;
        }
        pFinal
        {
            $p;
            relTol          0;
        }
        U
        {
            solver          smoothSolver;
            smoother        symGaussSeidel;
            tolerance       1e-05;
            relTol          0;
        }
    }
    PISO
    {
        nCorrectors     2;
        nNonOrthogonalCorrectors 0;
        pRefCell        0;
        pRefValue       0;
    }
    """,
    "C-run/case-1/constant/transportProperties": H % ("dictionary", "transportProperties") + """
    transportModel  Newtonian;
    nu              nu [0 2 -1 0 0 0 0] 0.01;
    """,
    "C-run/case-1/0/U": H % ("volVectorField", "U") + """
    dimensions      [0 1 -1 0 0 0 0];
    internalField   uniform (0 0 0);
    boundaryField
    {
        movingWall  { type fixedValue; value uniform (1 0 0); }
        fixedWalls  { type fixedValue; value uniform (0 0 0); }
        frontAndBack{ type empty; }
    }
    """,
    "C-run/case-1/0/p": H % ("volScalarField", "p") + """
    dimensions      [0 2 -2 0 0 0 0];
    internalField   uniform 0;
    boundaryField
    {
        movingWall  { type zeroGradient; }
        fixedWalls  { type zeroGradient; }
        frontAndBack{ type empty; }
    }
    """,
    }
    return FILES

def cavity_3d():
    H = "FoamFile\n{\n    version     2.0;\n    format      ascii;\n    class       %s;\n    object      %s;\n}\n// ***//\n\n"

    FILES = {
    "C-run/case-3d/system/blockMeshDict": H % ("dictionary", "blockMeshDict") + """
    convertToMeters 0.1;
    vertices
    (
        (0 0 0) (1 0 0) (1 1 0) (0 1 0)
        (0 0 1) (1 0 1) (1 1 1) (0 1 1)
    );
    blocks ( hex (0 1 2 3 4 5 6 7) (30 30 30) simpleGrading (1 1 1) );
    boundary
    (
        movingWall { type wall; faces ((3 7 6 2)); }
        fixedWalls { type wall; faces ((0 4 7 3) (2 6 5 1) (0 1 5 4) (0 3 2 1) (4 5 6 7)); }
    );
    """,
    "C-run/case-3d/system/controlDict": H % ("dictionary", "controlDict") + """
    application     icoFoam;
    startFrom       startTime;
    startTime       0;
    stopAt          endTime;
    endTime         0.1;
    deltaT          0.005;
    writeControl    timeStep;
    writeInterval   4;
    purgeWrite      0;
    writeFormat     ascii;
    writePrecision  6;
    writeCompression off;
    timeFormat      general;
    timePrecision   6;
    runTimeModifiable true;
    """,
    "C-run/case-3d/system/fvSchemes": H % ("dictionary", "fvSchemes") + """
    ddtSchemes          { default Euler; }
    gradSchemes         { default Gauss linear; }
    divSchemes          { default none; div(phi,U) Gauss linear; }
    laplacianSchemes    { default Gauss linear orthogonal; }
    interpolationSchemes{ default linear; }
    snGradSchemes       { default orthogonal; }
    """,
    "C-run/case-3d/system/fvSolution": H % ("dictionary", "fvSolution") + """
    solvers
    {
        p { solver PCG; preconditioner DIC; tolerance 1e-06; relTol 0.05; }
        pFinal { $p; relTol 0; }
        U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-05; relTol 0; }
    }
    PISO { nCorrectors 2; nNonOrthogonalCorrectors 0; pRefCell 0; pRefValue 0; }
    """,
    "C-run/case-3d/constant/transportProperties": H % ("dictionary", "transportProperties") + """
    transportModel  Newtonian;
    nu              nu [0 2 -1 0 0 0 0] 0.01;
    """,
    "C-run/case-3d/0/U": H % ("volVectorField", "U") + """
    dimensions      [0 1 -1 0 0 0 0];
    internalField   uniform (0 0 0);
    boundaryField
    {
        movingWall { type fixedValue; value uniform (1 0 0); }
        fixedWalls { type fixedValue; value uniform (0 0 0); }
    }
    """,
    "C-run/case-3d/0/p": H % ("volScalarField", "p") + """
    dimensions      [0 2 -2 0 0 0 0];
    internalField   uniform 0;
    boundaryField
    {
        movingWall { type zeroGradient; }
        fixedWalls { type zeroGradient; }
    }
    """,
    }
    return FILES

