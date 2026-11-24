"""Fixed, read-only Linux metadata inventory with an owned host bootstrap.

No command selection, file contents, child creation, or filesystem writes in
the native half. A kernel alarm and owner-pipe EOF bound its lifetime.
"""
import asyncio,base64,hashlib,importlib.util,json,math,os,re,signal,stat,subprocess,sys,threading,time
from pathlib import Path

SCHEMA='vibeflow.native-inventory.v1'
FILES=('cli_native_inventory_reader.py','process_identity_native.py','codex_wsl_state.py')
LIMIT=8192
STATUSES={'complete','lower_bound','deadline','entry_limit','redirected','changed','special','missing','unavailable'}

def module(name):
    spec=importlib.util.spec_from_file_location('inventory_'+name,Path(__file__).with_name(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

def validate(value):
    if (not isinstance(value,dict) or set(value)!={'schema','nonce','distribution','root','user_id','max_entries','stop_after','seconds','helpers'}
            or value['schema']!=SCHEMA or not isinstance(value['nonce'],str) or not re.fullmatch('[0-9a-f]{64}',value['nonce'])
            or not isinstance(value['distribution'],str) or not re.fullmatch('[a-z0-9-]+',value['distribution'])
            or value['user_id'] is not None and (type(value['user_id']) is not int or value['user_id']<=0)
            or type(value['max_entries']) is not int or value['max_entries']<0
            or value['stop_after'] is not None and (type(value['stop_after']) is not int or value['stop_after']<0)
            or type(value['seconds']) not in (float,int) or not math.isfinite(value['seconds']) or not 0<value['seconds']<=10):
        raise ValueError('Invalid fixed inventory request')
    helpers=value['helpers']
    if not isinstance(helpers,dict) or set(helpers)!=set(FILES):raise ValueError('Invalid inventory helpers')
    for name,digest in helpers.items():
        if digest!=hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest():raise ValueError('Inventory helper changed')
    module('codex_wsl_state').native_root(value['root'])
    return value

class Stop(Exception):pass

def native(request):
    signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.alarm(11)
    def owner_pipe():
        try:os.read(sys.stdin.fileno(),1)
        finally:os._exit(125)
    threading.Thread(target=owner_pipe,daemon=True).start()
    validate(request)
    if os.getuid()!=65534:raise ValueError('Inventory requires the native worker identity')
    reader=module('process_identity_native').observe(os.getpid())
    deadline=time.monotonic()+request['seconds'];entries=0;size=0;root=Path(request['root']);root_inode=None
    def check():
        if time.monotonic()>=deadline:raise Stop('deadline')
    def ordinary(path):
        check();info=path.lstat()
        if not stat.S_ISDIR(info.st_mode):raise Stop('redirected')
        return info
    def same(path,expected):
        info=ordinary(path)
        if (info.st_dev,info.st_ino)!=expected:raise Stop('changed')
    def charge(amount):
        nonlocal entries,size
        check();entries+=1
        if entries>request['max_entries']:raise Stop('entry_limit')
        size+=amount
        if request['stop_after'] is not None and size>request['stop_after']:raise Stop('lower_bound')
    status='complete'
    try:
        info=ordinary(root);root_identity=(info.st_dev,info.st_ino);root_inode=info.st_ino
        if root.resolve(strict=True)!=root:raise Stop('redirected')
        current=root;present=True
        for part in (() if request['user_id'] is None else ('users',str(request['user_id']))):
            current=current/part
            try:ordinary(current)
            except FileNotFoundError:present=False;break
        if present:
            info=ordinary(current);initial=(info.st_dev,info.st_ino);stack=[(current,initial)]
            while stack:
                directory,expected=stack.pop();same(directory,expected)
                with os.scandir(directory) as rows:
                    for entry in rows:
                        check();info=entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):charge(info.st_size)
                        elif stat.S_ISDIR(info.st_mode):
                            charge(0);stack.append((Path(entry.path),(info.st_dev,info.st_ino)))
                        else:raise Stop('special')
                same(directory,expected)
            same(current,initial)
        same(root,root_identity)
        if root.resolve(strict=True)!=root:raise Stop('redirected')
        check()
    except Stop as error:status=str(error)
    except FileNotFoundError:status='missing'
    except OSError:status='unavailable'
    return {'schema':SCHEMA,'nonce':request['nonce'],'reader':reader,'status':status,
        'root_inode':root_inode,'entries':entries,'bytes':size}

async def bounded(stream,limit):
    data=bytearray()
    while chunk:=await stream.read(min(4096,limit+1-len(data))):
        data.extend(chunk)
        if len(data)>limit:raise ValueError('Inventory output exceeds its bound')
    return bytes(data)

async def host(request):
    validate(request)
    path=str(Path(__file__).resolve()).replace('\\','/')
    if len(path)<3 or path[1]!=':' or not path[0].isalpha():raise ValueError('Inventory bootstrap requires a Windows path')
    path='/mnt/'+path[0].lower()+path[2:]
    encoded=base64.b64encode(json.dumps(request,separators=(',',':')).encode()).decode('ascii')
    process=await asyncio.create_subprocess_exec('wsl.exe','-d',request['distribution'],'--user','nobody','--exec',
        '/usr/bin/python3','-I','-S','-u',path,'--native',encoded,stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    readers=[asyncio.create_task(bounded(process.stdout,LIMIT)),asyncio.create_task(bounded(process.stderr,4096))]
    try:
        output,_,code=await asyncio.wait_for(asyncio.gather(*readers,process.wait()),15)
        if code:raise ValueError('Native inventory did not finish')
        return json.loads(output)
    finally:
        process.stdin.close()
        if process.returncode is None:process.kill()
        await process.wait()
        for task in readers:
            if not task.done():task.cancel()
        await asyncio.gather(*readers,return_exceptions=True)

def main():
    if len(sys.argv)!=3 or sys.argv[1] not in {'--host','--native'}:raise ValueError('Fixed inventory mode required')
    raw=base64.b64decode(sys.argv[2],validate=True)
    if len(raw)>LIMIT:raise ValueError('Inventory request exceeds its bound')
    request=json.loads(raw)
    if sys.argv[1]=='--native':
        if not sys.platform.startswith('linux'):raise ValueError('Native inventory requires Linux')
        result=native(request)
    else:
        if os.name!='nt':raise ValueError('Inventory bootstrap requires Windows')
        result=asyncio.run(host(request))
    encoded=json.dumps(result,separators=(',',':'))
    if len(encoded.encode())>LIMIT:raise ValueError('Inventory response exceeds its bound')
    print(encoded,flush=True)

if __name__=='__main__':main()
