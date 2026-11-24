#include "myFunctionObject.H"
#include "error.H"
#include "addToRunTimeSelectionTable.H"

namespace Foam
{
    defineTypeNameAndDebug(myFunctionObject, 0);
    addToRunTimeSelectionTable(functionObject, myFunctionObject, dictionary);

    myFunctionObject::myFunctionObject(const word& name, const Time& time, const dictionary& dict)
    :
        functionObject(name, true)
    {}

    bool myFunctionObject::execute()
    {
        static bool first = true;
        if (first)
        {
            Info << "P0_UDF_OK" << endl;
            first = false;
        }
        return true;
    }

    bool myFunctionObject::write()
    {
        return true;
    }
}
