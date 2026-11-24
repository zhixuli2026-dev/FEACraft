"""Prepare the official pinned Linux x64 Node archive for an offline Docker COPY."""
import argparse,hashlib,shutil,urllib.request
from pathlib import Path

VERSION='24.22.3'
NAME=f'node-v{VERSION}-linux-x64.tar.xz'
URL=f'https://nodejs.org/dist/v{VERSION}/{NAME}'
# Published in https://nodejs.org/dist/v24.22.3/SHASUMS256.txt
SHA256='4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb'
DEST=Path(__file__).resolve().parents[1]/'.build-inputs'/NAME

def digest(path):
 with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def main():
 p=argparse.ArgumentParser();p.add_argument('--archive',type=Path);args=p.parse_args()
 DEST.parent.mkdir(exist_ok=True)
 if DEST.exists():
  if digest(DEST)!=SHA256:raise ValueError('Existing Node archive hash mismatch; retained for inspection')
 else:
  part=DEST.with_suffix(DEST.suffix+'.part')
  if args.archive:
   if digest(args.archive)!=SHA256:raise ValueError('Node archive hash mismatch')
   with args.archive.open('rb') as source,part.open('xb') as target:shutil.copyfileobj(source,target)
  else:
   with urllib.request.urlopen(URL,timeout=120) as source,part.open('xb') as target:shutil.copyfileobj(source,target)
  if digest(part)!=SHA256:raise ValueError('Downloaded Node hash mismatch; partial file retained')
  part.rename(DEST)
 print(str(DEST));print('SHA256 '+SHA256)

if __name__=='__main__':main()
