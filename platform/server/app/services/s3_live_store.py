"""Logical case files over immutable objects and a permanent CAS manifest.

The only mutable physical key is HEAD. Every transition has a fresh nonce,
including rollback fences and deletion of an already absent logical file.
Already sent object PUTs can create orphans; a delayed HEAD PUT cannot pass a
newer HEAD's ETag. Private immutable journals retain their existing layout.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import AbstractContextManager, nullcontext
import base64
import hashlib
from bisect import bisect_right
import json
import re
import tempfile
import uuid
from dataclasses import dataclass

from botocore.exceptions import ClientError

DIRECTORY = '.vibeflow/live-store'
HEAD = DIRECTORY + '/head.json'
PENDING = '.vibeflow/file-transactions/pending.json'
SCHEMA = 'vibeflow.live-files.v1'
COMPACT_SCHEMA = 'vibeflow.live-files.v2'
MAX_HEAD = 64 * 1024 * 1024
MAX_FILES = 100000


@dataclass(frozen=True)
class Snapshot:
    bucket:str
    prefix:str
    value:dict
    etag:str
CASE = re.compile(r'^(workspaces/[1-9][0-9]*/cases/(?:[0-9a-f]{32}/)?[1-9][0-9]*)/(.*)$')
PRIVATE = ('attempts', 'attempt-provisions', 'publications', 'file-transactions', 'tree-transactions',
           'uploads', 'live-store', 'job-logs', 'remote-hpc')


def split(key):
    match = CASE.fullmatch(key)
    return match.groups() if match else (None, None)


def managed(path):
    return path == PENDING or not any(path == '.vibeflow/'+name or
        path.startswith('.vibeflow/'+name+'/') for name in PRIVATE)


def visible(entry):
    return entry is not None and not entry.get('deleted', False)


def generation(value):
    return value.get('generation') if value.get('schema') == COMPACT_SCHEMA else None


def fail(code, operation):
    raise ClientError({'Error': {'Code': str(code)},
                       'ResponseMetadata': {'HTTPStatusCode': code}}, operation)


def missing(error):
    return error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404


def condition(args, etag, operation):
    if args.get('IfMatch') is not None and (etag is None or args['IfMatch'].strip('"') != etag.strip('"')):
        fail(412, operation)
    if args.get('IfNoneMatch') is not None:
        value = args['IfNoneMatch']
        if etag is not None and (value == '*' or value.strip('"') == etag.strip('"')):
            fail(412, operation)


class Client:
    """The subset of the S3 client used by the application, with logical keys."""
    def __init__(self, raw):
        from .remote_write_budget import wrap
        self.raw = wrap(raw)
        self.meta = raw.meta
        self._conditional_buckets = set()

    def list_buckets(self, **args):return self.raw.list_buckets(**args)
    def create_bucket(self, **args):return self.raw.create_bucket(**args)
    def close(self):return self.raw.close()

    def _require_conditions(self, bucket, prefix):
        if bucket in self._conditional_buckets:return
        args = {'Bucket':bucket, 'Key':prefix+'/'+DIRECTORY+'/probes/'+uuid.uuid4().hex}
        def denied(**operation):
            try:self.raw.put_object(**args, **operation)
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode'] == 412:return
                raise
            raise ValueError('S3 backend does not enforce live manifest CAS conditions')
        try:
            first = self.raw.put_object(**args, Body=b'first', IfNoneMatch='*')['ETag']
            denied(Body=b'invalid-create', IfNoneMatch='*')
            denied(Body=b'invalid-update', IfMatch='"invalid-version"')
            second = self.raw.put_object(**args, Body=b'second', IfMatch=first)['ETag']
            if not first or not second or second == first:
                raise ValueError('S3 backend returned no distinct conditional version')
            denied(Body=b'stale-update', IfMatch=first)
            self._conditional_buckets.add(bucket)
        finally:
            # This probe is uniquely owned and is never a logical file or head.
            self.raw.delete_object(**args)

    def _load(self, bucket, prefix, *, check=None):
        if check is not None:
            check()
        try:
            response = self.raw.get_object(Bucket=bucket, Key=prefix+'/'+HEAD)
        except ClientError as error:
            if missing(error):return None, None
            raise
        body = response['Body']
        try:
            if response['ContentLength'] > MAX_HEAD:raise ValueError('Live manifest exceeds its limit')
            if check is None:
                encoded = body.read(MAX_HEAD+1)
            else:
                encoded = bytearray()
                while True:
                    check()
                    part = body.read(min(MAX_HEAD + 1 - len(encoded), 256 * 1024))
                    if not part:
                        break
                    encoded.extend(part)
                    if len(encoded) > MAX_HEAD:
                        raise ValueError('Live manifest exceeds its limit')
            if check is not None:
                check()
        finally:body.close()
        if len(encoded) != response['ContentLength'] or not response.get('ETag'):
            raise ValueError('Incomplete live manifest')
        value = json.loads(encoded)
        if (value.get('schema') not in {SCHEMA, COMPACT_SCHEMA} or value.get('bucket') != bucket or value.get('prefix') != prefix
                or str(uuid.UUID(value['nonce'])) != value['nonce'] or not isinstance(value.get('files'), dict)
                or len(value['files']) > MAX_FILES):
            raise ValueError('Live manifest binding is invalid')
        if value['schema'] == COMPACT_SCHEMA:
            if str(uuid.UUID(value['generation'])) != value['generation']:
                raise ValueError('Invalid compacted manifest generation')
        elif 'generation' in value:
            raise ValueError('Legacy manifest cannot declare a compaction generation')
        for path, entry in value['files'].items():
            if check is not None:
                check()
            if (not path or path.startswith('/') or '\\' in path or '..' in path.split('/') or not managed(path)
                    or not isinstance(entry, dict)
                    or entry.get('etag') != '"vf-'+entry.get('id', '')+'"'
                    or str(uuid.UUID(entry['id'])) != entry['id']):
                raise ValueError('Live file reference is invalid')
            if entry.get('deleted') is True:
                if set(entry) != {'id','etag','deleted'}:raise ValueError('Invalid logical tombstone')
            elif (not isinstance(entry.get('key'), str)
                    or not entry['key'].startswith(prefix+'/'+DIRECTORY+'/objects/'+entry['id']+'/')
                    or '..' in entry['key'].split('/') or not entry.get('physical_etag')
                    or type(entry.get('size')) is not int or entry['size'] < 0 or 'deleted' in entry):
                raise ValueError('Live file reference is invalid')
        return value, response['ETag']

    def _save(self, bucket, prefix, value, etag):
        self._require_conditions(bucket, prefix)
        value = dict(value, nonce=str(uuid.uuid4()))
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf8')
        if len(encoded) > MAX_HEAD or len(value['files']) > MAX_FILES:
            raise ValueError('Live manifest exceeds its limit')
        response = self.raw.put_object(Bucket=bucket, Key=prefix+'/'+HEAD, Body=encoded,
            ContentType='application/json', **({'IfMatch':etag} if etag else {'IfNoneMatch':'*'}))
        if not response.get('ETag'):raise ValueError('Live manifest transition returned no version')
        return value, response['ETag']

    def _raw_pages(self, bucket, prefix, *, page_size=None):
        from .storage_listing_budget import ListingBudget
        budget = ListingBudget()
        options = {'PaginationConfig': {'PageSize': page_size}} if page_size is not None else {}
        pages = iter(self.raw.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix, **options))
        while True:
            budget.check()
            try:page = next(pages)
            except StopIteration:break
            budget.check()
            for row in page.get('Contents', []):
                budget.visit()
                yield row

    def _freeze(self, bucket, prefix, path, body, headers, *, expected=None):
        identifier = str(uuid.uuid4())
        key = prefix+'/'+DIRECTORY+'/objects/'+identifier+'/'+path.rsplit('/', 1)[-1]
        response = self.raw.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch='*', **headers)
        info = self.raw.head_object(Bucket=bucket, Key=key)
        if not response.get('ETag') or info.get('ETag') != response['ETag']:
            raise ValueError('Immutable live object version changed')
        entry = {'id':identifier, 'key':key, 'etag':'"vf-'+identifier+'"',
                 'physical_etag':info['ETag'], 'size':info['ContentLength'],
                 'modified':info['LastModified'].isoformat()}
        if expected is not None:
            from .case_write import CaseWriteConflict
            from .s3_streams import object_metadata

            if (entry['size'] != expected['size'] or object_metadata(info) != headers
                    or self._digest_entry(bucket, entry, limit=expected['size']) != expected):
                raise CaseWriteConflict('recovery_evidence_invalid', '上传的恢复文件与已核验的日志不符', path=path)
        return entry

    def _ensure(self, bucket, prefix):
        value, etag = self._load(bucket, prefix)
        if value is not None:return value, etag
        # Fully import the old namespace. Once HEAD exists, absent logical
        # paths never fall back to old keys, including late old-format writes.
        def inventory():
            return {row['Key'][len(prefix)+1:]:(row.get('ETag'), row['Size'])
                for row in self._raw_pages(bucket, prefix+'/')
                if not row['Key'].endswith('/') and managed(row['Key'][len(prefix)+1:])}
        original = inventory()
        if len(original) > MAX_FILES:raise ValueError('Legacy namespace exceeds the live manifest limit')
        files = {}
        from .s3_streams import HEADERS
        for path, (version, size) in original.items():
            if not version:raise ValueError('Legacy object has no version')
            response = self.raw.get_object(Bucket=bucket, Key=prefix+'/'+path, IfMatch=version)
            source = response['Body']
            try:
                if response.get('ETag') != version or response['ContentLength'] != size:
                    raise ValueError('Legacy file changed during migration')
                with tempfile.TemporaryFile() as output:
                    total = 0
                    while block := source.read(1024*1024):
                        total += len(block)
                        if total > size:raise ValueError('Oversized legacy file response')
                        output.write(block)
                    if total != size:raise ValueError('Incomplete legacy file response')
                    output.seek(0)
                    files[path] = self._freeze(bucket, prefix, path, output,
                        {key:response[key] for key in HEADERS if key in response})
                    if files[path]['size'] != size:raise ValueError('Legacy migration copy has a different size')
            finally:source.close()
        if inventory() != original:raise ValueError('Legacy namespace changed during migration')
        try:
            return self._save(bucket, prefix, {'schema':SCHEMA,'bucket':bucket,'prefix':prefix,'files':files}, None)
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode'] != 412:raise
            value, etag = self._load(bucket, prefix)
            if value is None:raise ValueError('Live manifest disappeared during migration')
            return value, etag

    def fence(self, bucket, prefix, *, expected_etag=None):
        if expected_etag is None:
            value, etag = self._ensure(bucket, prefix)
        else:
            value, etag = self._load(bucket, prefix)
            if etag != expected_etag:
                fail(412, 'PutObject')
        # Never retry a failed CAS with a new token: the caller must reconsider
        # SQL ownership and the current journal before another transition.
        return self._save(bucket, prefix, value, etag)[1]

    def _selected(self, args, operation):
        prefix, path = split(args['Key'])
        if prefix is None or not managed(path):return None
        value, _ = self._load(args['Bucket'], prefix)
        if value is None:return None  # Read-only legacy access before migration.
        entry = value['files'].get(path)
        condition(args, entry['etag'] if visible(entry) else None, operation)
        if not visible(entry):fail(404, operation)
        selected = {key:value for key,value in args.items() if key not in {'IfMatch','IfNoneMatch'}}
        selected.update(Key=entry['key'], IfMatch=entry['physical_etag'])
        return entry, selected

    def _change(self, bucket, prefix, value, etag, path, before, after):
        # Independent Job logs can publish while another Job finalizes. A
        # rejected CAS may be retried against a newer manifest only while this
        # exact logical file still has its original identity. This rebase runs
        # in the live caller, never on the object server; a dead caller's queued
        # HTTP PUT therefore remains rejected by the recovery fence.
        original_generation = generation(value)
        for attempt in range(5):
            files = dict(value['files'])
            files[path] = after
            try:return self._save(bucket, prefix, dict(value, files=files), etag)
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode'] != 412 or attempt == 4:raise
                value, etag = self._load(bucket, prefix)
                if value is None or generation(value) != original_generation or value['files'].get(path) != before:raise

    def head_object(self, **args):
        selected = self._selected(args, 'HeadObject')
        if selected is None:return self.raw.head_object(**args)
        entry, physical = selected
        response = self.raw.head_object(**physical)
        if response.get('ETag') != entry['physical_etag'] or response['ContentLength'] != entry['size']:
            raise ValueError('Immutable live object changed')
        return dict(response, ETag=entry['etag'])

    def get_object(self, **args):
        selected = self._selected(args, 'GetObject')
        if selected is None:return self.raw.get_object(**args)
        entry, physical = selected
        response = self.raw.get_object(**physical)
        if (response.get('ETag') != entry['physical_etag'] or
                ('Range' not in args and response['ContentLength'] != entry['size'])):
            response['Body'].close()
            raise ValueError('Immutable live object changed')
        return dict(response, ETag=entry['etag'])

    def put_object(self, **args):
        return self._put_object(args)

    def put_verified_object(self, *, expected, **args):
        """Check a newly frozen body before exposing it through the live catalog."""
        return self._put_object(args, expected=expected)

    def _put_object(self, args, *, expected=None):
        prefix, path = split(args['Key'])
        if prefix is None or not managed(path):
            if expected is not None:
                raise ValueError('Verified writes require a logical case file')
            return self.raw.put_object(**args)
        from .s3_streams import HEADERS
        unknown = set(args)-{'Bucket','Key','Body','IfMatch','IfNoneMatch',*HEADERS}
        if unknown:raise ValueError('Unsupported logical PUT options: '+','.join(sorted(unknown)))
        value, etag = self._ensure(args['Bucket'], prefix)
        old = value['files'].get(path)
        condition(args, old['etag'] if visible(old) else None, 'PutObject')
        options = {} if expected is None else {'expected': expected}
        entry = self._freeze(args['Bucket'], prefix, path, args.get('Body', b''),
                             {key:args[key] for key in HEADERS if key in args}, **options)
        self._change(args['Bucket'], prefix, value, etag, path, old, entry)
        return {'ETag':entry['etag'], 'ResponseMetadata':{'HTTPStatusCode':200}}

    def delete_object(self, **args):
        prefix, path = split(args['Key'])
        if prefix is None or not managed(path):return self.raw.delete_object(**args)
        if set(args)-{'Bucket','Key','IfMatch'}:raise ValueError('Unsupported logical DELETE options')
        value, etag = self._ensure(args['Bucket'], prefix)
        old = value['files'].get(path)
        condition(args, old['etag'] if visible(old) else None, 'DeleteObject')
        identifier=str(uuid.uuid4())
        # Absence has an identity too. Without it, create -> delete -> absent
        # could admit a stale creator during a distinct-file CAS rebase.
        tombstone={'id':identifier,'etag':'"vf-'+identifier+'"','deleted':True}
        self._change(args['Bucket'], prefix, value, etag, path, old, tombstone)
        return {'ResponseMetadata':{'HTTPStatusCode':204}}

    def snapshot(self,bucket,prefix):
        if split(prefix+'/')[0]!=prefix:raise ValueError('Mutable S3 namespace requires explicit migration')
        value,etag=self._ensure(bucket,prefix)
        return Snapshot(bucket,prefix,value,etag)

    def public_snapshot(self,bucket,prefix,*,check=None):
        """Observe a catalog without creating or migrating a legacy namespace."""
        if split(prefix+'/')!=(prefix,''):raise ValueError('Invalid explicit S3 case namespace')
        value,etag=(self._load(bucket,prefix) if check is None else self._load(bucket,prefix,check=check))
        return Snapshot(bucket,prefix,value,etag) if value is not None else None

    def digest_snapshot(self,snapshot,path):
        """Hash the complete pinned body with a bounded read buffer."""
        from .attempt_workspace import MAX_BYTES, canonical_path

        if canonical_path(path)!=path or not managed(path):raise ValueError('Invalid logical snapshot path')
        entry=snapshot.value['files'].get(path)
        return self._digest_entry(snapshot.bucket, entry, limit=MAX_BYTES) if visible(entry) else None

    def _digest_entry(self, bucket, entry, *, limit):
        from .request_deadline import current
        from .s3_streams import _digest_body

        deadline = current()
        if deadline is not None:
            deadline.check()
        if not 0<=entry['size']<=limit:raise ValueError('Snapshot body exceeds its admitted size')
        try:response=self.raw.get_object(Bucket=bucket,Key=entry['key'],IfMatch=entry['physical_etag'])
        except ClientError as error:
            status = error.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            if entry['size'] == 0 and status == 412:
                # Some legacy servers reject a conditional GET of an empty
                # object. The same version and empty length are checked below.
                response = self.raw.get_object(Bucket=bucket, Key=entry['key'])
            elif missing(error) or status == 412:
                raise ValueError('Logical snapshot immutable body changed or disappeared') from error
            else:
                raise
        try:
            if response.get('ETag')!=entry['physical_etag'] or response['ContentLength']!=entry['size']:
                raise ValueError('Logical snapshot body changed')
            return _digest_body(response['Body'], entry['size'], deadline=deadline)
        finally:response['Body'].close()

    def read_snapshot(self,snapshot,path):
        """Read one pinned immutable body without reloading HEAD for each file."""
        from .attempt_workspace import canonical_path,MAX_BYTES
        if canonical_path(path)!=path or not managed(path):raise ValueError('Invalid logical snapshot path')
        entry=snapshot.value['files'].get(path)
        if not visible(entry):return None
        if not 0<=entry['size']<=MAX_BYTES:raise ValueError('Snapshot body exceeds its admitted size')
        try:response=self.raw.get_object(Bucket=snapshot.bucket,Key=entry['key'],IfMatch=entry['physical_etag'])
        except ClientError as error:
            if missing(error):raise ValueError('Logical snapshot references a missing immutable body') from error
            raise
        try:
            if response.get('ETag')!=entry['physical_etag'] or response['ContentLength']!=entry['size']:
                raise ValueError('Logical snapshot body changed')
            data=response['Body'].read(entry['size']+1)
            if len(data)!=entry['size']:raise ValueError('Logical snapshot body length changed')
            return data
        finally:response['Body'].close()

    def matches_snapshot(self,snapshot,paths):
        value,_=self._load(snapshot.bucket,snapshot.prefix)
        return (value is not None and generation(value)==generation(snapshot.value)
                and all(value['files'].get(path)==snapshot.value['files'].get(path) for path in paths))

    def remap_unpublished(self, snapshot, moves):
        """Rename entries of an unpublished restore without transcoding their objects."""
        from .attempt_workspace import canonical_path
        from .request_deadline import current

        files = dict(snapshot.value['files'])
        if len(set(moves.values())) != len(moves):
            raise ValueError('Multiple restored logs share a destination')
        deadline = current()
        for source, target in moves.items():
            if deadline is not None:
                deadline.check()
            if (canonical_path(source) != source or canonical_path(target) != target
                    or not managed(source) or not managed(target) or not visible(files.get(source)) or target in files):
                raise ValueError('Restored log paths differ from the staged catalog')
            files[target] = files.pop(source)
        if moves:
            self._save(snapshot.bucket, snapshot.prefix, dict(snapshot.value, files=files), snapshot.etag)

    def commit_snapshot(self,snapshot,changes,*,watch_paths):
        """Freeze bytes or a managed source sequentially before publishing one file map.

        A live caller may rebase an unrelated HEAD change only while every
        watched file retains its exact identity, including absent tombstones.
        """
        from .attempt_workspace import canonical_path
        watch=set(watch_paths)
        if set(changes)-watch or len(watch)>MAX_FILES:
            raise ValueError('Batch changes exceed their watched namespace')
        for path in watch:
            if canonical_path(path)!=path or not managed(path):raise ValueError('Invalid batch destination')
        value,etag=snapshot.value,snapshot.etag;after={}
        for path,reader in changes.items():
            if reader is None:
                identifier=str(uuid.uuid4());after[path]={'id':identifier,'etag':'"vf-'+identifier+'"','deleted':True}
            else:
                source = reader()
                if isinstance(source, bytes):
                    source = nullcontext(source)
                elif not isinstance(source, AbstractContextManager):
                    raise ValueError('Batch source must return bytes or a managed stream')
                with source as body:
                    after[path]=self._freeze(snapshot.bucket,snapshot.prefix,path,body,{})
        if not after:
            if not self.matches_snapshot(snapshot,watch):fail(412,'BatchPutObject')
            return snapshot
        for attempt in range(5):
            files=dict(value['files']);files.update(after)
            try:
                saved,version=self._save(snapshot.bucket,snapshot.prefix,dict(value,files=files),etag)
                return Snapshot(snapshot.bucket,snapshot.prefix,saved,version)
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode']!=412 or attempt==4:raise
                value,etag=self._load(snapshot.bucket,snapshot.prefix)
                if value is None or generation(value)!=generation(snapshot.value):raise
                # An accepted CAS whose response was lost may be observed on a
                # retry. Unique frozen IDs distinguish it from equal bytes.
                expected={path:after.get(path,snapshot.value['files'].get(path)) for path in watch}
                if all(value['files'].get(path)==expected[path] for path in watch):
                    return Snapshot(snapshot.bucket,snapshot.prefix,value,etag)
                if any(value['files'].get(path)!=snapshot.value['files'].get(path) for path in watch):raise

    def _listing_rows(self,bucket,requested):
        from .storage_listing_budget import ListingBudget
        budget = ListingBudget()
        roots = {}
        prefix, _ = split(requested)
        if prefix:roots[prefix] = self._load(bucket, prefix)[0]
        rows = {}
        for row in self._raw_pages(bucket, requested):
            budget.visit()
            prefix, path = split(row['Key'])
            if prefix:
                if prefix not in roots:roots[prefix] = self._load(bucket, prefix)[0]
                if path == DIRECTORY or path.startswith(DIRECTORY+'/'):continue
                if roots[prefix] is not None and managed(path):continue
            rows[row['Key']] = row
        for prefix, value in roots.items():
            if value is None:continue
            for path, entry in value['files'].items():
                budget.visit()
                if not visible(entry):continue
                key = prefix+'/'+path
                if key.startswith(requested):
                    rows[key] = {'Key':key,'Size':entry['size'],'ETag':entry['etag'],
                                 'LastModified':datetime.fromisoformat(entry['modified'])}
        budget.check()
        return rows

    def public_file_rows(self,bucket,prefix):
        """Read the authoritative public catalog without creating a namespace.

        None denotes a pre-catalog case and requires the ordinary raw listing
        fallback. Private journals are deliberately absent from this result.
        """
        if split(prefix.rstrip('/')+'/')!=(prefix.rstrip('/'),''):return None
        prefix=prefix.rstrip('/');value,_=self._load(bucket,prefix)
        if value is None:return None
        return [{'path':path,'size':entry['size'],'etag':entry['etag'].strip('"')}
            for path,entry in value['files'].items()
            if visible(entry) and path.split('/')[0].rstrip(' .').casefold()!='.vibeflow']

    @staticmethod
    def _listing_page(rows,keys,args):
        if set(args)-{'Bucket','Prefix','MaxKeys','ContinuationToken','StartAfter'}:
            raise ValueError('Unsupported logical listing options')
        after = args.get('StartAfter', '')
        if args.get('ContinuationToken'):
            after = base64.urlsafe_b64decode(args['ContinuationToken'].encode()).decode('utf8')
        maximum = args.get('MaxKeys', 1000)
        if type(maximum) is not int or not 1 <= maximum <= 1000:raise ValueError('Invalid listing page size')
        start=bisect_right(keys,after)
        page=[rows[key] for key in keys[start:start+maximum]]
        result = {'Contents':page,'KeyCount':len(page),'IsTruncated':start+len(page)<len(keys)}
        if result['IsTruncated']:
            result['NextContinuationToken'] = base64.urlsafe_b64encode(page[-1]['Key'].encode()).decode()
        return result

    def list_objects_v2(self, **args):
        # Validate before issuing any remote request.
        self._listing_page({},[],args)
        rows=self._listing_rows(args['Bucket'],args.get('Prefix',''))
        return self._listing_page(rows,sorted(rows),args)

    def get_paginator(self, name):
        if name != 'list_objects_v2':raise ValueError('Unsupported logical paginator')
        client = self
        class Paginator:
            def paginate(self, **args):
                config = args.pop('PaginationConfig', {})
                if set(config)-{'PageSize'}:raise ValueError('Unsupported paginator configuration')
                if 'PageSize' in config:args['MaxKeys'] = config['PageSize']
                client._listing_page({},[],args)
                # Build one merged listing for this iterator. Rebuilding the
                # complete physical inventory for every logical page causes
                # quadratic scans as private attempt/publication history grows.
                rows=client._listing_rows(args['Bucket'],args.get('Prefix',''))
                keys=sorted(rows)
                while True:
                    page = client._listing_page(rows,keys,args)
                    yield page
                    if not page['IsTruncated']:break
                    args['ContinuationToken'] = page['NextContinuationToken']
        return Paginator()

    def generate_presigned_url(self, ClientMethod, Params, **args):
        if ClientMethod != 'get_object':raise ValueError('Unsupported logical presigned operation')
        selected = self._selected(Params, 'GetObject')
        if selected is not None:
            _, Params = selected
            Params.pop('IfMatch', None)  # The unique immutable key pins this download.
        return self.raw.generate_presigned_url(ClientMethod, Params=Params, **args)

    def download_metadata(self, bucket, key, expires):
        """Select one physical object for both the size and the signed download."""
        from .file_read import FileReadConflict
        params = {'Bucket': bucket, 'Key': key}
        try:
            selected = self._selected(params, 'GetObject')
        except ClientError as error:
            if missing(error):
                raise FileNotFoundError(key) from error
            raise
        if selected is not None:
            entry, params = selected
        try:
            head = self.raw.head_object(**params)
        except ClientError as error:
            if selected is not None and error.response['ResponseMetadata']['HTTPStatusCode'] in {404, 412}:
                raise FileReadConflict('Selected download object changed') from error
            if missing(error):
                raise FileNotFoundError(key) from error
            raise
        if selected is not None:
            if head['ContentLength'] != entry['size'] or head.get('ETag') != entry['physical_etag']:
                raise FileReadConflict('Selected download object changed')
            params.pop('IfMatch')  # Browsers can GET this immutable key without custom headers.
        if head.get('VersionId') not in (None, 'null'):
            params['VersionId'] = head['VersionId']
        url = self.raw.generate_presigned_url('get_object', Params=params, ExpiresIn=expires)
        return head['ContentLength'], url


def fence(prefix, *, expected_etag=None):
    """Called only by recovery while the caller owns case control/reservation."""
    from .. import storage
    from ..config import get_settings
    client = storage.s3()
    if isinstance(client, Client):
        if split(prefix+'/')[0] != prefix:
            raise ValueError('S3 case namespace requires an explicit migration before mutable access')
        client.fence(get_settings().s3_bucket, prefix, expected_etag=expected_etag)
