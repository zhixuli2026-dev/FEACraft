"""Case-scoped CLI instructions and reversible snapshots, using file transactions."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .file_read import FileRead, FileReadConflict

Harness = Literal['claude', 'codex', 'opencode', 'kimi']
FILES = ('AGENTS.md', 'CLAUDE.md')
STATE = '.vibeflow/cli-contracts.json'
STATE_LIMIT = 3 * 1024 * 1024
BEGIN = '\n<!-- vibeflow-cli-contract:begin -->\n'
END = '<!-- vibeflow-cli-contract:end -->\n'
CONTRACT = '''# VibeFlow 算例工作约定

- 0-caseDict/caseDict 是算例参数源；遵循平台 A 几何/B 网格/C 求解/D 后处理/E 报告/V 开发流程。
- 不直接改写 workflowState 或伪造阶段完成；通过平台提供的确认与执行入口推进状态。
- 保留已有输入和用户修改；完成后说明实际改动、产物、验证结果和未完成事项。
- 不把模拟模型或固定回复当作真实模型验收，不把程序路径存在当作求解成功。
- 经验和用户纠错可记录于 0-caseDict/memory.md；执行专业工具前核对当前算例依赖。
'''


class Snapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    created_at: str
    created_by: int
    selected: Harness | None = None
    instructions: str = Field(default='', max_length=8000)
    provider_profile_id: int | None = Field(default=None, gt=0)
    provider_profile_owner_id: int | None = Field(default=None, gt=0)
    files: dict[str, str | None]

    @model_validator(mode='after')
    def validate_files(self):
        if set(self.files) != set(FILES):raise ValueError('Invalid CLI snapshot paths')
        validate_profile_ref(self)
        for value in self.files.values():
            if value is not None and len(value.encode('utf-8')) > 32768:raise ValueError('CLI instruction file exceeds 32 KiB')
        return self


class State(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    selected: Harness | None = None
    instructions: str = Field(default='', max_length=8000)
    provider_profile_id: int | None = Field(default=None, gt=0)
    provider_profile_owner_id: int | None = Field(default=None, gt=0)
    backups: list[Snapshot] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def validate_profile(self):
        validate_profile_ref(self)
        return self


def validate_profile_ref(value):
    profile_id, owner_id = value.provider_profile_id, value.provider_profile_owner_id
    if (profile_id is None) != (owner_id is None) or (profile_id is not None and value.selected not in {'codex','opencode','kimi'}):
        raise ValueError('Invalid CLI model profile reference')


def profile_ref(value, actor_id):
    return value.provider_profile_id if value.provider_profile_owner_id == actor_id else None


def select(prefix):
    readers = {}
    for name in (*FILES, STATE):
        try:
            readers[name] = FileRead(prefix, name)
        except FileNotFoundError:
            readers[name] = None
    return readers


def versions(readers):
    return {name: reader.version if reader is not None else None for name, reader in readers.items()}


def verify_versions(prefix, selected):
    if versions(select(prefix)) != selected:
        raise FileReadConflict('CLI configuration changed during capture')


def capture(prefix):
    readers = select(prefix)
    contents = {}
    for name, reader in readers.items():
        if reader is None:
            contents[name] = None
            continue
        if reader.size > (STATE_LIMIT if name == STATE else 32768):
            raise ValueError('CLI configuration file exceeds its size limit')
        contents[name] = b''.join(reader.chunks())
    raw = contents[STATE]
    state=State.model_validate_json(raw) if raw is not None else State()
    files={name:contents[name].decode('utf-8') if contents[name] is not None else None for name in FILES}
    digest=hashlib.sha256(json.dumps({**files,STATE:raw.decode('utf-8') if raw is not None else None},
        ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    expected={name:None if value is None else {'size':len(value),'sha256':hashlib.sha256(value).hexdigest()}
        for name,value in contents.items()}
    return state,files,digest,expected,versions(readers)


def public(state,files,revision,*,actor_id=None):
    target='CLAUDE.md' if state.selected=='claude' else 'AGENTS.md'
    backups = [
        {**b.model_dump(exclude={'files','instructions','provider_profile_id','provider_profile_owner_id'}),
         'provider_profile_id':profile_ref(b, actor_id)} for b in reversed(state.backups)
    ]
    return {'revision':revision,'selected':state.selected,'instructions':state.instructions,
        'provider_profile_id':profile_ref(state, actor_id),
        'target':target if state.selected else None,
        'applied':bool(state.selected and BEGIN in (files[target] or '') and END in (files[target] or '')),
        'files':{name:{'exists':value is not None,'bytes':len(value.encode('utf-8')) if value is not None else 0} for name,value in files.items()},
        'backups':backups,
        'backup_limit':32}


def remove_managed(value):
    if value is None:return None
    if BEGIN not in value and END not in value:return value
    if value.count(BEGIN)!=1 or value.count(END)!=1:raise ValueError('平台契约标记已被修改，请先修正文件或恢复备份')
    start=value.index(BEGIN);end=value.index(END)
    if end<start:raise ValueError('平台契约标记顺序无效')
    return value[:start]+value[end+len(END):]


def change(state, files, user_id, *, selected=None, instructions='', restore_id=None, profile_id=None):
    before=Snapshot(id=uuid.uuid4().hex,created_at=datetime.now(timezone.utc).isoformat(),created_by=user_id,
        selected=state.selected,instructions=state.instructions,files=files,
        provider_profile_id=state.provider_profile_id, provider_profile_owner_id=state.provider_profile_owner_id)
    profile_owner_id = user_id if profile_id is not None else None
    if restore_id is not None:
        chosen=next((b for b in state.backups if b.id==restore_id),None)
        if chosen is None:raise KeyError('CLI backup does not exist')
        updated=dict(chosen.files);selected=chosen.selected;instructions=chosen.instructions
        profile_id, profile_owner_id = chosen.provider_profile_id, chosen.provider_profile_owner_id
    else:
        updated={name:remove_managed(value) for name,value in files.items()}
        if BEGIN.strip() in instructions or END.strip() in instructions:raise ValueError('补充约定不能包含平台契约标记')
        target='CLAUDE.md' if selected=='claude' else 'AGENTS.md'
        if selected is not None:
            updated[target]=(updated[target] or '')+BEGIN+CONTRACT+'\n'+instructions+ '\n'+END
    for value in updated.values():
        if value is not None and len(value.encode('utf-8'))>32768:raise ValueError('CLI instruction file exceeds 32 KiB')
    next_state=State(selected=selected,instructions=instructions,backups=[*state.backups,before][-32:],
        provider_profile_id=profile_id, provider_profile_owner_id=profile_owner_id)
    changes={name:value.encode('utf-8') if value is not None else None for name,value in updated.items()}
    changes[STATE]=next_state.model_dump_json().encode('utf-8')
    if len(changes[STATE]) > STATE_LIMIT:
        raise ValueError('CLI configuration exceeds its 3 MiB size limit')
    return changes,before.id
