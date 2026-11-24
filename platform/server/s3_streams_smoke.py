"""Resource bounds and version conflicts at the S3 streaming read boundary."""
import hashlib
import io
from unittest.mock import patch

from botocore.exceptions import ClientError
from app.services import s3_streams as stream
from app.services.case_write import CaseWriteConflict


class Body(io.BytesIO):
    def read(self, size=-1):
        assert 0 <= size <= stream.CHUNK_BYTES, 'Unbounded object body allocation'
        return super().read(size)


class Client:
    def __init__(self, data, mode='normal'):
        self.data,self.mode,self.body=data,mode,None
    def head_object(self, **kwargs):
        if self.mode=='missing':raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404},'Error':{'Code':'NoSuchKey'}},'HeadObject')
        return {'ContentLength':len(self.data),'ETag':'"selected"','ContentType':'application/test'}
    def get_object(self, **kwargs):
        assert kwargs['IfMatch']=='"selected"'
        if self.mode in {'deleted','replaced'}:
            raise ClientError({'ResponseMetadata':{'HTTPStatusCode':404 if self.mode=='deleted' else 412},'Error':{'Code':'Changed'}},'GetObject')
        body=self.data[:-1] if self.mode=='short' else self.data+b'extra' if self.mode=='oversized' else self.data
        self.body=Body(body)
        return {'ContentLength':len(self.data),'ETag':'"foreign"' if self.mode=='ignored_condition' else '"selected"','Body':self.body}


def check(data, mode='normal', *, output=None, rewind=True, limit=10*1024*1024):
    client=Client(data,mode)
    with patch.object(stream.storage,'s3',return_value=client),patch.object(stream,'arguments',return_value={'Bucket':'test','Key':'owned'}):
        try:
            return stream.snapshot('prefix','owned',output=output,rewind=rewind,limit=limit)
        finally:
            assert client.body is None or client.body.closed


data=b'bytes'*(stream.CHUNK_BYTES//2)
out=io.BytesIO()
record,etag,headers=check(data,output=out)
assert record=={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} and etag=='"selected"'
assert out.tell()==0 and out.read()==data and headers['ContentType']=='application/test'
out=io.BytesIO(b'prefix');out.seek(6)
check(b'next',output=out,rewind=False)
assert out.getvalue()==b'prefixnext' and out.tell()==10
assert check(b'',mode='missing')==(None,None,None)
assert check(b'')[0]['size']==0
for mode in ('deleted','replaced','ignored_condition','short','oversized'):
    try:check(b'content',mode)
    except (ValueError,CaseWriteConflict):pass
    else:raise AssertionError(mode)
try:check(b'abcd',limit=3)
except ValueError:pass
else:raise AssertionError('Oversized HEAD was admitted')
print('S3_STREAMS_OK 10 bounded read, append, missing/empty and conflict checks')
