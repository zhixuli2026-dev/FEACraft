"""Actual loopback native-channel framing faults; no provider/model inference."""
import asyncio,base64,json,socket,subprocess,threading,time
from app.services.native_execution_client import context,run_native

def _case(mode):
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(5)
    port=listener.getsockname()[1];errors=[];observed={};stopped=threading.Event()
    payload=b'native diagnostic bytes\n'
    output=(json.dumps({'type':'output','data':base64.b64encode(payload).decode()})+'\n').encode()
    def serve():
        try:
            with listener.accept()[0] as channel:
                channel.settimeout(5);raw=b''
                while not raw.endswith(b'\n'):raw+=channel.recv(4096)
                request=json.loads(raw);observed['deadline_supplied']='deadline' in request
                if mode=='fragmented_success':
                    raw=output+(json.dumps({'type':'done','returncode':0,'child_id':'protocol-only'})+'\n').encode()
                    for offset in range(0,len(raw),3):channel.sendall(raw[offset:offset+3]);time.sleep(.001)
                else:
                    until=time.monotonic()+3
                    while time.monotonic()<until and not stopped.is_set():
                        channel.sendall(output if mode=='continuous_output' else b' ')
                        time.sleep(.04)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        except BaseException as error:errors.append(type(error).__name__)
        finally:listener.close()
    worker=threading.Thread(target=serve);worker.start();started=time.monotonic();expired=False;result=None
    try:
        with context('127.0.0.1:'+str(port),'a'*64):
            try:result=run_native('native_protocol_deadline',{},timeout=.6 if mode!='fragmented_success' else 3,capture=True)
            except subprocess.TimeoutExpired:expired=True
        elapsed=time.monotonic()-started
    finally:
        stopped.set();worker.join(6)
    assert not worker.is_alive() and not errors,errors
    if mode=='fragmented_success':assert not expired and result.returncode==0 and result.stdout==payload
    else:assert expired and .45<=elapsed<1.8,(mode,expired,elapsed)
    assert observed['deadline_supplied'],'Client did not carry its absolute deadline'
    return {'name':mode,'kind':'protocol_fault' if mode!='fragmented_success' else 'protocol',
        'elapsed':elapsed,'deadline_expired':expired,'actual_loopback_socket':True}

async def verify():
    return [await asyncio.to_thread(_case,mode) for mode in ('continuous_output','partial_line','fragmented_success')]
