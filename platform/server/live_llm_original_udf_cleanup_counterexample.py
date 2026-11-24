"""Negative control: actual previous cleanup code, original data and OS lock.

Expected engineering failure prevents the shared live wrapper from model calls.
No HTTP/native/model response is fabricated.
"""
import ast,asyncio,hashlib,zipfile
from live_llm_job_repeat_acceptance import main
import live_llm_original_udf_reference_acceptance as reference
from testsupport import SERVER

ARCHIVE=SERVER.parents[1]/'platform/backups/mainline-N3-07-udf-v2-cancel-20261109.zip'
assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()=='cf0a19a93c4686ac6f1a1a4004b47fa57e25e186c27f5b378f106f06d117f7ef'
with zipfile.ZipFile(ARCHIVE) as archive:source=archive.read('platform/server/app/services/private_cache.py')
tree=ast.parse(source);cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Cache')
cleanup=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='cleanup')
assert ast.unparse(cleanup).strip()=='def cleanup(self):\n    remove_owned(self.container, root=self.parent)'
OLD=r'''
from app.services.fs_directory import remove_owned
from app.services import private_cache
'''+ast.unparse(cleanup)+'\nprivate_cache.Cache.cleanup=cleanup\n'
FIXTURE=reference.API_FIXTURE+'\nORIGINAL_SQLITE='+repr(str(reference.DATABASE))+'\n'+OLD+reference.EXTRA

if __name__=='__main__':
    asyncio.run(main(selected_override=reference.scenario,fixture_override=FIXTURE,store_volume_max=16,
        store_factory=reference.restored_store))
