"""Detached database reads with an exact, replayable publication read set.

Each query uses its own short session. File validators run after that session
closes. Rechecking these observations requires the caller's case control lock
and a still-current content reservation; this is not a substitute for either.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

from sqlalchemy import inspect
from sqlalchemy.sql import Select

from ..models import ArtifactPublication, ArtifactPublicationHead, Job, JobAttempt
from .durable_events import json_text
from .publication_inputs import PublicationInputStale

MODELS = (ArtifactPublication, ArtifactPublicationHead, Job, JobAttempt)


def encoded(value):
    if isinstance(value, datetime):
        return {'datetime':value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None
                else value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, (tuple, list)):
        return [encoded(item) for item in value]
    if isinstance(value, dict):
        return {key:encoded(item) for key,item in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    if type(value) in MODELS:
        return {'model':type(value).__tablename__, 'columns':{
            attr.key:encoded(getattr(value,attr.key)) for attr in inspect(type(value)).column_attrs}}
    raise TypeError('Unsupported publication database observation')


def digest(value):
    return hashlib.sha256(json_text(encoded(value)).encode('utf8')).hexdigest()


@dataclass(frozen=True)
class Read:
    operation: str
    query: object
    identity: object
    value_sha256: str

    def key(self):
        if self.operation == 'get':
            return json_text(['get',self.query.__tablename__,encoded(self.identity)])
        compiled=self.query.compile()
        return json_text(['scalar',str(compiled),encoded(compiled.params)])

    async def execute(self, db):
        if self.operation == 'get':
            return await db.get(self.query,self.identity,populate_existing=True)
        query=self.query.execution_options(populate_existing=True)
        if self.operation == 'scalars':
            return tuple((await db.scalars(query)).all())
        return await db.scalar(query)


class PublicationReadSet:
    """Only the read operations used by registered publication validators.

    No write/flush/commit API, ambient transaction, lazy ORM loading or arbitrary
    scalar SQL. Adding a new validator read requires reviewing this interface.
    """
    def __init__(self):
        self.reads={}
        self.binding=None

    def fingerprint(self):
        return digest(sorted((read.key(),read.value_sha256) for read in self.reads.values()))

    async def _read(self, read):
        if self.binding is not None:
            raise ValueError('Publication read set is already sealed')
        from ..db import SessionLocal
        async with SessionLocal() as db:
            value=await read.execute(db)
            observed=Read(read.operation,read.query,read.identity,digest(value))
            # Only fully loaded column data crosses the session boundary.
            db.expunge_all()
        key=observed.key()
        previous=self.reads.get(key)
        if previous is not None and previous.value_sha256 != observed.value_sha256:
            raise PublicationInputStale('发布依赖在读取期间发生变化')
        self.reads[key]=observed
        return value

    async def get(self, model, identity, *, populate_existing=False):
        if model not in MODELS:
            raise TypeError('Unsupported publication model read')
        if isinstance(identity,list):
            identity=tuple(identity)
        if not isinstance(identity,(str,int,tuple)):
            raise TypeError('Publication identity must be immutable')
        return await self._read(Read('get',model,identity,''))

    async def _select(self, operation, statement):
        if not isinstance(statement,Select) or len(statement.column_descriptions)!=1:
            raise TypeError('Publication validation only supports ORM SELECT reads')
        description=statement.column_descriptions[0]
        if description['entity'] not in MODELS or description['expr'] is not description['entity']:
            raise TypeError('Publication validation must read a complete known entity')
        return await self._read(Read(operation,statement,None,''))

    async def scalar(self, statement):
        return await self._select('scalar', statement)

    async def scalars(self, statement):
        return await self._select('scalars', statement)

    def seal(self):
        if self.binding is not None:
            raise ValueError('Publication read set cannot be resealed')
        self.binding=self.fingerprint()

    async def require_current(self, db):
        if self.binding is None or self.binding != self.fingerprint():
            raise PublicationInputStale('发布依赖读取证明缺失或发生变化')
        for read in self.reads.values():
            if digest(await read.execute(db)) != read.value_sha256:
                raise PublicationInputStale('发布依赖的数据库归属或版本已变化')
