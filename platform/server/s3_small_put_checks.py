"""Verify signed conditional writes and the small-body handshake on a real disposable S3 endpoint."""
import hashlib
import io
import json
import os
import tempfile
import time
import uuid

from botocore.exceptions import ClientError

from s3testsupport import ObjectStore, SERVER


def main():
    store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed')
    print(store.root, flush=True)
    with store:
        os.environ.update(store.settings())
        from app import storage
        storage.get_settings.cache_clear()
        client = storage.s3()
        sent = []

        def observe(request, **_):
            sent.append({'expect':bool(request.headers.get('Expect')),
                         'conditional':bool(request.headers.get('If-None-Match'))})
        client.meta.events.register('before-send.s3.PutObject', observe)
        results = []
        with tempfile.TemporaryFile(dir=store.root) as file:
            file.write(b'X')
            file.seek(0)
            for label, content, body, expect in [
                ('empty', b'', b'', False),
                ('buffered', b'A'*65536, io.BytesIO(b'A'*65536), False),
                ('large', b'B'*65537, b'B'*65537, True),
                ('file', b'X', file, True)
            ]:
                key = {'Bucket':store.bucket, 'Key':'transport/'+uuid.uuid4().hex}
                started = time.monotonic()
                assert client.put_object(**key, Body=body, IfNoneMatch='*')['ETag']
                elapsed = round(1000*(time.monotonic()-started))
                assert sent[-1]=={'expect':expect, 'conditional':True}, (label, sent[-1])
                try:
                    client.put_object(**key, Body=b'different', IfNoneMatch='*')
                except ClientError as error:
                    assert error.response['ResponseMetadata']['HTTPStatusCode']==412
                else:
                    raise AssertionError('Conditional write replaced an existing test object')
                with client.get_object(**key)['Body'] as stream:
                    assert stream.read()==content
                client.delete_object(**key)
                results.append(dict(kind=label, bytes=len(content), expect_continue=expect, elapsed_ms=elapsed,
                                    conflict_http=412, sha256=hashlib.sha256(content).hexdigest()))
        client.close()
        (store.root/'small-put.json').write_text(json.dumps(results, indent=2), encoding='utf8')


if __name__=='__main__':
    main()
