// SPDX-License-Identifier: LGPL-2.1-or-later
#define main feacraft_su2_application_main
#include "SU2_CFD.cpp"
#undef main

extern "C" int feacraft_su2_run(int argc, char **argv)
{
    return feacraft_su2_application_main(argc, argv);
}
