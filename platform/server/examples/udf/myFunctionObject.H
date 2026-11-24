#ifndef myFunctionObject_H
#define myFunctionObject_H

#include "functionObject.H"
#include "className.H"
#include "Time.H"
#include "dictionary.H"

namespace Foam
{

class myFunctionObject : public functionObject
{
public:
    TypeName("myFunctionObject");

    myFunctionObject(const word& name, const Time& time, const dictionary& dict);
    virtual ~myFunctionObject() = default;

    virtual bool execute();
    virtual bool write();
};

} // End namespace Foam

#endif
