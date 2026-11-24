#ifndef Foam_fv_fixedVolumetricFan_H
#define Foam_fv_fixedVolumetricFan_H

#include "cellSetOption.H"

namespace Foam
{
namespace fv
{

class fixedVolumetricFan : public cellSetOption
{
    vector direction_;
    scalar target_;
    scalar area_;
    scalar thickness_;
    label faceZone_;
    scalar pressure_;
    label timeIndex_;

    scalar flowRate(const surfaceScalarField& phi, const surfaceScalarField& density) const;

public:
    TypeName("fixedVolumetricFan");

    fixedVolumetricFan(const word& name, const word& modelType, const dictionary& dict, const fvMesh& mesh);

    void addSup(const volScalarField& rho, fvMatrix<vector>& eqn, const label fieldi) override;
};

}
}

#endif
