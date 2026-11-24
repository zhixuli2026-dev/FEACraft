"""Actual S3 read views: version changes and cancellation cannot leak snapshots."""
import asyncio
import json
import os
from pathlib import Path
import threading
import time

from s3testsupport import ObjectStore
from testsupport import SERVER


def main():
    checks=[]
    with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
        os.environ.update(store.settings());os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(store.root/'cache')
        from app.config import get_settings
        from app import storage
        get_settings.cache_clear();storage._client=None
        from app.services import case_read_view as views,attempt_objects as objects
        from app.services.fs_directory import remove_owned
        storage._s3_put('case','input.txt',b'original')
        storage._s3_put('case','empty.txt',b'')
        storage._s3_put('case','.vibeflow/jobs/unselected.log',b'metadata')
        parent=store.root/'cache/.case-read-views'
        def no_leak():assert not list(parent.iterdir())
        with views.view('case') as root:
            assert (root/'input.txt').read_bytes()==b'original' and (root/'empty.txt').read_bytes()==b''
            assert not (root/'.vibeflow').exists()
        no_leak();checks.append('real_remote_bytes_empty_objects_and_metadata_exclusion')
        try:
            with views.view('case') as root:
                storage._s3_put('case','added.txt',b'new input')
        except ValueError:pass
        else:raise AssertionError('Changed source membership accepted')
        no_leak();checks.append('source_membership_change_rejected_and_view_removed')
        original=objects.read
        def changed(prefix,path,**kwargs):
            if path=='input.txt':storage._s3_put(prefix,path,b'changed during read')
            return original(prefix,path,**kwargs)
        objects.read=changed
        try:
            try:views.inspect('case',lambda root:None)
            except ValueError:pass
            else:raise AssertionError('Changed conditional input accepted')
        finally:objects.read=original
        no_leak();checks.append('conditional_download_failure_cleans_partial_materialization')
        async def cancellation():
            ready=threading.Event();release=threading.Event()
            def held(prefix,path,**kwargs):
                if path=='input.txt':
                    ready.set()
                    if not release.wait(10):raise TimeoutError('Read view fixture barrier')
                return original(prefix,path,**kwargs)
            async def entering():
                async with views.async_view('case'):
                    raise AssertionError('Cancelled view entered its reader')
            objects.read=held
            task=asyncio.create_task(entering())
            try:
                assert await asyncio.to_thread(ready.wait,10)
                task.cancel();release.set()
                try:await task
                except asyncio.CancelledError:pass
                else:raise AssertionError('Cancellation was swallowed')
            finally:
                release.set();objects.read=original
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            no_leak();checks.append('cancel_during_download_drains_and_closes_entered_context')
            inside=asyncio.Event()
            async def reading():
                async with views.async_view('case') as root:
                    assert (root/'input.txt').exists();inside.set()
                    await asyncio.Event().wait()
            task=asyncio.create_task(reading());await asyncio.wait_for(inside.wait(),10)
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            else:raise AssertionError('Reader cancellation was swallowed')
            no_leak();checks.append('cancel_inside_reader_removes_owned_view')
        asyncio.run(cancellation())
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            create=kernel.CreateFileW
            create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
            create.restype=wintypes.HANDLE
            close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
            def hold_directory(path):
                # A metadata-only handle (access=0) does not prevent deletion.
                handle=create(str(path),1,3,None,3,0x02000000,None)
                assert handle not in (None,ctypes.c_void_p(-1).value),ctypes.get_last_error()
                return handle
            release=None
            try:
                with views.view('case') as root:
                    handle=hold_directory(root)
                    def close_later():time.sleep(.15);assert close(handle)
                    release=threading.Thread(target=close_later);release.start()
            finally:
                if release is not None:release.join(timeout=5);assert not release.is_alive()
            no_leak();checks.append('actual_Windows_directory_handle_release_allows_bounded_cleanup')
            handle=None;blocked=None;started=time.monotonic()
            try:
                try:
                    with views.view('case') as root:
                        blocked=root;handle=hold_directory(root)
                except PermissionError as error:
                    assert error.winerror==32 and time.monotonic()-started<5
                    assert blocked.exists()
                else:raise AssertionError('Held directory cleanup unexpectedly succeeded')
            finally:
                if handle is not None:assert close(handle)
                if blocked is not None:remove_owned(blocked.parent,root=parent)
            no_leak();checks.append('persistent_Windows_directory_handle_fails_without_recursive_retry_or_silent_success')
        # A filesystem view returns the actual source and must never remove it.
        os.environ['VIBEFLOW_STORAGE_BACKEND']='fs';get_settings.cache_clear()
        live=store.root/'cache/local';live.mkdir();(live/'input.txt').write_bytes(b'keep')
        with views.view('local') as root:assert root==live
        assert (live/'input.txt').read_bytes()==b'keep'
        checks.append('fs_view_preserves_original_files')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(store.root)}
    (store.root/'read-view-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('CASE_READ_VIEW_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
