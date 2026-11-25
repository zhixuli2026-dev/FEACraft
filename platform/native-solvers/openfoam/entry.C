// SPDX-License-Identifier: GPL-3.0-or-later
// Compile the upstream application's entry point into a callable library.
#define main feacraft_upstream_main
#include FEACRAFT_OPENFOAM_APPLICATION
#undef main

extern "C" int feacraft_openfoam_run(int argc, char** argv)
{
    return feacraft_upstream_main(argc, argv);
}
