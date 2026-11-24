"""Real owned viz: reap idle kernels, retain active viewers and verify close."""
import os
import time
from testsupport import IsolatedServer
from viz_execution_smoke import STL

settings={'VIBEFLOW_VIZ_IDLE_SECONDS':'1','VIBEFLOW_VIZ_IDLE_CHECK_INTERVAL':'.05'}
previous={key:os.environ.get(key) for key in settings};os.environ.update(settings)
try:
    with IsolatedServer('viz-idle') as server:
        server.create_case('owned idle');server.put('D-post/cube.stl',STL)
        old=server.call('POST',server.prefix+'/viz:sessions',{'file_path':'D-post/cube.stl'})
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            row=server.call('GET',f"/viz:sessions/{old['id']}")
            if row['phase']=='closed':break
            time.sleep(.05)
        assert row['phase']=='closed' and row['cleanup_verified'] is True and row['alive'] is False,row
        assert not server.call('GET',server.prefix+'/viz:sessions')
        print('1. Idle real kernel exited, persisted verified cleanup and left active registry [OK]')
        fresh=server.call('POST',server.prefix+'/viz:sessions',{'file_path':'D-post/cube.stl'})
        for _ in range(7):
            server.call('GET',f"/viz-proxy/{fresh['id']}/index.html?token={server.token}",api_prefix=False,raw=True)
            time.sleep(.2)
        assert server.call('GET',f"/viz:sessions/{fresh['id']}")['alive'] is True
        server.call('DELETE',f"/viz:sessions/{fresh['id']}")
        row=server.call('GET',f"/viz:sessions/{fresh['id']}")
        assert row['phase']=='closed' and row['cleanup_verified'] is True and row['alive'] is False,row
        print('2. Proxy activity retains real kernel; explicit close still verifies exit [OK]')
finally:
    for key,value in previous.items():
        if value is None:os.environ.pop(key,None)
        else:os.environ[key]=value
print('VIZ_IDLE_OK')
