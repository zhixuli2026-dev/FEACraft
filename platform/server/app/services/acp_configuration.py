"""Portable provider configuration for installed ACP harnesses."""

import json


HARNESSES = {'opencode', 'kimi'}
TOKEN_ENV = 'VIBEFLOW_ACP_API_KEY'


def environment(harness, home, env, *, endpoint, model, api_key, wire='openai', allow_file_approval=False,
                allow_case_inputs=False,
                managed_text_only=False, contract_file=None, write_config=None):
    if harness not in HARNESSES or wire not in {'openai', 'anthropic'}:
        raise ValueError('Unsupported ACP provider protocol')
    folders = {name: home/name for name in ('config', 'cache', 'state', 'data', 'tmp', 'local', 'roaming')}
    for path in folders.values():
        path.mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(folders['roaming']),
               LOCALAPPDATA=str(folders['local']), TEMP=str(folders['tmp']), TMP=str(folders['tmp']),
               TMPDIR=str(folders['tmp']))
    no_proxy = env.get('NO_PROXY', env.get('no_proxy', ''))
    env['NO_PROXY'] = ','.join(filter(None, [no_proxy, 'localhost', '127.0.0.1', '::1']))
    if harness == 'opencode':
        env.update({f'XDG_{kind.upper()}_HOME': str(folders[kind]) for kind in ('config', 'cache', 'data', 'state')})
        provider = {'name': 'VibeFlow API', 'env': [TOKEN_ENV],
                    'package': '@opencode/ai/providers/'+('openai-compatible' if wire == 'openai' else 'anthropic'),
                    'settings': {'baseURL': endpoint if wire == 'openai' else endpoint.rstrip('/')+'/v1'},
                    'models': {'selected': {'modelID': model, 'name': model,
                                            'limit': {'context': 131072, 'output': 8192}}}}
        permissions = [{'action':'*','resource':'*','effect':'deny'}]
        if allow_case_inputs:
            permissions.append({'action':'read','resource':'case/*','effect':'allow'})
        if allow_file_approval:
            permissions.append({'action':'edit','resource':'*','effect':'ask'})
        config = {'model':'vibeflow_api/selected','providers':{'vibeflow_api':provider},'permissions':permissions}
        if contract_file is not None:
            config['instructions'] = [str(contract_file)]
        env.update(OPENCODE_DISABLE_AUTOUPDATE='1', OPENCODE_DISABLE_CLAUDE_CODE='1',
                   OPENCODE_CONFIG_CONTENT=json.dumps(config), **{TOKEN_ENV:api_key})
        return env
    env.update(KIMI_CODE_HOME=str(folders['config']), KIMI_DISABLE_TELEMETRY='1', KIMI_MODEL_NAME=model,
               KIMI_MODEL_PROVIDER_TYPE=wire, KIMI_MODEL_BASE_URL=endpoint, KIMI_MODEL_API_KEY=api_key,
               KIMI_MODEL_MAX_CONTEXT_SIZE='131072', KIMI_MODEL_CAPABILITIES='')
    if managed_text_only:
        enabled = (['Read'] if allow_case_inputs else []) + (['Write'] if allow_file_approval else [])
        documents = {'config.toml': ('[permission]\ndefault_permission_mode = "manual"\n'
                                      f'[tools]\nenabled = {json.dumps(enabled or ["*"])}\n').encode('ascii')}
        if contract_file is not None:
            documents['SYSTEM.md'] = b'You are Kimi Code assisting in a VibeFlow engineering case.\n\n${agents_md}\n'
        for name, content in documents.items():
            write_config(folders['config'], name, content)
    return env
