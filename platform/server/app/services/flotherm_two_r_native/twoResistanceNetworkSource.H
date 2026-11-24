/*---------------------------------------------------------------------------*\
  flotherm2foam: conservative branch of a two-resistance compact thermal model
\*---------------------------------------------------------------------------*/
#ifndef Foam_fv_twoResistanceNetworkSource_H
#define Foam_fv_twoResistanceNetworkSource_H

#include "fvOption.H"
#include "boundBox.H"

namespace Foam
{
namespace fv
{

class twoResistanceNetworkSource : public fv::option
{
    word deviceId_;
    word localRegion_;
    word remoteRegion_;
    boundBox localBounds_;
    boundBox remoteBounds_;
    scalar power_;
    scalar localResistance_;
    scalar remoteResistance_;
    labelList localCells_;
    mutable label lastLogTimeIndex_;

    static boundBox readBounds(const dictionary& dict, const word& key);
    static labelList selectCells(const fvMesh& mesh, const boundBox& bounds, const word& deviceId);
    static scalar averageTemperature(const fvMesh& mesh, const labelList& cells, const word& deviceId);
    void configure();
    void apply(fvMatrix<scalar>& eqn) const;

public:
    TypeName("twoResistanceNetworkSource");

    twoResistanceNetworkSource
    (
        const word& sourceName,
        const word& modelType,
        const dictionary& dict,
        const fvMesh& mesh
    );

    twoResistanceNetworkSource(const twoResistanceNetworkSource&) = delete;
    void operator=(const twoResistanceNetworkSource&) = delete;
    virtual ~twoResistanceNetworkSource() = default;

    virtual void addSup(fvMatrix<scalar>& eqn, const label fieldi);
    virtual void addSup
    (
        const volScalarField& rho,
        fvMatrix<scalar>& eqn,
        const label fieldi
    );
    virtual bool read(const dictionary& dict);
};

} // End namespace fv
} // End namespace Foam

#endif

// ************************************************************************* //

