"""Actual HTTP body/auth waits before the image service, followed by commit cases."""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import h11
from sqlalchemy import select,text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import DurableEvent,NativeRequest
from sandbox_request_deadline_checks import FIXTURE as SERVICE_FIXTURE,after_retirement as service_cases


FIXTURE=SERVICE_FIXTURE+'''
from app.models import User as _User
_user_get=AsyncSession.get
async def _actual_user_get(self,entity,*args,**kwargs):
 selected=_selected_http()
 scoped=_deadline.current() is not None and entity is _User
 if scoped:_http_mark('auth_lock')
 value=await _user_get(self,entity,*args,**kwargs)
 if scoped:await _http_pause('auth_read',actual_user_found=value is not None)
 return value
AsyncSession.get=_actual_user_get
_actual_clean=_cleanup._clean
async def _entered_clean(sid,rid,user_id,budget):
 if _selected_http():
  Path('http-service-entered.json').write_text(json.dumps({'deadline':budget.deadline,'remaining':budget.remaining()}))
 return await _actual_clean(sid,rid,user_id,budget)
_cleanup._clean=_entered_clean
'''


class WireRequest:
    """A real TCP request that can receive a response before its body is complete."""
    async def open(self,api,path,body):
        url=urlsplit(api.base)
        self.reader,self.writer=await asyncio.open_connection(url.hostname,url.port)
        self.connection=h11.Connection(h11.CLIENT)
        headers=[('Host',url.netloc),('Authorization','Bearer '+api.token),
            ('Content-Type','application/json'),('Content-Length',str(len(body))),('Connection','close')]
        self.writer.write(self.connection.send(h11.Request(method='POST',target=path,headers=headers)))
        await self.writer.drain()
        self.result=asyncio.create_task(self.response())
        return self

    async def send(self,data,*,complete=False):
        self.writer.write(self.connection.send(h11.Data(data=data)))
        if complete:self.writer.write(self.connection.send(h11.EndOfMessage()))
        await self.writer.drain()

    async def response(self):
        status=None;headers={};body=bytearray();starts=0
        async with asyncio.timeout(120):
            while True:
                event=self.connection.next_event()
                if event is h11.NEED_DATA:
                    chunk=await self.reader.read(16384)
                    self.connection.receive_data(chunk)
                elif isinstance(event,h11.Response):
                    starts+=1;status=event.status_code;headers=dict(event.headers)
                elif isinstance(event,h11.Data):
                    body.extend(event.data);assert len(body)<1024*1024
                elif isinstance(event,h11.EndOfMessage):
                    assert starts==1
                    assert headers.get(b'x-content-type-options')==b'nosniff'
                    return {'status':status,'body':json.loads(body),'response_starts':starts,'received':time.monotonic()}
                elif isinstance(event,h11.ConnectionClosed):
                    raise RuntimeError('Actual socket closed without a complete response')

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()
        if not self.result.done():self.result.cancel()
        await asyncio.gather(self.result,return_exceptions=True)


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'mock_llm_calls':0,'checks':[],'cases':[],
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    wire=None;holder=None;transaction=None
    async def state():
        async with engine.connect() as db:
            requests=[tuple(row) for row in await db.execute(select(NativeRequest.id,NativeRequest.phase,
                NativeRequest.children_json,NativeRequest.result_json).where(NativeRequest.case_id==api.case_id).order_by(NativeRequest.id))]
            events=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.case_id==api.case_id,
                DurableEvent.type.in_(['terminal.image_cleanup_started','terminal.image_cleanup_completed'])).order_by(DurableEvent.id)))
            return requests,events
    async def marker(name):
        async with asyncio.timeout(15):
            while not (api.root/name).exists():
                assert not wire.result.done(),await wire.result if wire.result.done() else name
                await asyncio.sleep(.01)
        return json.loads((api.root/name).read_text())
    def record(name):
        report['checks'].append({'name':name,'kind':'actual_infrastructure'})
        print('INGRESS_DEADLINE_PASS '+name,flush=True)
    try:
        original=await state()
        async with engine.connect() as db:
            selected=json.loads(await db.scalar(select(NativeRequest.profile_json).where(NativeRequest.id==rid)))
        image=Path(selected['image_root'])/'volume.ext4'
        archive=Path(selected['root'])/'recovery.zip';retained=archive.read_bytes()
        endpoint='/api/v1/terminal/sessions/'+sid+'/image-cleanup'
        body=json.dumps({'recovery_id':rid,'confirm':True}).encode()
        for stage in ('body','auth_read','archive',*(('auth_lock',) if api.database_url else ())):
            for name in ('http-deadline-start.json','http-deadline-paused.json','http-deadline-resume','http-service-entered.json'):
                (api.root/name).unlink(missing_ok=True)
            seconds=8 if stage=='archive' else 3
            (api.root/'http-deadline-case.json').write_text(json.dumps({'stage':stage,'seconds':seconds}))
            (api.root/'http-deadline-armed').write_text('one actual ingress wait')
            if stage=='auth_lock':
                holder=await engine.connect();transaction=await holder.begin()
                await holder.execute(text('LOCK TABLE users IN ACCESS EXCLUSIVE MODE'))
            wire=await WireRequest().open(api,endpoint,body)
            await wire.send(body[:1] if stage in {'body','archive'} else body,complete=stage not in {'body','archive'})
            clock=await marker('http-deadline-start.json')
            case={'stage':stage,'seconds':seconds,'clock':clock};report['cases'].append(case)
            if stage=='body':
                await asyncio.sleep(1)
                assert not wire.result.done()
                await wire.send(body[1:2])
            elif stage in {'auth_read','auth_lock'}:
                case['pause']=await marker('http-deadline-paused.json')
                if stage=='auth_read':assert case['pause']['actual_user_found']
            else:
                await asyncio.sleep(max(0,clock['deadline']-time.monotonic()-2.5))
                await wire.send(body[1:],complete=True)
                case['service']=await marker('http-service-entered.json')
                assert case['service']['deadline']==clock['deadline']
                assert 0<case['service']['remaining']<3
                await marker('http-deadline-paused.json')
                await asyncio.sleep(max(0,clock['deadline']-time.monotonic()+.3))
                assert not wire.result.done(),'Protected actual archive callback was abandoned'
                (api.root/'http-deadline-resume').write_text('release completed archive callback')
            response=await wire.result;case['response']=response
            assert response['status']==409 and response['body']['detail']['code']=='image_cleanup_incomplete'
            assert response['received']>=clock['deadline']
            assert response['received']-clock['deadline']<5
            if stage!='archive':assert not (api.root/'http-service-entered.json').exists()
            if transaction is not None:
                await transaction.rollback();await holder.close();transaction=None;holder=None
            await wire.close();wire=None
            assert await state()==original
            assert image.is_file() and archive.read_bytes()==retained
            (api.root/'http-deadline-case.json').unlink()
            (api.root/'http-deadline-armed').unlink(missing_ok=True)
            record(stage+'_deadline_before_cleanup_side_effects')
        # The route must retain the normal framework authorization/validation behavior.
        await asyncio.to_thread(api.call,'POST',endpoint,body=json.loads(body),expect=401,api_prefix=False,
            extra_headers={'Authorization':''})
        await asyncio.to_thread(api.call,'POST',endpoint,body={'recovery_id':rid,'confirm':False},expect=422,api_prefix=False)
        assert await state()==original
        record('normal_authorization_and_body_validation_retained')
        report['llm_calls']=await service_cases(api,sid,rid,proof,continuation=continuation)
        report['passed']=True
        return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]}
        raise
    finally:
        (api.root/'http-deadline-resume').write_text('drain protected operation')
        if transaction is not None:await transaction.rollback()
        if holder is not None:await holder.close()
        if wire is not None:await wire.close()
        await engine.dispose()
        (api.root/'ingress-deadline-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
