"""Test-only file-backed conditional S3 client for native session fault suites."""
from datetime import datetime, timezone
import hashlib
import io
import mimetypes
import threading
from botocore.exceptions import ClientError


def install():
    from app import storage
    class Client:
        def __init__(self):
            from types import SimpleNamespace
            self.meta=SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts':1}))
            self.metadata = {}
            self.lock = threading.RLock()

        def error(self, code):
            raise ClientError({'Error': {'Code': str(code)}, 'ResponseMetadata': {'HTTPStatusCode': code}}, 'FixtureS3')

        def current(self, key):
            path = storage._fs_path('', key)
            if not path.exists():
                return None, None
            data = path.read_bytes()
            return data, '"' + hashlib.sha256(data).hexdigest() + '"'

        def check(self, args, etag):
            if (args.get('IfNoneMatch') == '*' and etag is not None or
                    args.get('IfMatch') is not None and args['IfMatch'].strip('"') != (etag or '').strip('"')):
                self.error(412)

        def head_object(self, **args):
            with self.lock:
                data, etag = self.current(args['Key'])
                if data is None:self.error(404)
                self.check(args, etag)
                return {'ContentLength': len(data), 'ETag': etag,
                        'LastModified': datetime.now(timezone.utc),
                        **self.metadata.get(args['Key'], {'ContentType': mimetypes.guess_type(args['Key'])[0] or 'application/octet-stream'})}

        def get_object(self, **args):
            with self.lock:
                head = self.head_object(**args)
                return {**head, 'Body': io.BytesIO(self.current(args['Key'])[0])}

        def put_object(self, **args):
            with self.lock:
                self.check(args, self.current(args['Key'])[1])
                body = args['Body']; data = body.read() if hasattr(body, 'read') else body
                storage._fs_put('', args['Key'], data)
                self.metadata[args['Key']] = {key: args[key] for key in
                    ('ContentType', 'CacheControl', 'ContentDisposition', 'ContentEncoding', 'ContentLanguage', 'Metadata') if key in args}
                self.metadata[args['Key']].setdefault('ContentType', 'application/octet-stream')
                return {'ETag': self.current(args['Key'])[1]}

        def delete_object(self, **args):
            with self.lock:
                self.check(args, self.current(args['Key'])[1])
                storage._fs_delete('', args['Key'])
                self.metadata.pop(args['Key'], None)
                return {}

    from app.services.remote_write_budget import wrap
    client = wrap(Client())
    storage.s3 = lambda: client
    def listing(prefix, subdir=''):
        entries = storage._fs_list(prefix, subdir)
        for entry in entries:
            entry['etag'] = client.current(storage._s3_key(prefix, entry['path']))[1].strip('"')
        return entries
    storage._s3_list = listing
    return client
