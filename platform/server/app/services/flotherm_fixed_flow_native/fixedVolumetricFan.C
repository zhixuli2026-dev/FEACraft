#include "fixedVolumetricFan.H"
#include "addToRunTimeSelectionTable.H"
#include "fvc.H"
#include "fvMatrices.H"
#include "processorPolyPatch.H"
#include "PstreamReduceOps.H"
#include "IOdictionary.H"
#include <cmath>

namespace Foam
{
namespace fv
{
    defineTypeNameAndDebug(fixedVolumetricFan, 0);
    addToRunTimeSelectionTable(option, fixedVolumetricFan, dictionary);
}
}


Foam::fv::fixedVolumetricFan::fixedVolumetricFan
(
    const word& name,
    const word& modelType,
    const dictionary& dict,
    const fvMesh& mesh
)
:
    cellSetOption(name, modelType, dict, mesh),
    direction_(coeffs_.get<vector>("flowDir")),
    target_(coeffs_.get<scalar>("volumeFlow")),
    area_(coeffs_.get<scalar>("area")),
    thickness_(coeffs_.get<scalar>("thickness")),
    faceZone_(mesh.faceZones().findZoneID(coeffs_.get<word>("faceZone"))),
    pressure_(0),
    timeIndex_(-1)
{
    if (target_ <= 0 || area_ <= 0 || thickness_ <= 0 || mag(direction_) <= SMALL
        || returnReduce(faceZone_ < 0, orOp<bool>()))
    {
        FatalIOErrorInFunction(coeffs_)
            << "A fixed volumetric fan requires a positive flow, area, thickness and inlet face zone"
            << exit(FatalIOError);
    }
    direction_.normalise();
    fieldNames_.resize(1, "U");
    fv::option::resetApplied();

    const IOdictionary state
    (
        IOobject(name_+"Properties", mesh.time().timeName(), "uniform", mesh,
                 IOobject::READ_IF_PRESENT, IOobject::NO_WRITE, IOobject::NO_REGISTER)
    );
    pressure_ = state.getOrDefault<scalar>("gradient", 0)*thickness_;
}


Foam::scalar Foam::fv::fixedVolumetricFan::flowRate
(
    const surfaceScalarField& phi,
    const surfaceScalarField& density
) const
{
    const faceZone& inlet = mesh_.faceZones()[faceZone_];
    scalar flow = 0;
    forAll(inlet, i)
    {
        const label face = inlet[i];
        const scalar sign = inlet.flipMap()[i] ? -1 : 1;
        if (mesh_.isInternalFace(face))
        {
            flow += sign*phi[face]/density[face];
            continue;
        }
        // Decomposition duplicates a processor face in both neighbouring meshes.
        const label patchId = mesh_.boundaryMesh().whichPatch(face);
        const polyPatch& patch = mesh_.boundaryMesh()[patchId];
        if (isA<processorPolyPatch>(patch) && refCast<const processorPolyPatch>(patch).owner())
        {
            const label index = patch.whichFace(face);
            flow += sign*phi.boundaryField()[patchId][index]/density.boundaryField()[patchId][index];
        }
    }
    return returnReduce(flow, sumOp<scalar>());
}


void Foam::fv::fixedVolumetricFan::addSup
(
    const volScalarField& rho,
    fvMatrix<vector>& eqn,
    const label
)
{
    const Time& time = mesh_.time();
    if (timeIndex_ != time.timeIndex())
    {
        const auto& phi = mesh_.lookupObject<surfaceScalarField>("phi");
        const scalar measured = flowRate(phi, fvc::interpolate(rho)());
        if (!std::isfinite(measured))
        {
            FatalErrorInFunction << "Non-finite inlet flow for " << name_ << exit(FatalError);
        }
        const scalar relativeError = (target_ - measured)/target_;
        const scalar speed = target_/area_;
        const scalar scale = max(max(sqr(speed), 1e-6), 0.1*mag(pressure_));
        pressure_ += 0.2*scale*min(max(relativeError, -2.0), 2.0);
        timeIndex_ = time.timeIndex();

        if (time.writeTime())
        {
            IOdictionary state
            (
                IOobject(name_+"Properties", time.timeName(), "uniform", mesh_,
                         IOobject::NO_READ, IOobject::NO_WRITE, IOobject::NO_REGISTER)
            );
            state.add("gradient", pressure_/thickness_);
            state.add("flow_rate", measured);
            state.regIOobject::write();
        }
    }

    auto source = volVectorField::Internal::New
    (
        name_+"U", IOobject::NO_REGISTER, mesh_, dimensionedVector(eqn.dimensions()/dimVolume, Zero)
    );
    UIndirectList<vector>(source.ref(), cells_) = direction_*(pressure_/thickness_);
    eqn += source;
}
