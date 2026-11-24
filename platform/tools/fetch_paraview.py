"""Fetch or reuse the pinned official headless ParaView archive for Docker builds."""
import argparse,hashlib,shutil,urllib.request
from pathlib import Path

NAME='ParaView-5.11.2-osmesa-MPI-Linux-Python3.9-x86_64.tar.gz'
URL='https://www.paraview.org/files/v5.11/'+NAME
SHA256='657a44f53c61fb784340b65faa16e91a7b786e802820cf1a6aef35c4d30d4aeb'
DEST=Path(__file__).resolve().parents[1]/'.build-inputs'/NAME

def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as stream:
  while block:=stream.read(1024*1024):h.update(block)
 return h.hexdigest()

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--archive',type=Path,help='Reuse an already downloaded official archive');args=parser.parse_args()
 DEST.parent.mkdir(exist_ok=True)
 if DEST.exists():
  if digest(DEST)!=SHA256:raise ValueError('Existing build input has an unexpected SHA-256; retained for inspection')
 elif args.archive:
  if digest(args.archive)!=SHA256:raise ValueError('Archive SHA-256 mismatch')
  with args.archive.open('rb') as source,DEST.open('xb') as target:shutil.copyfileobj(source,target)
 else:
  part=DEST.with_suffix(DEST.suffix+'.part')
  # A partial file is retained on network failure and resumed on the next invocation.
  offset=part.stat().st_size if part.exists() else 0
  request=urllib.request.Request(URL,headers={'Range':f'bytes={offset}-'} if offset else {})
  with urllib.request.urlopen(request,timeout=120) as response:
   if offset and (response.status!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {offset}-')):
    raise ValueError('Server did not honor the requested resume offset; partial file retained')
   with part.open('ab' if offset else 'xb') as output:shutil.copyfileobj(response,output,1024*1024)
  if digest(part)!=SHA256:raise ValueError('Downloaded archive SHA-256 mismatch; partial file retained')
  part.rename(DEST)
 print(str(DEST));print('SHA256 '+SHA256)

if __name__=='__main__':main()
