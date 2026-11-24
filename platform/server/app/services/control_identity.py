"""Owned bootstrap infrastructure for fixed read-only native identity queries.

This is not a general execution API or a business Job. It must not depend on
native identity verification to start: doing so would recursively probe itself.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import uuid

from . import control_identity_reader as protocol,process_identity_native as identity
from .async_ops import finish_before_cancel

READER=Path(__file__).with_name('control_identity_reader.py').resolve()


class ReadUnavailable(ValueError):
    def __init__(self,reason):
        self.reason=reason
        super().__init__('Native control read unavailable: '+reason)


class ControlRead:
    def __init__(self,distro,kind,arguments):
        # Import lazily: OwnedProcess itself uses this bootstrap reader.
        from .owned_process import OwnedProcess
        self.owned=OwnedProcess()
        self.reader_path=READER
        self.request={'schema':protocol.SCHEMA,'nonce':uuid.uuid4().hex+uuid.uuid4().hex,
            'distribution':distro,'kind':kind,'arguments':copy.deepcopy(arguments),
            'helpers':{name:hashlib.sha256(READER.with_name(name).read_bytes()).hexdigest() for name in protocol.FILES}}
        # The production reader validates its own fixed sibling files. The
        # transport also checks the selected paths immediately before release.
        protocol.validate(self.request,directory=self.reader_path.parent)
        self.native_reader=None

    def check_code(self):
        if self.request['helpers']!={name:hashlib.sha256(self.reader_path.with_name(name).read_bytes()).hexdigest() for name in protocol.FILES}:
            raise ValueError('Selected control reader implementation changed')

    async def run(self):
        owned=self.owned
        encoded=base64.b64encode(json.dumps(self.request,separators=(',',':')).encode()).decode('ascii')
        reason='executor_error'
        try:
            self.check_code()
            argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S','-u',
                  str(self.reader_path),'--host',encoded]
            await owned.prepare(argv,cwd=self.reader_path.parent,env=None,runtime='windows')
            self.check_code();await owned.verify_registration();await owned.release()
            async def receive():
                data=await protocol.bounded(owned.proc.stdout,protocol.LIMIT)
                code=await owned.wait_root_exit()
                if code:raise ReadUnavailable('native_probe_failed')
                return json.loads(data)
            value=await asyncio.wait_for(receive(),25)
            if (not isinstance(value,dict) or set(value)!={'schema','nonce','kind','reader','result','error'}
                    or value['schema']!=protocol.SCHEMA or value['nonce']!=self.request['nonce']
                    or value['kind']!=self.request['kind']):
                raise ReadUnavailable('native_probe_invalid')
            if value['error'] is not None:
                if (value['error'] not in {'distribution_not_running','distribution_list_unavailable','native_probe_failed'}
                        or value['reader'] is not None or value['result'] is not None):
                    raise ReadUnavailable('native_probe_invalid')
                raise ReadUnavailable(value['error'])
            self.native_reader=identity.validate(value['reader'])
            if self.native_reader['platform']!='linux':raise ReadUnavailable('native_probe_invalid')
            result=value['result']
            if self.request['kind']=='identity':
                if (not isinstance(result,dict) or set(result)!={'state','reason'} or result['state'] not in identity.STATES
                        or not isinstance(result['reason'],str) or len(result['reason'])>128):
                    raise ReadUnavailable('native_probe_invalid')
            elif identity.validate(result)['platform']!='linux':raise ReadUnavailable('native_probe_invalid')
            await finish_before_cancel(owned.finish())
            return result
        except asyncio.CancelledError:
            reason='cancelled';raise
        except asyncio.TimeoutError as error:
            reason='timeout';raise ReadUnavailable('native_probe_unavailable') from error
        finally:
            try:
                if owned.proc is not None and not owned.closed and owned.stop_result is None:
                    await finish_before_cancel(owned.stop(reason=reason,grace_ms=0))
            finally:owned.close()


async def read(distro,kind,arguments):
    return await ControlRead(distro,kind,arguments).run()
