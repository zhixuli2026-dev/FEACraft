"""Export a source-built MOOSE package with its installed /opt dependencies for Linux acceptance."""

import argparse
import os
from pathlib import Path
import subprocess
import tarfile


def archive_runtime(package, output):
    executable = package/'bin/combined-opt'
    subprocess.run(['sha256sum', '--quiet', '-c', 'SHA256SUMS'], cwd=package, check=True)
    result = subprocess.check_output(['ldd', str(executable)], text=True)
    if '=> not found' in result:
        raise ValueError('MOOSE has unresolved runtime libraries')
    paths = {Path(os.path.normpath(line.split(' => ')[1].split()[0]))
             for line in result.splitlines() if ' => ' in line}
    system = ('/lib', '/lib64', '/usr/lib', '/usr/local/lib')
    bundled = sorted(path for path in paths if path.is_relative_to('/opt') and not path.is_relative_to(package))
    if any(not path.is_relative_to(package) and path not in bundled
           and not any(path.is_relative_to(prefix) for prefix in system) for path in paths):
        raise ValueError('MOOSE acceptance requires external libraries in /opt or the base Linux image')
    with tarfile.open(output, 'x:gz', compresslevel=1, dereference=True) as archive:
        archive.add(package, 'package')
        directory = tarfile.TarInfo('opt')
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        for library in bundled:
            archive.add(library, library.as_posix().removeprefix('/'))
    print('MOOSE runtime libraries exported:', len(bundled))


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('archive',type=Path)
    args = parser.parse_args()
    archive_runtime(args.package.resolve(strict=True),args.archive)
