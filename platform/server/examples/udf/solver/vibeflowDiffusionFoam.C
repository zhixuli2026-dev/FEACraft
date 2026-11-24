#include "fvCFD.H"

int main(int argc, char* argv[])
{
    #include "setRootCase.H"
    #include "createTime.H"
    #include "createMesh.H"

    Foam::IOdictionary properties
    (
        Foam::IOobject
        (
            "transportProperties", runTime.constant(), mesh,
            Foam::IOobject::MUST_READ, Foam::IOobject::NO_WRITE
        )
    );
    const Foam::dimensionedScalar diffusivity("DT", Foam::dimViscosity, properties);
    Foam::volScalarField temperature
    (
        Foam::IOobject
        (
            "T", runTime.timeName(), mesh,
            Foam::IOobject::MUST_READ, Foam::IOobject::AUTO_WRITE
        ),
        mesh
    );

    Foam::Info << "VIBEFLOW_DIFFUSION_SOLVER " << diffusivity.value() << Foam::nl;
    while (runTime.loop())
    {
        Foam::Info << "Time = " << runTime.timeName() << Foam::nl;
        Foam::solve(Foam::fvm::ddt(temperature) - Foam::fvm::laplacian(diffusivity, temperature));
        runTime.write();
    }
    Foam::Info << "End" << Foam::nl;
    return 0;
}
