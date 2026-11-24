"""I18：SCNET 中文语义化详情模型、搜索与脱敏摘要。"""
from __future__ import annotations

import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

from .scnet import redact

_OVERVIEW = {
    'id': '资源编号', 'jobid': '作业编号', 'sessionid': '会话编号', 'sid': '桌面编号', 'jobs': '关联作业',
    'name': '名称', 'jobname': '作业名称', 'instanceservicename': '实例名称', 'notebookname': 'Notebook 名称',
    'status': '状态', 'jobstatus': '作业状态', 'jobstate': '作业状态', 'notebookstatus': 'Notebook 状态',
    'user': '所属用户', 'username': '所属用户', 'owner': '所属用户',
    'createdat': '创建时间', 'createtime': '创建时间', 'createdate': '创建时间',
    'jobsubmittime': '提交时间', 'jobqueuetime': '入队时间', 'jobstarttime': '开始时间', 'starttime': '开始时间',
    'jobendtime': '结束时间', 'endtime': '结束时间', 'jobruntime': '运行时长',
    'duration': '时长', 'jobwalltimeused': '运行时长（秒）', 'resolution': '分辨率',
    'url': '访问链接', 'accessurl': '访问链接', 'serviceurl': '服务地址', 'novncurl': '桌面访问地址',
}
_RESOURCES = {
    'queue': '队列', 'jobmanagername': '调度器', 'hostname': '节点', 'node': '节点', 'nodeused': '执行节点',
    'jobexechost': '执行节点', 'workdir': '工作目录', 'command': '命令', 'outputpath': '标准输出',
    'errorpath': '标准错误', 'image': '镜像', 'imagename': '镜像名称', 'imagepath': '镜像地址',
    'version': '镜像版本', 'cpu': 'CPU', 'cpunumber': 'CPU 核数', 'gpu': 'GPU', 'gpunumber': 'GPU 数量',
    'memory': '内存', 'ramsize': '内存', 'memused': '已用内存', 'jobmemused': '已用物理内存（KB）',
    'procnumreq': '申请 CPU 核数', 'procnumused': '已用 CPU 核数', 'jobprocnum': '已用 CPU 核数',
    'jobreqcpu': '申请 CPU 核数', 'jobreqmem': '申请内存', 'jobreqgpu': '申请 GPU 数量', 'jobreqnodes': '申请节点数',
    'acceleratortype': '加速器类型', 'acceleratornumber': '加速器数量', 'resourcespec': '资源规格',
    'resourcegroup': '资源分组', 'resourcegroupcode': '资源分组编号', 'jobinitattr': '调度器参数',
    'mountinfolist': '挂载信息', 'containerportinfolist': '服务端口', 'jobvncsessioninfo': '桌面会话参数',
}
_SENSITIVE = ('token', 'secret', 'password', 'passwd', 'authorization', 'signature', 'apikey', 'accesskey',
              'privatekey', 'credential', 'cookie', 'loginpass')
_URL = re.compile(r'''(?i)\b(?:https?|ssh)://[^\s"'<>]+''')
_STATES = {'statr': '运行中', 'statq': '排队中', 'statc': '已完成', 'statd': '失败', 'statt': '超时',
           'statde': '已取消', 'running': '运行中', 'waiting': '等待中', 'creating': '创建中', 'failed': '失败',
           'restarting': '启动中', 'stopped': '已停止', 'shutting': '关机中'}
_RESOURCE_NAMES = {'jobs': '计算作业', 'desktops': '远程桌面', 'notebooks': 'Notebook', 'containers': '容器实例'}


def _key(key):
    return ''.join(char for char in key.casefold() if char.isalnum())


def _url_label(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        return '***'
    return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))


def redact_detail(value, secrets=(), *, key='', depth=0):
    normalized = _key(key)
    if any(part in normalized for part in _SENSITIVE) or normalized in {'ak', 'sk', 'mm'}:
        return '***'
    if isinstance(value, str):
        if normalized.endswith(('url', 'link')) and value.startswith('/'):
            value = _url_label(value)
        return redact(_URL.sub(lambda match: _url_label(match[0]), value), secrets)
    if isinstance(value, (list, dict)) and depth >= 6:
        return '…'
    if isinstance(value, dict):
        return {redact(name, secrets): redact_detail(item, secrets, key=name, depth=depth+1)
                for name, item in value.items()}
    if isinstance(value, list):
        return [redact_detail(item, secrets, depth=depth+1) for item in value]
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _seconds(value) -> str:
    seconds = int(value)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"


def _group(key):
    if key in _OVERVIEW:
        return 'overview'
    if key in _RESOURCES:
        return 'resources'
    return 'advanced'


def detail_model(data: dict, resource: str, secrets=()) -> dict:
    if not isinstance(data, dict):
        raise ValueError("详情必须是对象")
    if 'data' in data and ('code' in data or 'msg' in data):
        data = data['data']
        if not isinstance(data, dict):
            raise ValueError('SCNET 资源详情缺少对象数据')
    data = redact_detail(data, secrets)
    order = {'overview':0, 'resources':1, 'advanced':2}
    ordered_fields = sorted(data.items(), key=lambda entry: order[_group(_key(entry[0]))])
    fields = []
    for key, value in ordered_fields[:100]:
        if resource == 'desktops' and key in {'width', 'length', 'height'}:
            continue
        normalized = _key(key)
        group = _group(normalized)
        if normalized in {'status', 'jobstatus', 'jobstate', 'notebookstatus'} and isinstance(value, str):
            state = _STATES.get(value.casefold())
            if resource == 'notebooks' and value.casefold() == 'terminated':
                state = '已关机'
            if state:
                value = f'{state} ({value})'
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        if isinstance(value, str) and len(value) > 2000:
            value = value[:2000] + '…'
        fields.append({'key': key, 'label': _OVERVIEW.get(normalized, _RESOURCES.get(normalized, key)),
                       'value': value, 'group': group})
    if resource == 'desktops' and not data.get('resolution') and any(key in data for key in ('width', 'length', 'height')):
        width = data.get('width') or '?'
        height = data.get('length') or data.get('height') or '?'
        fields.insert(0, {"key": "resolution", "label": "分辨率",
                          "value": f'{width}×{height}', "group": "overview"})
    duration = data.get('duration', data.get('jobWalltimeUsed'))
    if resource == 'jobs' and type(duration) in (int, float) and duration >= 0 and 'duration_text' not in data:
        fields.append({"key": "duration_text", "label": "可读时长", "value": _seconds(duration), "group": "overview"})
    by_key = {_key(field['key']): field['value'] for field in fields}
    name = next((by_key[key] for key in ('name', 'jobname', 'instanceservicename', 'notebookname', 'id', 'jobid')
                 if by_key.get(key) not in (None, '')), _RESOURCE_NAMES.get(resource, resource))
    status = next((by_key[key] for key in ('status', 'jobstatus', 'jobstate', 'notebookstatus')
                   if by_key.get(key) not in (None, '')), None)
    summary = f'{name} · {status}' if status else str(name)
    return {"resource": resource, "summary": summary, "fields": fields[:100],
            "truncated": max(len(data), len(fields)) > 100}


def search_detail(model: dict, query: str = "") -> list[dict]:
    query = query.lower().strip()
    if not query:
        return [field for field in model["fields"] if field["group"] == "overview"]
    return [field for field in model["fields"]
            if any(query in str(field[key]).lower() for key in ('key', 'label', 'value'))]


def copy_summary(model: dict) -> str:
    lines = [model["summary"]]
    lines += [f"{field['label']}: {field['value']}" for field in model["fields"]]
    return redact("\n".join(lines))
