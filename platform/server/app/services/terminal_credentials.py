"""Bind a new Windows CLI terminal to an owned, encrypted API profile."""
import hashlib
import json
import os
import re
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException

from ..config import get_settings
from ..models import ProviderProfile
from . import llm


def descriptor(profile, harness='kimi'):
    if profile.provider not in llm.PROVIDERS:
        raise HTTPException(400, '不支持此 API 提供方')
    wire = llm.provider_api(profile.provider)
    endpoint = llm.resolve_endpoint(profile.provider, profile.base_url)
    suffix = '/chat/completions' if wire == 'openai' else '/v1/messages'
    base = endpoint.removesuffix(suffix)
    if harness == 'codex':
        from .codex_credentials import endpoint as responses_endpoint
        base, wire = responses_endpoint(profile.provider, profile.base_url), 'responses'
        if profile.provider == 'deepseek' and base in {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}:
            base = 'https://api.deepseek.com'
    if harness == 'claude':
        # Only the documented official DeepSeek endpoint has this mapping.
        # OpenAI-compatible custom gateways do not imply Messages support.
        if profile.provider == 'deepseek' and base in {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}:
            base, wire = 'https://api.deepseek.com/anthropic', 'anthropic'
        elif wire != 'anthropic':
            raise HTTPException(400, 'Claude Code 需要 Anthropic Messages 配置或官方 DeepSeek 配置')
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or any(ord(c) <= 32 for c in base)
            or (parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})):
        raise HTTPException(400, 'CLI 需要有效的 API 基础地址')
    try: parsed.port
    except ValueError: raise HTTPException(400, 'API 端口无效') from None
    model = profile.default_model or ''
    if not model or len(model) > 120 or any(ord(c) < 32 for c in model):
        raise HTTPException(400, '请先在 API 配置中设置默认模型')
    revision = hashlib.sha256(json.dumps([profile.id, profile.user_id, profile.provider,
        base, model, profile.key_ciphertext], ensure_ascii=True).encode()).hexdigest()
    return {'harness': harness, 'profile_id': profile.id, 'owner_id': profile.user_id,
            'provider': profile.provider, 'base_url': base, 'model': model, 'wire_api': wire, 'revision': revision}


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'harness','profile_id','owner_id','provider','base_url','model','wire_api','revision'}
            or value['harness'] not in {'kimi','opencode','claude','codex'} or value['wire_api'] not in {'openai','anthropic','responses'}
            or (value['harness']=='claude' and value['wire_api']!='anthropic')
            or (value['harness']=='codex') != (value['wire_api']=='responses')
            or any(type(value[k]) is not int or value[k] <= 0 for k in ('profile_id','owner_id'))
            or any(not isinstance(value[k],str) or not value[k] or len(value[k])>4096 for k in ('provider','base_url','model','revision'))
            or not re.fullmatch('[0-9a-f]{64}',value['revision'])):
        raise ValueError('Invalid terminal API binding')
    return value


async def owned(db, profile_id, user_id):
    profile = await db.get(ProviderProfile, profile_id, populate_existing=True)
    # An interactive shell can inspect its environment. Shared API access does
    # not authorize exporting somebody else's key into that shell.
    if profile is None or profile.org_id is not None or profile.user_id != user_id:
        raise HTTPException(403, '终端只能使用自己的 API 配置；共享配置不可导出到交互式命令环境')
    return profile


async def select(db, profile_id, user_id, harness, shell):
    if profile_id is None:
        if harness: raise HTTPException(400, 'CLI API 绑定缺少配置编号')
        return None, None
    if harness not in {'kimi','opencode','claude','codex'} or shell != 'cmd':
        raise HTTPException(400, 'API 配置启动目前支持 Windows Kimi/OpenCode 2/Claude Code/Codex；WSL 请使用原 CLI 配置')
    profile = await owned(db, profile_id, user_id)
    selected = descriptor(profile,harness)
    try: secret = llm.decrypt_key(profile.key_ciphertext)
    except Exception: raise HTTPException(409, 'API 密钥不可解密，请重新保存配置') from None
    if not 1 <= len(secret) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise HTTPException(400, 'API 密钥必须是有效单行凭据')
    return selected, secret


async def check(db, selected, user_id):
    current = descriptor(await owned(db, selected['profile_id'], user_id),selected['harness'])
    if current != selected:
        raise HTTPException(409, 'API 配置在启动期间发生变化，请重新启动')


def environment(selected, secret):
    from .cli_private_paths import root
    from .file_transactions import _path
    validate(selected)
    base = root(get_settings())
    base.mkdir(parents=True, exist_ok=True)
    home = _path(base, f"users/{selected['owner_id']}/windows/{selected['harness']}/providers/{selected['profile_id']}/{selected['revision']}")
    home.mkdir(parents=True, exist_ok=True)
    if selected['harness']=='codex':
        from . import codex_credentials as credentials
        from .codex_environment import OS_ENVIRONMENT
        from .file_transactions import _write_new
        native={'schema':credentials.SCHEMA,**{k:selected[k] for k in ('profile_id','owner_id','provider','base_url','wire_api','revision')},
                'default_model':selected['model']}
        configuration='model = '+credentials.toml(selected['model'])+'\n'+credentials.config(native)
        # This platform-generated config contains only an environment key name.
        # Atomic replacement keeps simultaneous launches from reading half TOML.
        target=_path(home,'config.toml');temporary=_path(home,'config-'+uuid.uuid4().hex+'.tmp')
        try:
            _write_new(temporary,configuration.encode('utf-8'));os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
        env={k:v for k,v in os.environ.items() if k.upper() in OS_ENVIRONMENT}
        temporary=home/'tmp';temporary.mkdir(exist_ok=True)
        env.update(CODEX_HOME=str(home),HOME=str(home),USERPROFILE=str(home),TMP=str(temporary),TEMP=str(temporary))
        # Windows system proxies must not intercept a selected local gateway.
        env['NO_PROXY']=','.join(filter(None,[os.environ.get('NO_PROXY',os.environ.get('no_proxy','')),
                                             'localhost','127.0.0.1','::1']))
        env[credentials.TOKEN_ENV]=secret
        return env
    if selected['harness']=='claude':
        env={k:v for k,v in os.environ.items() if not k.upper().startswith(('CLAUDE_', 'ANTHROPIC_'))
             or k.upper()=='CLAUDE_CODE_GIT_BASH_PATH'}
        temporary=home/'tmp';temporary.mkdir(exist_ok=True)
        env.update(HOME=str(home),USERPROFILE=str(home),TMP=str(temporary),TEMP=str(temporary),
                   CLAUDE_CONFIG_DIR=str(home),ANTHROPIC_BASE_URL=selected['base_url'],
                   ANTHROPIC_AUTH_TOKEN=secret,ANTHROPIC_MODEL=selected['model'],
                   CLAUDE_CODE_SUBAGENT_MODEL=selected['model'],CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   DISABLE_AUTOUPDATER='1')
        for tier in ('OPUS','SONNET','HAIKU'):
            env['ANTHROPIC_DEFAULT_'+tier+'_MODEL']=selected['model']
        return env
    if selected['harness']=='opencode':
        # V2 standalone uses this process environment. No secret is embedded
        # in config, argv, case files, or the durable session descriptor.
        env={k:v for k,v in os.environ.items() if not k.upper().startswith('OPENCODE_')
             or k.upper()=='OPENCODE_GIT_BASH_PATH'}
        for kind in ('config','data','cache','state'):
            folder=home/kind;folder.mkdir(exist_ok=True)
            env['XDG_'+kind.upper()+'_HOME']=str(folder)
        temporary=home/'tmp';temporary.mkdir(exist_ok=True)
        env.update(HOME=str(home),USERPROFILE=str(home),TMP=str(temporary),TEMP=str(temporary),
                   VIBEFLOW_OPENCODE_API_KEY=secret,OPENCODE_DISABLE_AUTOUPDATE='1',OPENCODE_DISABLE_CLAUDE_CODE='1')
        provider={'name':'VibeFlow API','env':['VIBEFLOW_OPENCODE_API_KEY'],
                  'package':'@opencode/ai/providers/'+('openai-compatible' if selected['wire_api']=='openai' else 'anthropic'),
                  'settings':{'baseURL':selected['base_url']+('/v1' if selected['wire_api']=='anthropic' else '')},
                  'models':{'selected':{'modelID':selected['model'],'name':selected['model'],
                     'limit':{'context':131072,'output':8192}}}}
        env['OPENCODE_CONFIG_CONTENT']=json.dumps({'model':'vibeflow_api/selected','providers':{'vibeflow_api':provider}},ensure_ascii=True)
        return env
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith('KIMI_MODEL_')
           and k.upper() not in {'KIMI_CODE_CUSTOM_HEADERS', 'KIMI_CODE_HOME'}}
    env.update(KIMI_CODE_HOME=str(home), KIMI_DISABLE_TELEMETRY='1',
               KIMI_MODEL_NAME=selected['model'], KIMI_MODEL_API_KEY=secret,
               KIMI_MODEL_PROVIDER_TYPE=selected['wire_api'], KIMI_MODEL_BASE_URL=selected['base_url'],
               KIMI_MODEL_MAX_CONTEXT_SIZE='131072', KIMI_MODEL_CAPABILITIES='')
    return env
