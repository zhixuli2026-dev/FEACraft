"""Poll configured API nodes and deliver durable alert transitions."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

from . import monitoring_store as store
from .monitor_export import LOG_CAPACITY


LABEL = re.compile(r'[A-Za-z0-9_.-]{1,80}')
ROUTE = re.compile(r'/[A-Za-z0-9_/.{}:-]{1,200}')
BOOT = re.compile(r'[a-f0-9]{32}')
MAX_EXPORT = 512 * 1024


@dataclass(frozen=True)
class Node:
    name: str
    cluster: str
    origin: str


def endpoint(value, *, webhook=False):
    parts = urlsplit(value)
    if (parts.scheme not in ('http','https') or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or '\\' in value
            or any(ord(char)<32 for char in value)):
        raise ValueError('Invalid monitoring endpoint')
    if webhook:
        if parts.scheme != 'https' and parts.hostname not in {'127.0.0.1','::1','localhost'}:
            raise ValueError('Alert delivery requires HTTPS or a loopback test endpoint')
    elif parts.path not in ('','/'):
        raise ValueError('A monitoring node must be configured by origin')
    return value.rstrip('/') if not webhook else value


def nodes_from(value):
    configured = json.loads(value)
    if not isinstance(configured, dict) or not 1 <= len(configured) <= 32:
        raise ValueError('Configure between one and 32 monitoring nodes')
    result = []
    for name, fields in configured.items():
        if not isinstance(fields, dict) or not LABEL.fullmatch(name):
            raise ValueError('Invalid monitoring node')
        cluster = fields.get('cluster', name)
        if not isinstance(cluster,str) or not LABEL.fullmatch(cluster):
            raise ValueError('Invalid monitoring database group')
        result.append(Node(name, cluster, endpoint(fields['origin'])))
    return tuple(result)


def _number(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value < 10**12:
        raise ValueError('Invalid monitoring measurement')
    return value


def _count(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError('Invalid monitoring counter')
    return value


def _counts(value):
    if not isinstance(value,dict) or len(value)>128 or any(not LABEL.fullmatch(key) for key in value):
        raise ValueError('Invalid monitoring counter labels')
    return {key:_count(count) for key,count in value.items()}


def _probe(value):
    if not isinstance(value,dict) or type(value.get('ok')) is not bool:
        raise ValueError('Invalid connectivity probe')
    return {'ok':value['ok'],'latency_ms':_number(value['latency_ms'])}


def clean_export(document):
    if document['schema'] != 'vibeflow.telemetry.v1':
        raise ValueError('Unknown monitoring export')
    snapshot=document['sample']
    alerts=[]
    for row in snapshot['alerts']:
        if row['level'] not in ('warning','critical') or not LABEL.fullmatch(row['code']):
            raise ValueError('Invalid monitoring alert')
        alerts.append({'level':row['level'],'code':row['code']})
    sample={'reachable':True,'database':_probe(snapshot['database']),'storage':_probe(snapshot['storage']),
            'jobs':_counts(snapshot['jobs']),'processes':_counts(snapshot['processes']),
            'leases':_counts(snapshot['job_lease_states']),'alerts':alerts}
    source=document['logs']
    if not BOOT.fullmatch(source['boot']) or len(source['records'])>LOG_CAPACITY:
        raise ValueError('Invalid monitoring log page')
    records=[]
    for row in source['records']:
        if not ROUTE.fullmatch(row['route']) or not re.fullmatch(r'[A-Z]{2,8}',row['method']):
            raise ValueError('Invalid HTTP log metadata')
        seq=_count(row['seq'])
        status=_count(row['status'])
        if not 100<=status<=599 or (records and seq!=records[-1]['seq']+1):
            raise ValueError('Invalid HTTP log sequence')
        records.append({'seq':seq,'status':status,'time':_number(row['time']),'method':row['method'],
                        'route':row['route'],'latency_ms':_number(row['latency_ms'])})
    after=_count(source['after'])
    if records and records[-1]['seq'] != after:
        raise ValueError('Invalid monitoring log cursor')
    return sample,{'boot':source['boot'],'after':after,'lost':_count(source['lost']),'records':records}


async def fetch(client,node,token,cursor):
    try:
        body=bytearray()
        async with client.stream('GET', node.origin+'/api/v1/runtime/telemetry',
                                 headers={'X-VibeFlow-Telemetry-Token':token,'Accept-Encoding':'identity'},
                                 params={'boot':cursor[0],'after':cursor[1]}) as response:
            response.raise_for_status()
            if response.headers.get('content-encoding','identity')!='identity':
                raise ValueError('Monitoring export uses an unexpected encoding')
            async for chunk in response.aiter_raw(65536):
                body.extend(chunk)
                if len(body)>MAX_EXPORT:
                    raise ValueError('Monitoring export too large')
        sample,log=clean_export(json.loads(body))
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        sample={'reachable':False,'error':type(error).__name__,'alerts':[{'level':'critical','code':'peer.unreachable'}]}
        log=None
    return node,sample,log


async def collect_once(path,nodes,token,webhook='',delivery_token='',retention_seconds=7*86400):
    cursors=await asyncio.to_thread(store.cursors,path)
    async with httpx.AsyncClient(timeout=httpx.Timeout(8,connect=3),follow_redirects=False,trust_env=False) as client:
        results=await asyncio.gather(*(fetch(client,node,token,cursors.get(node.name,('',0))) for node in nodes))
        for node,sample,log in results:
            await asyncio.to_thread(store.ingest,path,node.name,node.cluster,sample,log)
        if webhook:
            for identifier,payload,attempts in await asyncio.to_thread(store.next_deliveries,path,time.time()):
                headers={'Idempotency-Key':identifier}
                if delivery_token:
                    headers['Authorization']='Bearer '+delivery_token
                try:
                    async with client.stream('POST',webhook,json=payload,headers=headers) as response:
                        success=200<=response.status_code<300
                except httpx.RequestError:
                    success=False
                await asyncio.to_thread(store.record_delivery,path,identifier,time.time(),attempts,success)
    await asyncio.to_thread(store.prune,path,time.time()-retention_seconds)


async def run(once):
    path=Path(os.environ['VIBEFLOW_MONITORING_DB_PATH']).resolve()
    nodes=nodes_from(os.environ['VIBEFLOW_MONITORING_NODES'])
    token=os.environ['VIBEFLOW_MONITORING_EXPORT_TOKEN']
    if not token:
        raise ValueError('A monitoring export token is required')
    webhook=os.getenv('VIBEFLOW_MONITORING_WEBHOOK','')
    if webhook:
        webhook=endpoint(webhook, webhook=True)
    delivery_token=os.getenv('VIBEFLOW_MONITORING_WEBHOOK_TOKEN','')
    interval=float(os.getenv('VIBEFLOW_MONITORING_INTERVAL','15'))
    if not math.isfinite(interval) or not 1<=interval<=600:
        raise ValueError('Monitoring interval must be between one and 600 seconds')
    store.initialize(path)
    while True:
        await collect_once(path,nodes,token,webhook,delivery_token)
        if once:
            return
        await asyncio.sleep(interval)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    asyncio.run(run(parser.parse_args().once))
