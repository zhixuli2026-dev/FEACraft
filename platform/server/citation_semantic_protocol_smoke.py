"""Semantic Scholar HTTP protocol fixtures, without browser, database queries or model calls."""
import asyncio
import json


DOI = "10.1145/1330311.1330313"


async def protocol():
    import httpx
    from fastapi import HTTPException
    from app.routers.cite import _search_semantic_scholar

    attempts = []
    delays = []
    stable_id = 'ab' * 20

    def scholarly_http(request):
        assert request.url.host == 'api.semanticscholar.org'
        attempts.append(request.url.raw_path.split(b'?', 1)[0].decode('ascii'))
        if len(attempts) <= 2:
            return httpx.Response(429, headers={'Retry-After': str(len(attempts))}, request=request)
        data = {'paperId': stable_id, 'title': 'A & B {notation}', 'year': 2024,
                'authors': [{'name': 'Author ' + str(index)} for index in range(8)]}
        return httpx.Response(200, json={'data': [data, None, data]}, request=request)

    async def observe_delay(value):
        delays.append(float(value))

    rows = await _search_semantic_scholar('engineering terms', 8, httpx.MockTransport(scholarly_http), observe_delay)
    assert attempts == ['/graph/v1/paper/search'] * 3 and delays == [1., 2.]
    assert len(rows) == 1 and rows[0]['id'] == 'semanticscholar:' + stable_id
    assert len(rows[0]['authors']) == 8 and r'A \& B \{notation\}' in rows[0]['bibtex']
    assert 'note = {semanticscholar:' + stable_id + '}' in rows[0]['bibtex']

    def retry_after(request):
        assert request.url.raw_path.startswith(b'/graph/v1/paper/DOI:10.1145%2F1330311.1330313?')
        return httpx.Response(429, headers={'Retry-After': '55'}, request=request)

    delays.clear()
    try:
        await _search_semantic_scholar('https://doi.org/' + DOI, 8, httpx.MockTransport(retry_after), observe_delay)
        raise AssertionError('The longer wait must be returned to the caller')
    except HTTPException as error:
        assert error.status_code == 503 and error.headers == {'Retry-After': '55'} and not delays

    def missing(request):
        assert request.url.path == '/graph/v1/paper/' + stable_id
        return httpx.Response(404, request=request)

    assert not await _search_semantic_scholar('semanticscholar:' + stable_id, 8, httpx.MockTransport(missing))

    def authenticated_search(request):
        assert request.url.host == 'api.semanticscholar.org' and request.url.scheme == 'https'
        assert request.headers['x-api-key'] == 'protocol-fixture'
        assert 'protocol-fixture' not in str(request.url)
        return httpx.Response(307, headers={'Location': 'https://example.org/redirect'}, request=request)

    try:
        await _search_semantic_scholar('engineering terms', 8, httpx.MockTransport(authenticated_search),
                                       api_key='protocol-fixture')
    except HTTPException as error:
        assert error.status_code == 502 and 'protocol-fixture' not in error.detail
    else:
        raise AssertionError('An upstream redirect must not forward the API key')
    return ['relevance_retry_and_no_doi_metadata', 'long_retry_after_is_propagated_without_an_early_retry',
            'missing_paper_identifier_returns_empty_results', 'api_key_uses_header_and_does_not_follow_redirect']

if __name__ == "__main__":
    from app.config import Settings

    settings = Settings(_env_file=None, semantic_scholar_api_key="settings-protocol")
    assert "settings-protocol" not in settings.model_dump_json()
    print("SEMANTIC_SCHOLAR_PROTOCOL_OK", json.dumps(asyncio.run(protocol())))
