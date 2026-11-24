"""Bounded public event projection and private encrypted secret redaction."""
import json
import os
from pathlib import Path
import tempfile
import shutil
import stat

from cryptography.fernet import Fernet

from .redaction import redact_text
from .sanitizer import ProtocolSanitizer, sanitize_text

MAX_TEXT = 8 * 1024 * 1024
MAX_SECRET_STORE = 1024 * 1024
APPROVAL_FIELDS = {'threadId', 'turnId', 'itemId', 'startedAtMs', 'command', 'cwd', 'reason', 'kind', 'approvalId',
    'availableDecisions', 'commandActions', 'additionalPermissions', 'proposedExecpolicyAmendment',
    'proposedNetworkPolicyAmendments', 'networkApprovalContext', 'grantRoot', 'permissions', 'questions', 'isBlocking'}


class Secrets:
    def __init__(self, home, *, ephemeral=()):
        self.folder = Path(home).parent
        self.path = self.folder / '.redaction-secrets.bin'
        self.key_path = self.folder / '.redaction.key'
        self.values = []
        self.stamp = None
        self.ephemeral = tuple(ephemeral)

    def vault(self):
        # Reads must never recreate a missing key for an existing ciphertext.
        return Fernet(self.read(self.key_path,44))

    def directory(self):
        from ..config import get_settings
        from .cli_private_paths import account_directory
        return account_directory(self.folder,get_settings())

    def read(self,path,maximum):
        from .file_transactions import _path
        self.directory();_path(self.folder,path.name)
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size>maximum:
            raise ValueError('Private redaction file exceeds its limit')
        with path.open('rb') as stream:
            data=stream.read(maximum+1)
        if len(data)>maximum:raise ValueError('Private redaction file exceeds its limit')
        return data

    def stored(self):
        self.directory()
        from .file_transactions import _path
        _path(self.folder,self.path.name)
        if not self.path.exists():
            return self.values
        stamp = (self.path.stat().st_mtime_ns, self.path.stat().st_size)
        if stamp != self.stamp:
            if stamp[1] > MAX_SECRET_STORE:
                raise ValueError('Private redaction store exceeds its limit')
            values = json.loads(self.vault().decrypt(self.read(self.path,MAX_SECRET_STORE)))
            if not isinstance(values, list) or len(values) > 128 or any(not isinstance(item, str) or not 1 <= len(item) <= 4096 for item in values):
                raise ValueError('Invalid private redaction store')
            self.values, self.stamp = values, stamp
        return self.values

    def load(self):
        return list(dict.fromkeys([*self.stored(), *self.ephemeral]))

    def prepare_update(self, values):
        # Call under a database lock on this user, across conversations/workers.
        # Approval allows 64 questions with eight answers each; the persistent
        # limit applies to distinct merged secrets, not repeated input values.
        if not isinstance(values,(list,tuple)) or len(values)>512 or any(
                not isinstance(value,str) or not 1<=len(value)<=4096 for value in values):
            raise ValueError('Invalid private secret redaction values')
        existing = list(self.stored())
        previous=list(existing)
        for value in values:
            if value not in existing and value not in self.ephemeral:
                existing.append(value)
        if len(existing) > 128:
            raise ValueError('Private secret redaction capacity reached')
        if existing==previous:
            return
        plaintext=json.dumps(existing,ensure_ascii=False).encode('utf8')
        key=self.read(self.key_path,44) if self.key_path.exists() else Fernet.generate_key()
        payload=Fernet(key).encrypt(plaintext)
        if len(payload)>MAX_SECRET_STORE:
            raise ValueError('Private redaction store exceeds its limit')
        return existing,plaintext,key,payload

    def update_size(self,values):
        prepared=self.prepare_update(values)
        return len(prepared[2])+len(prepared[3]) if prepared is not None else 0

    def remember(self, values, *, maximum_bytes=None):
        prepared=self.prepare_update(values)
        if prepared is None:return
        existing,plaintext,key,payload=prepared
        # Existing ciphertext remains in the measured inventory. Reserve the
        # complete replacement plus a key before either can reach the disk.
        from ..config import get_settings
        from .node_storage_budget import reserve
        from .case_write import CaseWriteConflict
        peak=len(payload)+len(key)
        if maximum_bytes is not None and (type(maximum_bytes) is not int or maximum_bytes<0 or peak>maximum_bytes):
            raise ValueError('Private redaction write exceeds its admitted user bytes')
        with reserve(peak,private=True):
            folder=self.directory()
            if shutil.disk_usage(folder).free<peak+get_settings().node_storage_min_free_mb*1024*1024:
                raise CaseWriteConflict('node_storage_full','私有账户缓存所在磁盘空间不足')
            from .file_transactions import _path
            _path(folder,self.key_path.name);_path(folder,self.path.name)
            try:
                descriptor=os.open(self.key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError:
                # A concurrent initializer may have created a different key.
                # The user-row lock remains required for merging stored values.
                payload=self.vault().encrypt(plaintext)
            else:
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(key);stream.flush();os.fsync(stream.fileno())
            descriptor, name = tempfile.mkstemp(prefix='.redaction-', dir=folder)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
                self.values, self.stamp = existing, None
            finally:
                Path(name).unlink(missing_ok=True)

    def clean(self, value):
        values = self.load()
        def clean(item):
            if isinstance(item, str):
                return redact_text(item.replace('\0', '\\0'), values, limit=MAX_TEXT)
            if isinstance(item, dict):
                return {key: clean(value) for key, value in item.items()}
            if isinstance(item, list):
                return [clean(value) for value in item]
            return item
        return clean(value)


class Projection:
    def __init__(self, home, *, ephemeral=()):
        self.secrets = Secrets(home, ephemeral=ephemeral)
        self.buffers = {}
        self.sanitizers = {}
        self.answers = {}

    def delta(self, key, text, *, final=False, model_text=True):
        if not isinstance(text, str):
            raise ValueError('Invalid native text delta')
        value = self.buffers.get(key, '') + text
        if len(value.encode('utf8')) > MAX_TEXT:
            raise ValueError('Native text stream exceeds its limit')
        secrets = self.secrets.load()
        hold = max([32, *(len(secret) - 1 for secret in secrets)]) if not final else 0
        end = max(0, len(value) - hold)
        # Move a cut before a known secret that crosses it. Keeping the tail
        # alone would otherwise publish the secret's prefix in one event.
        for secret in secrets:
            start = value.find(secret)
            while start >= 0:
                if start < end < start + len(secret):
                    end = start
                start = value.find(secret, start + 1)
        self.buffers[key] = value[end:]
        result = self.secrets.clean(value[:end])
        if not model_text:
            return result
        sanitizer = self.sanitizers.setdefault(key, ProtocolSanitizer())
        result = sanitizer.feed(result, final=final)
        if len(sanitizer._buffer.encode('utf8')) > MAX_TEXT:
            raise ValueError('Native protocol sanitizer exceeds its limit')
        return result

    def approval(self, params):
        value = self.secrets.clean({key: value for key, value in params.items() if key in APPROVAL_FIELDS})
        if 'questions' in value:
            value['questions'] = [{key: item for key, item in question.items() if key != 'options' or not question.get('isSecret')}
                                  for question in value['questions']]
        return value

    def item(self, item):
        kind = item.get('type')
        if kind in {'agentMessage', 'plan'}:
            text = item.get('text', '')
            if not isinstance(text, str) or len(text.encode('utf8')) > MAX_TEXT:
                raise ValueError('Invalid native assistant message')
            text = sanitize_text(self.secrets.clean(text))
            self.answers[item['id']] = text
            if sum(len(value.encode('utf8')) for value in self.answers.values()) > MAX_TEXT:
                raise ValueError('Native assistant messages exceed their aggregate limit')
            return {'id': item['id'], 'type': kind, 'text': text}
        if kind == 'commandExecution':
            fields = {'id', 'type', 'command', 'cwd', 'status', 'aggregatedOutput', 'exitCode', 'durationMs'}
        elif kind == 'fileChange':
            fields = {'id', 'type', 'status', 'changes'}
        elif kind == 'dynamicToolCall':
            fields = {'id', 'type', 'status', 'tool', 'success'}
        elif kind in {'webSearch', 'mcpToolCall', 'imageView'}:
            fields = {'id', 'type', 'status', 'name', 'server', 'tool'}
        else:
            fields = {'id', 'type'}
        return self.secrets.clean({key: value for key, value in item.items() if key in fields})
