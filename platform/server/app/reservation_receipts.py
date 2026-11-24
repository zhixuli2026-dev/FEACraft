"""Lossless inline storage for settled reservation history."""
import base64
import hashlib
import json
import zlib

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


FORMAT = 'vibeflow.receipt.v1'
PREFIX = '["' + FORMAT + '",'
MAX_RECEIPT_BYTES = 128 * 1024
MAX_ENVELOPE = 2 * MAX_RECEIPT_BYTES
# Fixed format vocabulary only. Keep this dictionary with the encoding version.
DICTIONARY = (
    b'{"etag":"","schema":"vibeflow.remote-write-receipt.v1","status":200,"version_id":""}'
    b'{"etag":"","schema":"vibeflow.remote-write-receipt.v1","sha256":"","status":"verified_immutable"}'
    b'{"binding":{"backend":"s3","bucket":"","endpoint":"http://127.0.0.1:","prefix":"workspaces/"},'
    b'"closed":true,"id":"","nonce":"","pending":null,"schema":"vibeflow.owner-capacity.v1","size":0,"used":0}'
    b'"immutable_receipt":{},"upload_chunk_receipt":{},"status":412,"version_id":null'
)


def unpack(value):
    if value is None or not value.startswith(PREFIX):
        return value
    if len(value) > MAX_ENVELOPE:
        raise ValueError('Oversized compressed reservation receipt')
    try:
        version, size, encoded = json.loads(value)
        if version != FORMAT or type(size) is not int or not 0 < size <= MAX_RECEIPT_BYTES:
            raise ValueError('Compressed reservation receipt metadata differs')
        envelope = base64.b85decode(encoded)
        digest, compressed = envelope[:32], envelope[32:]
        decoder = zlib.decompressobj(zdict=DICTIONARY)
        raw = decoder.decompress(compressed, size + 1)
        if (len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or hashlib.sha256(raw).digest() != digest):
            raise ValueError('Compressed reservation receipt failed integrity verification')
        return raw.decode('utf-8')
    except (TypeError, UnicodeError, zlib.error) as error:
        raise ValueError('Invalid compressed reservation receipt') from error


def pack(value):
    raw = value.encode('utf-8')
    if len(raw) > MAX_RECEIPT_BYTES:
        return None
    compressor = zlib.compressobj(zdict=DICTIONARY)
    compressed = compressor.compress(raw) + compressor.flush()
    encoded = base64.b85encode(hashlib.sha256(raw).digest() + compressed).decode('ascii')
    result = json.dumps([FORMAT, len(raw), encoded], separators=(',', ':'))
    if len(result) >= len(raw):
        return None
    if unpack(result) != value:
        raise ValueError('Compressed reservation receipt did not round-trip')
    return result


class ReservationReceipt(TypeDecorator):
    """Keep the database column and the ORM's original JSON text contract."""
    impl = Text
    cache_ok = True

    def process_result_value(self, value, dialect):
        return unpack(value)
