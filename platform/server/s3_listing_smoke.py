"""Actual S3 listings retain case-relative paths, matching filesystem listings."""
import json
import os

from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME


def main():
    checks=[]
    with ObjectStore(executable=RUNTIME) as store:
        os.environ.update(store.settings());os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(store.root/'files')
        from app.config import get_settings
        from app import storage
        get_settings.cache_clear();storage._client=None
        data={'root.txt':b'root','C-run/thermal/system/controlDict':b'control',
              'C-run/thermal/0/T':b'initial','C-run/thermal/1/T':b'result',
              'C-run/thermal-extra/1/T':b'other run','E-report/build/report.html':b'report'}
        for path,body in data.items():
            storage._s3_put('case',path,body)
            storage._fs_put('case',path,body)
        storage._s3_put('case-neighbor','C-run/thermal/1/T',b'other case')
        storage._s3_put('case','C-run/thermal/',b'')
        storage._s3_put('case','C-run/thermal/empty/',b'')
        # Force multiple list pages without requiring hundreds of objects.
        client=storage.s3()
        class Paginated:
            def get_paginator(self,name):
                delegate=client.get_paginator(name)
                class Pages:
                    def paginate(self,**kwargs):
                        return delegate.paginate(**kwargs,PaginationConfig={'PageSize':2})
                return Pages()
            def __getattr__(self,name):return getattr(client,name)
        storage._client=Paginated()
        for subdir in ('','.','C-run/thermal','C-run/thermal/','C-run\\thermal','E-report/build','missing'):
            remote=storage.list_tree('case',subdir)
            local=storage._fs_list('case',subdir)
            assert [(row['path'],row['size']) for row in remote]==[(row['path'],row['size']) for row in local],subdir
            assert all(storage.get_bytes('case',row['path'])==data[row['path']] for row in remote)
        checks.append('paginated_root_and_subdirectory_paths_match_fs_and_roundtrip_reads')
        assert [row['path'] for row in storage.list_tree('case','C-run/thermal')]==[
            'C-run/thermal/0/T','C-run/thermal/1/T','C-run/thermal/system/controlDict']
        checks.append('case_and_run_boundaries_exclude_neighbors_and_directory_markers')
        assert [row['path'] for row in storage._s3_list('case','C-run/thermal')]==[
            '0/T','1/T','system/controlDict']
        checks.append('private_attempt_inventory_listing_remains_prefix_relative')
        try:storage.list_tree('case','../neighbor')
        except ValueError:pass
        else:raise AssertionError('Traversal accepted')
        checks.append('traversal_still_rejected')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(store.root)}
    (store.root/'listing-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_LISTING_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
