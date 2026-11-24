#include "addToRunTimeSelectionTable.H"
#include "fixedValueFvPatchFields.H"
#include "fvPatchFieldMapper.H"

namespace Foam
{
class VibeflowVelocityBoundary final : public fixedValueFvPatchVectorField
{
    using Base = fixedValueFvPatchVectorField;
    using Internal = DimensionedField<vector, volMesh>;

    vector velocity_;

public:
    TypeName("vibeflowFixedVelocity");

    VibeflowVelocityBoundary(const fvPatch& patch, const Internal& field)
    : Base(patch, field), velocity_(Zero)
    {}

    VibeflowVelocityBoundary
    (
        const fvPatch& patch,
        const Internal& field,
        const dictionary& settings
    )
    : Base(patch, field, settings), velocity_(settings.get<vector>("velocity"))
    {
        operator==(velocity_);
        Info << "VIBEFLOW_BOUNDARY_APPLIED " << patch.name() << ' ' << velocity_ << nl;
    }

    VibeflowVelocityBoundary
    (
        const VibeflowVelocityBoundary& source,
        const fvPatch& patch,
        const Internal& field,
        const fvPatchFieldMapper& mapper
    )
    : Base(source, patch, field, mapper), velocity_(source.velocity_)
    {}

    VibeflowVelocityBoundary(const VibeflowVelocityBoundary& source, const Internal& field)
    : Base(source, field), velocity_(source.velocity_)
    {}

    tmp<fvPatchVectorField> clone() const override
    {
        return tmp<fvPatchVectorField>(new VibeflowVelocityBoundary(*this));
    }

    tmp<fvPatchVectorField> clone(const Internal& field) const override
    {
        return tmp<fvPatchVectorField>(new VibeflowVelocityBoundary(*this, field));
    }

    void updateCoeffs() override
    {
        if (!updated())
        {
            operator==(velocity_);
            Base::updateCoeffs();
        }
    }

    void write(Ostream& stream) const override
    {
        Base::write(stream);
        stream.writeEntry("velocity", velocity_);
    }
};

makePatchTypeField(fvPatchVectorField, VibeflowVelocityBoundary);
}
