"""Actual browser/PTY SQL contention tracing on isolated accepted native history.

Observes real statements and native close/heartbeat calls without recording SQL
parameters or authentication query strings. Optional lock is a real disposable
SQLite writer transaction, not a model, response or stop-receipt substitution.
"""
import sys
import case_workflow_controls_acceptance as controls

TRACE='''
import asyncio,contextvars,json,re,sqlite3,threading,time
from fastapi import HTTPException
from sqlalchemy import event
from app.db import engine
from app.services import terminal_records
from app.services.terminal_sessions import TerminalSession
root=Path.cwd();origin=contextvars.ContextVar('acceptance_sql_origin',default='background')
active_writes={};held={'task':None};trace_path=root/'transaction-trace.jsonl'

def record(kind,**values):
 value={'kind':kind,'monotonic':time.monotonic(),**values}
 with trace_path.open('a',encoding='utf8') as stream:stream.write(json.dumps(value)+'\\n')

class TraceOrigin:
 def __init__(self,app):self.app=app
 async def __call__(self,scope,receive,send):
  token=origin.set(str(scope.get('type'))+' '+str(scope.get('path','')))
  try:await self.app(scope,receive,send)
  finally:origin.reset(token)
app.add_middleware(TraceOrigin)

@event.listens_for(engine.sync_engine,'before_cursor_execute')
def before_sql(conn,cursor,statement,parameters,context,executemany):
 match=re.match(r'\\s*(INSERT(?: OR \\w+)? INTO|UPDATE|DELETE FROM|REPLACE INTO|CREATE TABLE|ALTER TABLE|DROP TABLE)\\s+["`]?([A-Za-z_][A-Za-z0-9_]*)',statement,re.I)
 if match:conn.info['acceptance_attempt']={'started':time.monotonic(),'origin':origin.get(),'verb':match[1].upper(),'table':match[2]}

@event.listens_for(engine.sync_engine,'after_cursor_execute')
def after_sql(conn,cursor,statement,parameters,context,executemany):
 value=conn.info.pop('acceptance_attempt',None)
 if value is None:return
 now=time.monotonic();wait=round((now-value['started'])*1000,3)
 if 'acceptance_writer' not in conn.info:
  conn.info['acceptance_writer']={**value,'acquired':now};active_writes[id(conn)]=conn.info['acceptance_writer']
  record('writer_acquired',connection=id(conn),wait_ms=wait,**value)
 elif wait>=100:record('write_wait',connection=id(conn),wait_ms=wait,**value)

def end_write(conn,ending):
 value=conn.info.pop('acceptance_writer',None);conn.info.pop('acceptance_attempt',None);active_writes.pop(id(conn),None)
 if value is not None:record('writer_end',connection=id(conn),ending=ending,held_ms=round((time.monotonic()-value['acquired'])*1000,3),**value)
event.listen(engine.sync_engine,'commit',lambda conn:end_write(conn,'commit'))
event.listen(engine.sync_engine,'rollback',lambda conn:end_write(conn,'rollback'))

@event.listens_for(engine.sync_engine,'handle_error')
def sql_error(context):
 conn=context.connection;value=conn.info.pop('acceptance_attempt',None) if conn else None
 if value is not None:record('write_error',error=type(context.original_exception).__name__,sqlite_errorcode=getattr(context.original_exception,'sqlite_errorcode',None),wait_ms=round((time.monotonic()-value['started'])*1000,3),**value)

original_heartbeat=terminal_records.heartbeat
async def heartbeat(owner):
 token=origin.set('native.terminal.heartbeat');started=time.monotonic()
 try:
  result=await original_heartbeat(owner);record('terminal_heartbeat',session_id=owner.id,elapsed_ms=round((time.monotonic()-started)*1000,3));return result
 except BaseException as error:
  record('terminal_heartbeat_error',session_id=owner.id,error=type(error).__name__,elapsed_ms=round((time.monotonic()-started)*1000,3));raise
 finally:origin.reset(token)
terminal_records.heartbeat=heartbeat

original_persist=TerminalSession._persist_completion
async def persist(self):
 stop=self._pending_completion[1]['stop'];record('terminal_completion_begin',session_id=self.id,actual_stop_verified=(stop or {}).get('verified'))
 token=origin.set('native.terminal.complete');started=time.monotonic()
 try:
  result=await original_persist(self);record('terminal_completion_end',session_id=self.id,elapsed_ms=round((time.monotonic()-started)*1000,3));return result
 except BaseException as error:
  record('terminal_completion_error',session_id=self.id,error=type(error).__name__,elapsed_ms=round((time.monotonic()-started)*1000,3));raise
 finally:origin.reset(token)
TerminalSession._persist_completion=persist

original_close=TerminalSession._close
async def close(self,*args,**kwargs):
 record('terminal_close_phase',session_id=self.id)
 return await original_close(self,*args,**kwargs)
TerminalSession._close=close

@app.post('/api/v1/test/sql-lock/{operation}')
async def sql_lock(operation:str,user=Depends(get_current_user)):
 if user.id!=1:raise HTTPException(403)
 if operation=='start':
  if held['task'] is not None and not held['task'].done():raise HTTPException(409,'Original SQLite lock still held')
  held.update(ready=threading.Event(),release=threading.Event())
  def hold():
   conn=sqlite3.connect(str(root/'test.sqlite'),timeout=5);started=time.monotonic()
   try:
    conn.execute('BEGIN IMMEDIATE');held['ready'].set();record('explicit_writer_lock_acquired')
    held['release'].wait(60)
   finally:
    conn.rollback();conn.close();record('explicit_writer_lock_released',released_explicitly=held['release'].is_set(),elapsed_seconds=round(time.monotonic()-started,3))
  held['task']=asyncio.create_task(asyncio.to_thread(hold))
  while not held['ready'].is_set() and not held['task'].done():await asyncio.sleep(.01)
  if held['task'].done():held['task'].result()
  return {'actual_writer_lock_held':held['ready'].is_set()}
 if operation=='release':
  if held['task'] is not None:
   held['release'].set();await asyncio.shield(held['task'])
  return {'released':True}
 raise HTTPException(400)

@app.get('/api/v1/test/sql-trace')
async def trace_summary(user=Depends(get_current_user)):
 if user.id!=1:raise HTTPException(403)
 rows=[json.loads(line) for line in trace_path.read_text(encoding='utf8').splitlines()] if trace_path.exists() else []
 writers=sorted([x for x in rows if x['kind']=='writer_end'],key=lambda x:x['held_ms'],reverse=True)[:15]
 return {'actual_trace_rows':len(rows),'longest_writers':writers,'active_writers':list(active_writes.values()),'failures':[x for x in rows if 'error' in x['kind']][-20:],'terminal_events':[x for x in rows if x['kind'].startswith('terminal_')][-40:],'lock_task_done':held['task'].done() if held['task'] is not None else None}
'''
APP=controls.APP+TRACE

def run():
 controls.APP=APP
 return controls.run()

if __name__=='__main__':sys.exit(run())
