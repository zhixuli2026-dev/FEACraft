"""SCNET B/S 资源客户端：签名 HTTP、超时与生命周期操作映射。"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from .scnet import (MAX_REDIRECTS, REDIRECT_STATUSES, compact_sign, header_diagnostics, parse_error,
                    parse_list, safe_link, safe_redirect, service_url)


_REQUEST_SECONDS = 10


class ScnetError(Exception):
    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("message", "SCNET 请求失败"))


class ScnetClient:
    def __init__(self, base_url: str, access_key: str, secret_key: str, token: str = '', *, service='hpc'):
        self.base_url = service_url(base_url, service)
        self.access_key, self.secret_key, self.token = access_key, secret_key, token

    def headers(self):
        if not self.access_key:
            return {'token': self.token} if self.token else {}
        payload = {"accessKey": self.access_key, "token": self.token}
        headers = {"X-Access-Key": self.access_key,
                   "X-Signature": compact_sign(payload, self.secret_key)}
        if self.token:
            headers["token"] = self.token
        return headers

    async def request(self, method: str, path: str, body=None, params=None, *, headers=None):
        headers = self.headers() if headers is None else headers
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                async with httpx.AsyncClient(timeout=_REQUEST_SECONDS, follow_redirects=False, trust_env=False) as client:
                    url = self.base_url + path
                    for hop in range(MAX_REDIRECTS + 1):
                        response = await client.request(method, url, json=body, params=params, headers=headers)
                        if response.status_code not in REDIRECT_STATUSES:
                            break
                        location = response.headers.get('Location', '')
                        if method != 'GET':
                            raise ScnetError({'code':'scnet_mutation_redirect',
                                              'message':'远端变更请求收到重定向，请刷新资源核对结果'})
                        try:
                            if hop == MAX_REDIRECTS:
                                raise ValueError('重定向次数超限')
                            url = safe_redirect(str(response.url), location)
                        except ValueError as error:
                            raise ScnetError({'code':'scnet_redirect_refused','message':str(error)}) from error
                        params = None
                        client.cookies.clear()
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ScnetError({'code':'scnet_timeout','message':'SCNET 请求超时，请刷新资源核对结果'}) from error
        except httpx.HTTPError as error:
            raise ScnetError({"message": f"SCNET 网络请求失败（{type(error).__name__}）"}) from error
        def failure():
            secrets = [self.secret_key, self.access_key, self.token, headers.get('signature'), headers.get('token')]
            detail = parse_error(response.status_code, method, response.url, response.content,
                                 secrets)
            detail['header_diagnostics'] = header_diagnostics(response.request.headers)
            return ScnetError(detail)

        if response.status_code >= 400:
            raise failure()
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as error:
            raise ScnetError({"message": "SCNET 返回非 JSON"}) from error
        if (isinstance(data, dict) and data.get('code') not in (None, 0, '0', 200, '200')
                and any(data.get(field) for field in ('msg', 'message', 'error'))):
            raise failure()
        return data

    @staticmethod
    def _id(args: dict, key: str = 'id') -> str:
        raw = args.get(key)
        value = str(raw) if type(raw) in (str, int) else ''
        if not value or '/' in value or '\\' in value or '..' in value or any(ord(char) < 32 for char in value):
            raise ValueError("资源 ID 无效")
        return quote(value, safe='')

    async def detail(self, operation: str, args: dict):
        """Fetch the documented SCNET detail payload for display."""
        ident = self._id(args)
        if operation == 'jobs.detail':
            return await self.request('GET', f'/openapi/v2/jobs/{ident}')
        if operation == 'jobs.history-detail':
            manager = self._id(args, 'manager_id')
            account_time = args.get('account_time')
            params = {'acctTime': account_time} if isinstance(account_time, str) and account_time else None
            return await self.request('GET', f'/openapi/v2/historyjobs/{manager}/{ident}', params=params)
        if operation == 'containers.detail':
            return await self.request('GET', f'/openapi/v2/instance-service/{ident}/detail')
        if operation == 'notebooks.detail':
            return await self.request('GET', '/openapi/v2/notebook/detail', params={'notebookId': str(args['id'])})
        if operation == 'desktops.detail':
            rows = parse_list(await self.request('GET', '/openapi/v2/desktop/vnc'))
            requested = str(args['id'])
            matches = [row for row in rows if isinstance(row, dict)
                       and any(str(row[key]) == requested for key in ('sessionId', 'id') if row.get(key) is not None)]
            if len(matches) == 1:
                return matches[0]
            raise ScnetError({'message': '无法从当前桌面列表确认对应会话，请刷新后重试'})
        raise ValueError('此 SCNET 操作不提供资源详情')

    async def account_list(self, operation: str):
        if operation == 'desktops.list':
            return parse_list(await self.request('GET', '/openapi/v2/desktop/vnc'))
        clusters = parse_list(await self.request('GET', '/openapi/v2/cluster'))
        manager = next((str(row['id']) for row in clusters if isinstance(row, dict)
                        and str(row.get('id', '')).isascii() and str(row.get('id', '')).isdigit()), None)
        if manager is None:
            if not clusters:
                return []
            raise ScnetError({'message': 'SCNET 区域未返回有效的调度集群编号'})
        return parse_list(await self.request('GET', '/openapi/v2/jobs', params={
            'strClusterIDList': manager, 'start': 0, 'limit': 25,
        }))

    async def execute(self, operation: str, args: dict):
        path = args.get("path", "")
        if operation == "jobs.list":
            return parse_list(await self.request("GET", "/openapi/v2/jobs", params=args.get("query")))
        if operation == "jobs.history":
            return parse_list(await self.request("GET", "/openapi/v2/job-history", params=args.get("query")))
        if operation == "jobs.detail":
            return await self.request("GET", f"/openapi/v2/jobs/{self._id(args)}")
        if operation == "jobs.submit":
            return await self.request("POST", "/openapi/v2/jobs", body=args.get("job", {}))
        if operation == "jobs.cancel":
            return await self.request("POST", f"/openapi/v2/jobs/{self._id(args)}:cancel")
        if operation == "files.list":
            return parse_list(await self.request("GET", "/openapi/v2/files", params={"path": path}))
        if operation == "files.mkdir":
            return await self.request("POST", "/openapi/v2/files:mkdir", body={"path": path})
        if operation == "files.upload":
            return await self.request("POST", "/openapi/v2/files:upload",
                                      body={"path": path, "content": args.get("content", "")})
        if operation == "files.download":
            import asyncio
            from .async_ops import finish_before_cancel
            from .scnet_download import preview
            return await finish_before_cancel(asyncio.to_thread(preview, self, path))
        if operation in {"files.rename", "files.copy", "files.move", "files.delete"}:
            action = operation.split(".")[1]
            return await self.request("POST", f"/openapi/v2/files:{action}",
                                      body={"path": path, "target": args.get("target", "")})
        if operation in {"containers.list", "notebooks.list", "desktops.list"}:
            resource = operation.split(".")[0]
            return parse_list(await self.request("GET", f"/openapi/v2/{resource}",
                                                 params=args.get("query")))
        if operation in {"containers.detail", "notebooks.detail", "desktops.detail"}:
            resource = operation.split(".")[0]
            return await self.request("GET", f"/openapi/v2/{resource}/{self._id(args)}")
        if operation in {"containers.create", "notebooks.create", "desktops.create"}:
            resource = operation.split(".")[0]
            return await self.request("POST", f"/openapi/v2/{resource}", body=args.get("resource", {}))
        if operation in {"containers.start", "containers.restart", "containers.stop", "containers.delete",
                         "containers.exec", "containers.open", "notebooks.start", "notebooks.stop",
                         "notebooks.release", "notebooks.rename", "notebooks.open",
                         "desktops.delete", "desktops.open"}:
            resource, action = operation.split(".")
            ident = self._id(args)
            if action == "open":
                data = await self.request("GET", f"/openapi/v2/{resource}/{ident}:open")
                return {"url": safe_link(self.base_url, data.get("url", ""))}
            if action == "rename":
                return await self.request("POST", f"/openapi/v2/{resource}/{ident}:rename",
                                          body={"name": args.get("name", "")})
            if action == "exec":
                return await self.request("POST", f"/openapi/v2/{resource}/{ident}:exec",
                                          body={"command": args.get("command", "")})
            if action == "stop" and resource == "notebooks":
                return await self.request("POST", f"/openapi/v2/{resource}/{ident}:stop",
                                          body={"save_environment": bool(args.get("save_environment"))})
            return await self.request("POST", f"/openapi/v2/{resource}/{ident}:{action}")
        raise ValueError(f"未知 SCNET 操作: {operation}")
