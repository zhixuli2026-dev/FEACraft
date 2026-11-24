"""Case-scoped file reads for Codex's read-only conversation modes."""
from .agent_storage_capacity import read_case
from .tools import ToolExecutor

READ_FILE = 'vibeflow_read_case_file'
TOOL_CALL = 'item/tool/call'

TOOLS = {
    READ_FILE: ('read_text_file', 'Read a UTF-8 text file by its path relative to the current engineering case.', ('path',), ()),
    'vibeflow_list_case_files': ('list_files', 'List files relative to the current engineering case.', (), ('subdir',)),
    'vibeflow_search_case_text': ('search_text', 'Search text files in the current engineering case.', ('query',), ('subdir',)),
}

READ_TOOLS = [{'type': 'function', 'name': name, 'description': description,
               'inputSchema': {'type': 'object', 'properties': {key: {'type': 'string'} for key in required + optional},
                               'required': list(required), 'additionalProperties': False}}
              for name, (_, description, required, optional) in TOOLS.items()]

READ_INSTRUCTIONS = ('Use vibeflow_read_case_file, vibeflow_list_case_files and vibeflow_search_case_text when you need to '
                     'inspect the current engineering case. They use case-relative paths and do not require shell approval.')


async def call(execution, params):
    session = execution.session
    args = params.get('arguments')
    name = params.get('tool')
    spec = TOOLS.get(name) if isinstance(name, str) else None
    if session.case_id is None or spec is None or params.get('namespace') is not None or not isinstance(args, dict):
        return {'ok': False, 'result': '只支持在当前算例内读取、列出和搜索文件。'}
    tool, _, required, optional = spec
    if not set(required) <= set(args) <= set(required + optional) or any(not isinstance(value, str) for value in args.values()):
        return {'ok': False, 'result': '算例读取工具参数不正确。'}

    await execution.authorize()
    source = session.selected['source_prefix']
    executor = ToolExecutor(source, case_id=session.case_id, user_id=session.user_id)
    return await read_case(session.case_id, source, session.user_id, executor.execute,
                           tool, args, cancel=execution.cancel)
