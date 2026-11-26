"""Fetch or reuse the pinned official headless ParaView archive for Docker builds."""
import argparse
import hashlib
import shutil
import urllib.request
from pathlib import Path

ARCHIVE_SHA256 = {
    'osmesa': '657a44f53c61fb784340b65faa16e91a7b786e802820cf1a6aef35c4d30d4aeb',
    'egl': '819cebf5eba5e6555f9ac3114c7e334b32db424ce0cea215f9ce0452d3ed43bb',
}

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=ARCHIVE_SHA256, default='osmesa')
    parser.add_argument('--archive', type=Path, help='Reuse an already downloaded official archive')
    args = parser.parse_args()
    name = f'ParaView-5.11.2-{args.variant}-MPI-Linux-Python3.9-x86_64.tar.gz'
    url = 'https://www.paraview.org/files/v5.11/' + name
    checksum = ARCHIVE_SHA256[args.variant]
    dest = Path(__file__).resolve().parents[1] / '.build-inputs' / name
    dest.parent.mkdir(exist_ok=True)
    if dest.exists():
        if digest(dest) != checksum:
            raise ValueError('Existing build input has an unexpected SHA-256; retained for inspection')
    elif args.archive:
        if digest(args.archive) != checksum:
            raise ValueError('Archive SHA-256 mismatch')
        with args.archive.open('rb') as source, dest.open('xb') as target:
            shutil.copyfileobj(source, target)
    else:
        part = dest.with_suffix(dest.suffix + '.part')
        # Resume a partial download when the server confirms its range.
        offset = part.stat().st_size if part.exists() else 0
        headers = {'Range': f'bytes={offset}-'} if offset else {}
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            if offset and (response.status != 206 or
                           not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-')):
                raise ValueError('Server did not honor the requested resume offset; partial file retained')
            with part.open('ab' if offset else 'wb') as target:
                shutil.copyfileobj(response, target, 1024 * 1024)
        if digest(part) != checksum:
            raise ValueError('Downloaded archive SHA-256 mismatch; partial file retained')
        part.rename(dest)
    print(str(dest))
    print('SHA256 ' + checksum)


if __name__ == '__main__':
    main()
