"""Read-only mapping of a private PTY gate to a child of the registered WSL root."""
import importlib.util
import json
from pathlib import Path
import sys


def locate(root,reported):
    spec=importlib.util.spec_from_file_location('terminal_os_identity',Path(__file__).with_name('process_identity_native.py'))
    identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity)
    identity.validate(root);identity.validate(reported)
    if root['platform']!='linux' or reported['platform']!='linux' or identity.probe(root)['state']!='match':
        raise ValueError('Terminal root is not the registered native birth')
    todo=[root['pid']];seen=set();found=[]
    while todo:
        pid=todo.pop()
        if pid in seen:continue
        seen.add(pid)
        if len(seen)>4096:raise ValueError('Terminal descendant inspection exceeds its bound')
        folder=Path('/proc')/str(pid)
        try:
            candidate=identity.observe(pid)
            if all(candidate[key]==reported[key] for key in ('host_id','boot_id','start_ticks','uid','pid_namespace')):
                values=dict(line.split(':',1) for line in (folder/'status').read_text().splitlines() if ':' in line)
                if (int(values['NSpid'].split()[-1])==reported['pid']
                        and int(values['NSpgid'].split()[-1])==reported['process_group']
                        and int(values['NSsid'].split()[-1])==reported['session']):found.append(candidate)
            todo.extend(int(value) for value in (folder/'task'/str(pid)/'children').read_text().split())
        except (FileNotFoundError,ProcessLookupError,PermissionError):continue
    if len(found)!=1 or identity.probe(root)['state']!='match' or identity.probe(found[0])['state']!='match':
        raise ValueError('Terminal gate is not a unique live descendant of its root')
    return found[0]


if __name__=='__main__':
    value=json.loads(sys.stdin.buffer.read(16385))
    if set(value)!={'root','reported'}:raise ValueError('Invalid native terminal inspection')
    print(json.dumps(locate(value['root'],value['reported'])))
