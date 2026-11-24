"""Native controlDict insertion preserves existing objects, directives and code."""
import json

from app.services.udf_control import insert_function


def main():
    checks = []
    source = '''FoamFile { class dictionary; object controlDict; }
#include "functions"
application icoFoam;
functions
{
    // type myFunctionObject; is a comment, not an existing registration.
    existing
    {
        type coded;
        codeExecute #{ const char* s = "{ functions }"; #};
        libs ("libutilityFunctionObjects.so");
    }
    #include "moreFunctions"
}
// retain trailing comment
'''
    result = insert_function(source, "fixture")
    expected_entry = '\n    fixture\n    {\n        type myFunctionObject;\n        libs ("libmyFunctionObject.so");\n    }\n'
    assert result.replace(expected_entry, "", 1) == source
    assert result.count("functions\n{") == 1
    checks.append("existing_native_objects_comments_includes_and_verbatim_code_preserved_byte_for_byte")
    simple = "application icoFoam;\n"
    assert insert_function(simple, "fixture").startswith(simple)
    checks.append("missing_functions_block_created_once")
    quoted = 'description "functions";\nfunctions {}\n'
    assert insert_function(quoted, "fixture").startswith('description "functions";\n')
    checks.append("quoted_scalar_named_functions_is_not_mistaken_for_block")
    for name, text, error_type in (
        ("same_name", 'functions { fixture { type other; } }', FileExistsError),
        ("same_type", 'functions { other { type "myFunctionObject"; } }', FileExistsError),
        ("repeated_integration", result, FileExistsError),
        ("duplicate_blocks", 'functions {} functions {}', ValueError),
        ("unclosed_block", 'functions { existing { type other; }', ValueError),
        ("extra_close", 'functions {} }', ValueError),
        ("indirect_block", 'functions $shared;', ValueError),
    ):
        try:
            insert_function(text, "fixture")
            raise AssertionError(name)
        except error_type:
            checks.append(name+"_rejected_before_write")
    print("PROFESSIONAL_CONTROL_OK "+json.dumps({"passed": True, "count": len(checks), "checks": checks}))


if __name__ == "__main__":
    main()
