"""Real Windows directory handles: bounded cleanup, no LLM/native inference."""
import ctypes,json,os,tempfile,threading,time
from ctypes import wintypes
from pathlib import Path
from app.services import fs_directory

def main():
 assert os.name=='nt'
 repository=Path(__file__).resolve().parents[2];parent=(repository/'tmp').resolve()
 root=Path(tempfile.mkdtemp(prefix='owned-directory-sharing-',dir=parent)).resolve()
 assert root.parent==parent
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
 kernel.CreateFileW.restype=wintypes.HANDLE
 kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
 def held(path):
  handle=kernel.CreateFileW(str(path),1,3,None,3,0x02000000,None)
  if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
  return handle
 sentinel=root/'outside-owned-containers.txt';sentinel.write_text('RETAIN_THIS_SIBLING')
 container=root/'transient';child=container/'data/C-run';child.mkdir(parents=True)
 handle=held(child);actual_errors=[]
 import shutil
 original=shutil.rmtree
 def observed(*args,**kwargs):
  try:return original(*args,**kwargs)
  except OSError as error:actual_errors.append(getattr(error,'winerror',None));raise
 shutil.rmtree=observed
 release=threading.Thread(target=lambda:(time.sleep(1.1),kernel.CloseHandle(handle)))
 release.start();started=time.monotonic()
 try:fs_directory.remove_owned(container,root=root)
 finally:release.join();shutil.rmtree=original
 elapsed=time.monotonic()-started
 assert not container.exists() and elapsed>=1 and actual_errors and set(actual_errors)<={32,33}
 permanent=root/'held';(permanent/'data/C-run').mkdir(parents=True);handle=held(permanent/'data/C-run')
 started=time.monotonic()
 try:
  try:fs_directory.remove_owned(permanent,root=root)
  except OSError as error:assert getattr(error,'winerror',None) in {32,33}
  else:raise AssertionError('A genuinely held directory was deleted')
  blocked_seconds=time.monotonic()-started;assert 2.8<=blocked_seconds<5 and permanent.exists()
 finally:kernel.CloseHandle(handle)
 fs_directory.remove_owned(permanent,root=root)
 assert sentinel.read_text()=='RETAIN_THIS_SIBLING'
 try:fs_directory.remove_owned(root,root=root)
 except ValueError:pass
 else:raise AssertionError('Storage root deletion accepted')
 report={'passed':True,'evidence_kind':'actual_Windows_handles_and_filesystem','llm_calls':0,'mock_llm_calls':0,
  'transient_lock_released_seconds':1.1,'actual_retry_errors':actual_errors,'cleanup_seconds':round(elapsed,4),
  'permanent_lock_refused_seconds':round(blocked_seconds,4),'held_directory_preserved_until_release':True,
  'unrelated_sibling_unchanged':True,'root_delete_refused':True,'root':str(root)}
 (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 print(json.dumps(report))
if __name__=='__main__':main()
