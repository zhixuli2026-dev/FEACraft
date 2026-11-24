"""Build a symmetric rectangular wing and run a headless VSPAERO sweep."""

import argparse
import csv
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distribution', required=True, type=Path, help='OpenVSP Windows Python 3.11 distribution')
    parser.add_argument('--output', type=Path, default=Path('wing-results'))
    args = parser.parse_args()
    installed = args.distribution.resolve(strict=True)
    if not (installed / 'vspaero.exe').is_file():
        parser.error('The distribution must contain the native VSPAERO executable')

    sys.path[:0] = [str(installed / 'python' / package) for package in ('openvsp', 'openvsp_config')]
    import openvsp_config
    openvsp_config._IGNORE_IMPORTS = True
    import openvsp as vsp

    work = args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    os.chdir(work)
    if not vsp.SetVSPAEROPath(str(installed)):
        raise RuntimeError('OpenVSP could not locate VSPAERO')

    wing = vsp.AddGeom('WING', '')
    vsp.SetGeomName(wing, 'VerificationWing')
    vsp.SetDriverGroup(wing, 1, vsp.SPAN_WSECT_DRIVER, vsp.ROOTC_WSECT_DRIVER, vsp.TIPC_WSECT_DRIVER)
    for name, value in [('Span', 5.0), ('Root_Chord', 1.0), ('Tip_Chord', 1.0), ('Sweep', 0.0)]:
        vsp.SetParmVal(wing, name, 'XSec_1', value)
    surface = vsp.GetXSecSurf(wing, 0)
    for index in range(vsp.GetNumXSec(surface)):
        section = vsp.GetXSec(surface, index)
        vsp.SetParmVal(vsp.GetXSecParm(section, 'Camber'), 0.0)
    vsp.Update()
    model = str(work / 'VerificationWing.vsp3')
    vsp.WriteVSPFile(model, vsp.SET_ALL)
    vsp.ClearVSPModel()
    vsp.ReadVSPFile(model)
    wing, = vsp.FindGeoms()
    vsp.Update()

    geometry = 'VSPAEROComputeGeometry'
    sweep = 'VSPAEROSweep'
    for analysis in (geometry, sweep):
        vsp.SetAnalysisInputDefaults(analysis)
        vsp.SetIntAnalysisInput(analysis, 'GeomSet', [vsp.SET_NONE])
        vsp.SetIntAnalysisInput(analysis, 'ThinGeomSet', [vsp.SET_ALL])
    if not vsp.ExecAnalysis(geometry):
        raise RuntimeError('VSPAERO geometry failed')

    vsp.SetIntAnalysisInput(sweep, 'NCPU', [1])
    vsp.SetDoubleAnalysisInput(sweep, 'AlphaStart', [0.0])
    vsp.SetDoubleAnalysisInput(sweep, 'AlphaEnd', [6.0])
    vsp.SetIntAnalysisInput(sweep, 'AlphaNpts', [3])
    vsp.SetIntAnalysisInput(sweep, 'RefFlag', [1])
    vsp.SetStringAnalysisInput(sweep, 'WingID', [wing])
    if not vsp.ExecAnalysis(sweep):
        raise RuntimeError('VSPAERO sweep failed')

    result = vsp.FindLatestResultsID('VSPAERO_Polar')
    names = set(vsp.GetAllDataNames(result))
    lift = 'CL' if 'CL' in names else 'CLtot'
    alphas = list(vsp.GetDoubleResults(result, 'Alpha'))
    coefficients = list(vsp.GetDoubleResults(result, lift))
    if not alphas or len(alphas) != len(coefficients):
        raise RuntimeError('VSPAERO returned no complete lift polar')
    with (work / 'wing-polar.csv').open('w', encoding='ascii', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('alpha_deg', 'CL'))
        writer.writerows(zip(alphas, coefficients))
    (work / 'wing-summary.json').write_text(json.dumps({
        'version': vsp.GetVSPVersion(), 'alpha_deg': alphas, 'lift_coefficient': coefficients,
    }, indent=2) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
