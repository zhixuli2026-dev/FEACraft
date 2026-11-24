"""Host-local port reservations, retained until the owned kernel has exited."""
import os
from pathlib import Path
import tempfile


class Reservation:
    def __init__(self,port,handle,closer):self.port=port;self.handle=handle;self.closer=closer
    def close(self):
        if self.handle is not None:
            self.closer(self.handle);self.handle=None


def reserve(port):
    if type(port) is not int or not 1<=port<=65535:raise ValueError('Invalid rendering port')
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];kernel.CreateMutexW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        ctypes.set_last_error(0)
        handle=kernel.CreateMutexW(None,False,f'Local\\VibeFlow-VizPort-{port}');error=ctypes.get_last_error()
        if not handle:raise ctypes.WinError(error)
        if error==183:kernel.CloseHandle(handle);return None
        return Reservation(port,handle,kernel.CloseHandle)
    import fcntl
    folder=Path(tempfile.gettempdir())/'vibeflow-viz-ports';folder.mkdir(mode=0o700,exist_ok=True)
    handle=os.open(folder/str(port),os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:os.close(handle);return None
    except BaseException:os.close(handle);raise
    return Reservation(port,handle,os.close)
