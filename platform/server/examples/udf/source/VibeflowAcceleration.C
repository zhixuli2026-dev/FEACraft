#include "addToRunTimeSelectionTable.H"
#include "fvMatrices.H"
#include "fvOption.H"

namespace Foam::fv
{
class VibeflowAcceleration final : public option
{
    vector acceleration_;

public:
    TypeName("vibeflowUniformAcceleration");

    VibeflowAcceleration
    (
        const word& name,
        const word& modelType,
        const dictionary& settings,
        const fvMesh& mesh
    )
    : option(name, modelType, settings, mesh), acceleration_(coeffs_.get<vector>("acceleration"))
    {
        fieldNames_.resize(1);
        fieldNames_[0] = "U";
        resetApplied();
        Info << "VIBEFLOW_ACCELERATION " << acceleration_ << nl;
    }

    void addSup(fvMatrix<vector>& equation, const label) override
    {
        auto& source = equation.source();
        forAll(source, cell)
        {
            source[cell] -= mesh_.V()[cell]*acceleration_;
        }
    }

    bool read(const dictionary& settings) override
    {
        if (!option::read(settings))
        {
            return false;
        }
        acceleration_ = coeffs_.get<vector>("acceleration");
        return true;
    }
};

defineTypeNameAndDebug(VibeflowAcceleration, 0);
addToRunTimeSelectionTable(option, VibeflowAcceleration, dictionary);
}
