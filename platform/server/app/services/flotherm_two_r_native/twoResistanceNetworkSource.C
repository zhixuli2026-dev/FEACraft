#include "twoResistanceNetworkSource.H"
#include "addToRunTimeSelectionTable.H"
#include "fvMatrices.H"
#include "PstreamReduceOps.H"
#include "DynamicList.H"

namespace Foam
{
namespace fv
{
    defineTypeNameAndDebug(twoResistanceNetworkSource, 0);
    addToRunTimeSelectionTable(option, twoResistanceNetworkSource, dictionary);
}
}


Foam::boundBox Foam::fv::twoResistanceNetworkSource::readBounds
(
    const dictionary& dict,
    const word& key
)
{
    const List<vector> points(dict.lookup(key));
    if (points.size() != 2)
    {
        FatalIOErrorInFunction(dict)
            << "Entry '" << key << "' must contain exactly two vectors" << nl
            << exit(FatalIOError);
    }
    const boundBox result(points[0], points[1]);
    if (!result.good())
    {
        FatalIOErrorInFunction(dict)
            << "Entry '" << key << "' is not a valid boundBox: " << result << nl
            << exit(FatalIOError);
    }
    return result;
}


Foam::labelList Foam::fv::twoResistanceNetworkSource::selectCells
(
    const fvMesh& mesh,
    const boundBox& bounds,
    const word& deviceId
)
{
    DynamicList<label> selected;
    const vectorField& centres = mesh.C();
    scalar localVolume = 0;
    forAll(centres, celli)
    {
        if (bounds.contains(centres[celli]))
        {
            selected.append(celli);
            localVolume += mesh.V()[celli];
        }
    }
    const scalar globalVolume = returnReduce(localVolume, sumOp<scalar>());
    if (globalVolume <= VSMALL)
    {
        FatalErrorInFunction
            << "2R device " << deviceId << " selected no cells in region "
            << mesh.name() << "; bounds=" << bounds << nl
            << "Refine the mesh or inspect conversion/model.json."
            << exit(FatalError);
    }
    labelList result;
    result.transfer(selected);
    return result;
}


Foam::scalar Foam::fv::twoResistanceNetworkSource::averageTemperature
(
    const fvMesh& mesh,
    const labelList& cells,
    const word& deviceId
)
{
    if (!mesh.foundObject<volScalarField>("T"))
    {
        FatalErrorInFunction
            << "Region " << mesh.name() << " has no T field for 2R device "
            << deviceId << exit(FatalError);
    }
    const volScalarField& temperature = mesh.lookupObject<volScalarField>("T");
    scalar localWeightedT = 0;
    scalar localVolume = 0;
    for (const label celli : cells)
    {
        localWeightedT += temperature[celli]*mesh.V()[celli];
        localVolume += mesh.V()[celli];
    }
    const scalar globalWeightedT = returnReduce(localWeightedT, sumOp<scalar>());
    const scalar globalVolume = returnReduce(localVolume, sumOp<scalar>());
    if (globalVolume <= VSMALL)
    {
        FatalErrorInFunction
            << "Zero selected volume while averaging 2R device " << deviceId
            << " in region " << mesh.name() << exit(FatalError);
    }
    return globalWeightedT/globalVolume;
}


Foam::fv::twoResistanceNetworkSource::twoResistanceNetworkSource
(
    const word& sourceName,
    const word& modelType,
    const dictionary& dict,
    const fvMesh& mesh
)
:
    fv::option(sourceName, modelType, dict, mesh),
    deviceId_(word::null),
    localRegion_(word::null),
    remoteRegion_(word::null),
    localBounds_(),
    remoteBounds_(),
    power_(0),
    localResistance_(0),
    remoteResistance_(0),
    localCells_(),
    lastLogTimeIndex_(-1)
{
    configure();
}


void Foam::fv::twoResistanceNetworkSource::configure()
{
    fieldNames_ = coeffs_.getOrDefault<wordList>("fields", wordList(1, "h"));
    if (fieldNames_.size() != 1)
    {
        FatalIOErrorInFunction(coeffs_)
            << "Exactly one energy field is supported; got " << fieldNames_ << nl
            << exit(FatalIOError);
    }
    fv::option::resetApplied();

    deviceId_ = coeffs_.getOrDefault<word>("deviceId", name_);
    localRegion_ = coeffs_.get<word>("localRegion");
    remoteRegion_ = coeffs_.get<word>("remoteRegion");
    if (localRegion_ != mesh_.name())
    {
        FatalIOErrorInFunction(coeffs_)
            << "localRegion=" << localRegion_ << " but option is loaded on "
            << mesh_.name() << " for device " << deviceId_ << nl
            << exit(FatalIOError);
    }
    localBounds_ = readBounds(coeffs_, "localBounds");
    remoteBounds_ = readBounds(coeffs_, "remoteBounds");
    power_ = coeffs_.get<scalar>("power");
    localResistance_ = coeffs_.get<scalar>("localResistance");
    remoteResistance_ = coeffs_.get<scalar>("remoteResistance");
    if (localResistance_ <= VSMALL || remoteResistance_ <= VSMALL)
    {
        FatalIOErrorInFunction(coeffs_)
            << "Both 2R resistances must be positive for device " << deviceId_
            << exit(FatalIOError);
    }
    localCells_ = selectCells(mesh_, localBounds_, deviceId_);
    lastLogTimeIndex_ = -1;
}


void Foam::fv::twoResistanceNetworkSource::apply(fvMatrix<scalar>& eqn) const
{
    if (!mesh_.time().foundObject<fvMesh>(remoteRegion_))
    {
        FatalErrorInFunction
            << "Cannot find remote region " << remoteRegion_ << " for 2R device "
            << deviceId_ << exit(FatalError);
    }
    const fvMesh& remoteMesh = mesh_.time().lookupObject<fvMesh>(remoteRegion_);
    const labelList remoteCells(selectCells(remoteMesh, remoteBounds_, deviceId_));
    const scalar localT = averageTemperature(mesh_, localCells_, deviceId_);
    const scalar remoteT = averageTemperature(remoteMesh, remoteCells, deviceId_);
    const scalar localG = 1.0/localResistance_;
    const scalar remoteG = 1.0/remoteResistance_;
    const scalar junctionT =
        (power_ + localT*localG + remoteT*remoteG)/(localG + remoteG);
    const scalar localPower = (junctionT - localT)/localResistance_;

    scalar localVolume = 0;
    for (const label celli : localCells_)
    {
        localVolume += mesh_.V()[celli];
    }
    const scalar globalVolume = returnReduce(localVolume, sumOp<scalar>());

    auto tsu = DimensionedField<scalar, volMesh>::New
    (
        name_ + eqn.psi().name() + "Su",
        mesh_,
        dimensionedScalar(eqn.dimensions()/dimVolume, Zero)
    );
    scalarField& values = tsu.ref().field();
    for (const label celli : localCells_)
    {
        values[celli] += localPower/globalVolume;
    }
    eqn += tsu;

    if
    (
        mesh_.time().outputTime()
     && mesh_.time().timeIndex() != lastLogTimeIndex_
    )
    {
        lastLogTimeIndex_ = mesh_.time().timeIndex();
        if (Pstream::master())
        {
            Info<< type() << " device=" << deviceId_
                << " time=" << mesh_.time().timeName()
                << " localRegion=" << localRegion_
                << " Tlocal=" << localT
                << " Tremote=" << remoteT
                << " Tjunction=" << junctionT
                << " Qlocal=" << localPower << endl;
        }
    }
}


void Foam::fv::twoResistanceNetworkSource::addSup
(
    fvMatrix<scalar>& eqn,
    const label
)
{
    apply(eqn);
}


void Foam::fv::twoResistanceNetworkSource::addSup
(
    const volScalarField&,
    fvMatrix<scalar>& eqn,
    const label
)
{
    apply(eqn);
}


bool Foam::fv::twoResistanceNetworkSource::read(const dictionary& dict)
{
    if (!fv::option::read(dict))
    {
        return false;
    }
    configure();
    return true;
}

// ************************************************************************* //

