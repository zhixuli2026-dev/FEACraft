"""Carry selected portable modes as a hash-verified member of sealed inputs."""
import json

from .. import storage
from . import tree_metadata_native as metadata


def read_local(root):
    path = metadata.checked(root, metadata.PATH)
    if not path.exists():return None
    with path.open('rb') as stream:data = stream.read(metadata.MAX_BYTES+1)
    metadata.decode(data)
    return data


def read_remote(prefix):
    if not storage.exists(prefix, metadata.PATH):return None
    if storage.file_size(prefix, metadata.PATH)>metadata.MAX_BYTES:
        raise ValueError('Oversized portable mode descriptor')
    data=storage.get_bytes(prefix, metadata.PATH)
    metadata.decode(data)
    return data


def directories(raw, paths, all_live):
    if raw is None:return []
    return sorted(path for path, entry in metadata.decode(raw)['entries'].items()
        if entry['kind']=='directory' and (all_live or any(
            path==p or path.startswith(p+'/') or p.startswith(path+'/') for p in paths)))


def selected(raw, records, directories, *, verify_hashes=True):
    if raw is None or metadata.PATH in records:return None
    saved=metadata.decode(raw)['entries'];entries={}
    for path,entry in saved.items():
        if entry['kind']=='directory':
            if path in directories:entries[path]=dict(entry)
        else:
            record=records.get(path)
            if record is not None and record['size']==entry['size'] and (not verify_hashes or record['sha256']==entry['sha256']):
                entries[path]=dict(entry)
    if not entries:return None
    # Parents of any retained entry are present in the validated descriptor and
    # the selected input directory inventory. No unselected file is introduced.
    document={'schema':metadata.SCHEMA,'materialize_directories':True,'entries':entries}
    metadata.validate(document)
    encoded=json.dumps(document,sort_keys=True,separators=(',',':')).encode()
    if len(encoded)>metadata.MAX_BYTES:raise ValueError('Oversized selected mode descriptor')
    return encoded
