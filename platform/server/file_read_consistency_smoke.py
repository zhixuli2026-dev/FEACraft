"""Isolated HTTP downloads with deterministic replacement between range reads."""
import argparse
from contextlib import nullcontext
import io
import json
from unittest.mock import patch

import httpx

from testsupport import IsolatedServer, SERVER


FAULTS = '''
from app.main import app
from app import storage
from app.services.file_read import FileRead
original_init, original_chunks = FileRead.__init__, FileRead.chunks
def initialize(self, prefix, path):
    original_init(self, prefix, path)
    self.test_prefix, self.test_path, self.test_reads = prefix, path, 0
def chunks(self, start=None, end=None):
    path = self.test_path
    self.test_reads += 1
    def replace():
        (storage._s3_put if self.remote else storage._fs_put)(self.test_prefix, path, b'new version')
    if self.test_reads == 1 and path.endswith('race-before.bin'):
        replace()
    yield from original_chunks(self, start, end)
    if self.test_reads == 1:
        if path.endswith(('race-between.bin', 'race-after.bin')):
            replace()
        elif path.endswith('race-delete.bin'):
            (storage._s3_delete if self.remote else storage._fs_delete)(self.test_prefix, path)
        elif path.endswith('race-inplace.bin') and not self.remote:
            storage._fs_path(self.test_prefix, path).write_bytes(b'X' * 256)
FileRead.__init__, FileRead.chunks = initialize, chunks
'''


def protocol_checks():
    """Stores that ignore preconditions/ranges must not produce mixed 206s."""
    from types import SimpleNamespace
    from botocore.exceptions import ClientError
    from app.services.file_read import FileRead, FileReadConflict
    checks = []
    client = SimpleNamespace(head_object=lambda **_: {'ETag': '"old"', 'ContentLength': 8})
    with patch('app.storage._use_s3', return_value=True), patch('app.storage.s3', return_value=client):
        for label, metadata, body in [
            ('ignored_if_match', {'ETag': '"new"', 'ContentLength': 2, 'ContentRange': 'bytes 0-1/8'}, b'XY'),
            ('ignored_range', {'ETag': '"old"', 'ContentLength': 8}, b'12345678'),
            ('wrong_range', {'ETag': '"old"', 'ContentLength': 2, 'ContentRange': 'bytes 2-3/8'}, b'34'),
            ('short_body', {'ETag': '"old"', 'ContentLength': 2, 'ContentRange': 'bytes 0-1/8'}, b'1'),
        ]:
            stream = io.BytesIO(body)
            def get(**params):
                assert params['IfMatch'] == '"old"' and params['Range'] == 'bytes=0-1'
                return {**metadata, 'Body': stream}
            client.get_object = get
            try:
                list(FileRead('test', 'payload').chunks(0, 1))
            except FileReadConflict:
                pass
            else:
                raise AssertionError(label)
            assert stream.closed
            checks.append(label)
        def forbidden(**_):
            raise ClientError({'Error': {'Code': 'AccessDenied'},
                               'ResponseMetadata': {'HTTPStatusCode': 403}}, 'HeadObject')
        client.head_object = forbidden
        try:
            FileRead('test', 'payload')
        except ClientError as error:
            assert error.response['Error']['Code'] == 'AccessDenied'
        else:
            raise AssertionError('Storage authorization failure hidden as missing file')
        checks.append('storage_access_denied_is_not_404')
        client.head_object = lambda **_: {'ContentLength': 8}
        try:
            FileRead('test', 'payload')
        except FileReadConflict:
            pass
        else:
            raise AssertionError('Missing read precondition accepted')
        checks.append('missing_etag_refused')
    return checks


def run(store=None):
    api = IsolatedServer('file-read-consistency', app='file_read_fixture:app')
    settings = store.settings() if store else {}
    (api.root/'file_read_fixture.py').write_text(
        'import os\nos.environ.update('+repr(settings)+')\n'+FAULTS, encoding='utf8')
    checks = []
    with api:
        api.create_case('Download consistency')
        with httpx.Client(base_url=api.base+'/api/v1', trust_env=False, timeout=30,
                          headers={'Authorization': 'Bearer '+api.token}) as client:
            payload = bytes(range(256))
            def upload(name, data=payload):
                path = 'Ref/'+name+'.bin'
                response = client.post(api.prefix+'/upload', params={'path': path},
                    files={'file': (name+'.bin', data, 'application/octet-stream')})
                assert response.status_code == 200, response.text
                return path
            path = upload('normal')
            def download(path=path, method='GET', span=None, version=None):
                return client.request(method, api.prefix+'/raw', params={'path': path},
                    headers={**({'Range': span} if span else {}),
                             **({'X-Vibeflow-File-Version': version} if version is not None else {})})
            for span, data, content_range in [
                ('bytes=0-99', payload[:100], 'bytes 0-99/256'),
                ('bytes=100-', payload[100:], 'bytes 100-255/256'),
                ('bytes=-16', payload[-16:], 'bytes 240-255/256'),
                ('bytes=0-999', payload, 'bytes 0-255/256'),
            ]:
                response = download(span=span)
                assert response.status_code == 206 and response.content == data
                assert response.headers['content-range'] == content_range
                assert int(response.headers['content-length']) == len(data)
            checks.append('single_open_suffix_clamped_ranges')
            for span in ('bytes=256-', 'bytes=20-10', 'not-a-range', 'bytes=0-5,4-8'):
                response = download(span=span)
                assert response.status_code == 416 and response.headers['content-range'] == 'bytes */256'
            checks.append('invalid_and_overlapping_ranges')
            response = download()
            assert response.status_code == 200 and response.content == payload
            assert response.headers['content-length'] == '256'
            checks.append('full_get')
            response = download(span='bytes=0-3,250-255')
            assert response.status_code == 206
            assert b'Content-Range: bytes 0-3/256\r\n\r\n'+payload[:4]+b'\r\n' in response.content
            assert b'Content-Range: bytes 250-255/256\r\n\r\n'+payload[250:]+b'\r\n' in response.content
            multipart_length = len(response.content)
            assert int(response.headers['content-length']) == multipart_length
            checks.append('multipart_exact_bytes_and_length')
            for span, length, status in [(None, 256, 200), ('bytes=10-19', 10, 206),
                                         ('bytes=0-3,250-255', multipart_length, 206)]:
                response = download(method='HEAD', span=span)
                assert response.status_code == status and response.content == b''
                assert int(response.headers['content-length']) == length
            checks.append('head_full_single_multipart')
            versioned = upload('versioned')
            initial = download(versioned, method='HEAD')
            version = initial.headers['x-vibeflow-file-version']
            assert len(version) == 64 and initial.headers['cache-control'] == 'no-store'
            for span, data in [('bytes=0-3', payload[:4]), ('bytes=250-255', payload[250:])]:
                response = download(versioned, span=span, version=version)
                assert response.status_code == 206 and response.content == data
                assert response.headers['x-vibeflow-file-version'] == version
            checks.append('head_version_pins_multiple_requests')
            assert download(path, span='bytes=0-3', version=version).status_code == 409
            assert download(versioned, version='invalid').status_code == 422
            checks.append('different_object_and_malformed_versions_refused')
            upload('versioned', payload[::-1])
            assert download(versioned, span='bytes=250-255', version=version).status_code == 409
            assert download(versioned, method='HEAD', version=version).status_code == 409
            latest = download(versioned, method='HEAD').headers['x-vibeflow-file-version']
            assert latest != version
            assert download(versioned, span='bytes=250-255', version=latest).content == payload[::-1][250:]
            checks.append('equal_length_replacement_refuses_old_version_and_new_head_recovers')
            response = client.delete(api.prefix+'/file', params={'path': versioned})
            assert response.status_code == 200, response.text
            assert download(versioned, version=latest).status_code == 409
            assert download(versioned).status_code == 404
            checks.append('deleted_pinned_file_is_conflict_not_new_content')
            peer = IsolatedServer('file-read-peer', app='file_read_peer:app')
            peer_settings = {**settings, **{key: api.env[key] for key in
                ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_JWT_SECRET')}}
            if not store:
                peer_settings['VIBEFLOW_STORAGE_FS_ROOT'] = api.env['VIBEFLOW_STORAGE_FS_ROOT']
            (peer.root/'file_read_peer.py').write_text('import os\nos.environ.update('+repr(peer_settings)
                +')\nfrom app.main import app\n', encoding='utf8')
            stable = download(method='HEAD').headers['x-vibeflow-file-version']
            with peer:
                response = httpx.get(peer.base+'/api/v1'+api.prefix+'/raw', params={'path':path},
                    headers={'Authorization':'Bearer '+api.token, 'Range':'bytes=0-3',
                             'X-Vibeflow-File-Version':stable}, trust_env=False, timeout=30)
                assert response.status_code == 206 and response.content == payload[:4]
                assert response.headers['x-vibeflow-file-version'] == stable
            checks.append('fresh_API_accepts_same_storage_version')
            for name, span in [('race-before', None), ('race-between', 'bytes=0-3,250-255'),
                               ('race-delete', 'bytes=0-3,250-255')]:
                response = download(upload(name), span=span)
                assert response.status_code == 409, (name, response.status_code, response.text)
                assert response.json()['detail']['code'] == 'file_changed_during_read'
                assert 'content-range' not in response.headers
                checks.append(name+'_409_without_partial_response')
            if not store:
                response = download(upload('race-inplace'), span='bytes=0-3,250-255')
                assert response.status_code == 409, response.text
                checks.append('same_size_inplace_change_refused')
            response = download(upload('race-after'))
            assert response.status_code == 200 and response.content == payload
            assert response.headers['content-length'] == '256'
            checks.append('completed_old_version_survives_later_replacement')
            assert download('Ref/missing.bin').status_code == 404
            assert download('../outside').status_code == 422
            checks.append('missing_and_traversal')
            response = client.get(api.prefix+'/raw', params={'path': path}, headers={'Authorization': ''})
            assert response.status_code in (401, 403)
            old_prefix = api.prefix
            api.create_case('Different owner')
            client.headers['Authorization'] = 'Bearer '+api.token
            response = client.get(old_prefix+'/raw', params={'path': path})
            assert response.status_code in (403, 404)
            checks.append('authentication_and_foreign_owner')
        result = {'backend': 's3' if store else 'fs', 'checks': checks,
                  'protocol_checks': protocol_checks(), 'api_evidence': str(api.root),
                  'peer_evidence': str(peer.root)}
        (api.root/'download-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        print('FILE_READ_OK '+json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--s3', action='store_true')
    args = parser.parse_args()
    if args.s3:
        from s3testsupport import ObjectStore
        context = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed')
    else:
        context = nullcontext(None)
    with context as store:
        run(store)
