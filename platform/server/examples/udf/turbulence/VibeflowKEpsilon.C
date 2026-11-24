#include "kEpsilon.H"
#include "turbulentTransportModels.H"
#include <cmath>

namespace Foam::RASModels
{
template<class BasicModel>
class VibeflowKEpsilon final : public kEpsilon<BasicModel>
{
    using Parent = kEpsilon<BasicModel>;

    void applyCoefficient()
    {
        const scalar coefficient = this->coeffDict().template get<scalar>("effectiveCmu");
        if (!(coefficient > 0) || !std::isfinite(coefficient))
        {
            FatalErrorInFunction << "effectiveCmu must be positive and finite" << exit(FatalError);
        }
        this->Cmu_.value() = coefficient;
        this->correctNut();
    }

public:
    TypeName("vibeflowKEpsilon");

    VibeflowKEpsilon
    (
        const typename Parent::alphaField& alpha,
        const typename Parent::rhoField& density,
        const volVectorField& velocity,
        const surfaceScalarField& massFlux,
        const surfaceScalarField& flux,
        const typename Parent::transportModel& transport,
        const word& propertiesName
    )
    : Parent(alpha, density, velocity, massFlux, flux, transport, propertiesName, typeName)
    {
        applyCoefficient();
        Info << "VIBEFLOW_RAS_CMU " << this->Cmu_.value() << nl;
    }

    bool read() override
    {
        if (!Parent::read())
        {
            return false;
        }
        applyCoefficient();
        return true;
    }
};
}

makeRASModel(VibeflowKEpsilon);
