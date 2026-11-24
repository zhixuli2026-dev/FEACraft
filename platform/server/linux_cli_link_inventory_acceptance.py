"""Actual Linux filesystem checks for private CLI link accounting. No LLM."""
import json,os,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from app.services.storage_usage import Counter,MeasurementUnavailable,_scan_fs_tree

assert os.name!='nt'
root=Path(tempfile.mkdtemp(prefix='cli-link-inventory-'));tree=root/'private';tree.mkdir()
outside=root/'outside';outside.mkdir();(outside/'payload').write_bytes(b'x'*65536)
(tree/'regular').write_bytes(b'abc')
(tree/'file-link').symlink_to(outside/'payload');(tree/'directory-link').symlink_to(outside,target_is_directory=True)
(tree/'dangling-link').symlink_to(root/'absent')
settings=SimpleNamespace(storage_fs_root=str(root),storage_measurement_max_entries=100)
def scan(*,parts=('private',),links=True,limit=None):
 c=Counter(settings,time.monotonic()+10,limit);_scan_fs_tree(settings,parts,c,link_bytes=links);return c
def rejected(operation):
 try:operation()
 except MeasurementUnavailable:return
 raise AssertionError('Expected actual filesystem rejection')
expected=3+sum((tree/name).lstat().st_size for name in ('file-link','directory-link','dangling-link'))
c=scan();assert c.bytes==expected and c.entries==4
checks=['file_directory_and_dangling_links_charged_without_following_targets']
rejected(lambda:scan(links=False));checks.append('public_default_still_rejects_links')
(root/'redirected-root').symlink_to(tree,target_is_directory=True)
rejected(lambda:scan(parts=('redirected-root',)));checks.append('selected_root_symlink_rejected')
os.mkfifo(tree/'special-fifo');rejected(scan);(tree/'special-fifo').unlink();checks.append('special_file_rejected')
settings.storage_measurement_max_entries=2;rejected(scan);checks.append('entry_budget_still_enforced')
assert (outside/'payload').stat().st_size==65536
print(json.dumps({'passed':True,'checks':checks,'link_tree_bytes':expected,'external_bytes_not_charged':65536,'real_model_calls':0,'mock_llm_calls':0}))
