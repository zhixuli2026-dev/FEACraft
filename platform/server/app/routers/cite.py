"""在线文献检索（对标原版 LatexCitationDialog 的 Crossref/arXiv 检索面）

服务端代理检索（固定域名白名单，非任意 URL——无 SSRF 面）：
  GET /cite/search?q=&source=crossref|arxiv|semanticscholar
bibtex 由我们按检索字段自构（格式化自有实现）。
"""
from __future__ import annotations

import re
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil, isfinite
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/api/v1/cite", tags=["cite"])

CROSSREF = "https://api.crossref.org/works"
ARXIV = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
_SEMANTIC_FIELDS = "title,authors,year,externalIds,paperId,url"
_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}
_ARXIV_ID = re.compile(r"(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z.-]+/\d{7}(?:v\d+)?)", re.IGNORECASE)
_DOI = re.compile(r"10\.\d{4,9}/[^\s]+", re.IGNORECASE)
_PAPER_ID = re.compile(r"[a-f0-9]{40}", re.IGNORECASE)
_BIB_ESCAPES = str.maketrans({"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
                             "&": r"\&", "%": r"\%", "#": r"\#", "$": r"\$", "_": r"\_"})


class CitationFormatIn(BaseModel):
    bibtex: str = Field(min_length=1, max_length=1024 * 1024)
    keys: list[Annotated[str, Field(pattern=r"^[\w:./+\-]+$", max_length=128)]] = Field(min_length=1, max_length=100)
    preset: Literal["apa", "ieee", "chicago", "custom"]
    style: str | None = Field(default=None, max_length=512 * 1024)
    parent: str | None = Field(default=None, max_length=512 * 1024)


@router.post("/cases/{case_id}/format")
async def format_citation(case_id: int, body: CitationFormatIn, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    from .workspaces import get_owned_case
    from ..services.citation_format import CitationInputError, CitationUnavailable, render
    from ..services.case_write import CaseWriteConflict

    await get_owned_case(case_id, user, db, write=False)
    user_id = user.id
    await db.rollback()
    try:
        html = await render(case_id, user_id, **body.model_dump())
    except CitationInputError as error:
        raise HTTPException(422, str(error)) from error
    except CitationUnavailable as error:
        raise HTTPException(503, str(error)) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, str(error)) from error
    except TimeoutError as error:
        raise HTTPException(504, "引文格式化超时，请减少需要预览的条目。") from error
    return {"html": html}


def _key_hint(authors: list[str], year: str) -> str:
    first = ((authors[0].split() or ["ref"])[-1] if authors else "ref").lower()
    first = re.sub(r"[^a-z]", "", first) or "ref"
    return f"{first}{year or 'nd'}"


def _bibtex(kind: str, key: str, title: str, authors: list[str], year: str, ident: str,
            *, names: list[str] | None = None, publication: dict | None = None) -> str:
    title = " ".join(title.split()).translate(_BIB_ESCAPES)
    author_names = [" ".join(name.split()).translate(_BIB_ESCAPES) for name in (names or authors)]
    fields = [f"title = {{{title}}}", f"author = {{{' and '.join(author_names)}}}", f"year = {{{year}}}"]
    if ident.startswith("doi:"):
        fields.append(f"doi = {{{ident[4:]}}}")
    elif ident.startswith("arXiv:"):
        fields.extend([f"eprint = {{{ident[6:]}}}", "archivePrefix = {arXiv}",
                       f"url = {{https://arxiv.org/abs/{ident[6:]}}}"])
    else:
        fields.append(f"note = {{{ident}}}")
    for name, value in (publication or {}).items():
        if isinstance(value, str) and value.strip():
            value = " ".join(value.split()).translate(_BIB_ESCAPES)
            fields.append(f"{name} = {{{value}}}")
    return f"@{kind}{{{key},\n  " + ",\n  ".join(fields) + "\n}"


def _semantic_lookup(q: str):
    query = q.strip()
    doi = re.sub(r"^(?:doi:\s*|https://(?:dx\.)?doi\.org/)", "", query, flags=re.I)
    doi = unquote(doi)
    if _DOI.fullmatch(doi):
        return "DOI:" + doi
    arxiv = _ARXIV_ID.fullmatch(query)
    if arxiv:
        return "ARXIV:" + re.sub(r"v\d+$", "", arxiv[1], flags=re.I)
    paper_id = re.sub(r"^semanticscholar:", "", query, flags=re.I)
    if _PAPER_ID.fullmatch(paper_id):
        return paper_id.lower()
    corpus = re.fullmatch(r"CorpusId:(\d+)", query, flags=re.I)
    return "CorpusId:" + corpus[1] if corpus else None


def _semantic_retry_after(header, attempt):
    if header:
        try:
            seconds = float(header)
            if isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            try:
                until = parsedate_to_datetime(header)
                return max(0, (until.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2 ** attempt


def _semantic_item(item):
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    external = item.get("externalIds")
    external = external if isinstance(external, dict) else {}
    doi, arxiv, paper = external.get("DOI"), external.get("ArXiv"), item.get("paperId")
    if isinstance(doi, str) and _DOI.fullmatch(doi):
        ident = "doi:" + doi
    elif isinstance(arxiv, str) and (match := _ARXIV_ID.fullmatch(arxiv)):
        ident = "arXiv:" + match[1]
    elif isinstance(paper, str) and _PAPER_ID.fullmatch(paper):
        ident = "semanticscholar:" + paper.lower()
    else:
        return None
    people = item.get("authors")
    authors = [author["name"].strip() for author in people if isinstance(author, dict)
               and isinstance(author.get("name"), str) and author["name"].strip()] if isinstance(people, list) else []
    year = str(item["year"]) if type(item.get("year")) is int and 1 <= item["year"] <= 9999 else ""
    key = _key_hint(authors, year)
    return {"key_hint": key, "title": title, "authors": authors, "year": year, "id": ident,
            "source": "semanticscholar", "bibtex": _bibtex("article", key, title, authors, year, ident)}


async def _scholarly_response(name, url, params, headers=None):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 429:
            raise HTTPException(503, f"{name} 限流，请稍后重试") from error
        raise HTTPException(502, f"{name} 上游错误 ({error.response.status_code})") from error
    except httpx.RequestError as error:
        raise HTTPException(502, f"{name} 网络请求未完成，请稍后重试") from error


async def _search_semantic_scholar(q: str, limit: int, transport=None, sleep=asyncio.sleep, *, api_key=None):
    """Use the paper endpoint for identifiers and relevance search for text."""
    lookup = _semantic_lookup(q)
    path = quote(lookup, safe=":") if lookup else "search"
    params = {"fields": _SEMANTIC_FIELDS}
    if not lookup:
        params.update(query=q, limit=limit)
    client_args = {"timeout": httpx.Timeout(15.0), "follow_redirects": False}
    headers = {"User-Agent": "VibeFlow-Platform/0.1"}
    if api_key:
        headers["x-api-key"] = api_key
    if transport is not None:
        client_args["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_args) as client:
            for attempt in range(3):
                response = await client.get(f"{SEMANTIC_SCHOLAR}/{path}", params=params, headers=headers)
                if response.status_code == 429:
                    delay = _semantic_retry_after(response.headers.get("Retry-After"), attempt)
                    if attempt == 2 or delay > 5:
                        raise HTTPException(503, "Semantic Scholar 限流，请稍后重试",
                                            headers={"Retry-After": str(ceil(delay))})
                    await sleep(delay)
                    continue
                if lookup and response.status_code == 404:
                    return []
                if response.status_code >= 500:
                    raise HTTPException(502, f"Semantic Scholar 上游错误 ({response.status_code})")
                if response.status_code != 200:
                    raise HTTPException(502, f"Semantic Scholar 请求被拒绝 ({response.status_code})")
                data = response.json()
                break
    except httpx.HTTPError as error:
        raise HTTPException(502, "Semantic Scholar 网络请求未完成，请稍后重试") from error
    except ValueError as error:
        raise HTTPException(502, "Semantic Scholar 返回了无效 JSON") from error

    if not isinstance(data, dict) or not lookup and not isinstance(data.get("data"), list):
        raise HTTPException(502, "Semantic Scholar 返回了无效的文献数据")
    papers = [data] if lookup else data["data"]
    results = {}
    for paper in papers:
        item = _semantic_item(paper)
        if item:
            results.setdefault(item["id"].lower(), item)
    return list(results.values())[:limit]


@router.get("/search")
async def cite_search(q: str = Query(..., min_length=2, max_length=200),
                      source: str = Query("crossref", pattern="^(crossref|arxiv|semanticscholar)$"),
                      limit: int = Query(8, ge=1, le=20),
                      user: User = Depends(get_current_user)):
    if source == "crossref":
        r = await _scholarly_response("Crossref", CROSSREF, {"query": q, "rows": limit},
                                     {"User-Agent": "VibeFlow-Platform/0.1 (mailto:dev@vibeflow.cn)"})
        try:
            items = r.json().get("message", {}).get("items", [])
        except (ValueError, AttributeError) as error:
            raise HTTPException(502, "Crossref 返回了无效的文献数据") from error
        out = []
        for it in items:
            title = (it.get("title") or [""])[0]
            names = it.get("author", [])
            authors = [f"{a.get('given','')} {a.get('family','')}".strip() or a.get("name", "")
                       for a in names]
            bib_names = [", ".join(part for part in (a.get("family"), a.get("suffix"), a.get("given")) if part)
                         or a.get("name", "") for a in names]
            year = str((it.get("issued", {}).get("date-parts") or [[""]])[0][0] or "")
            doi = it.get("DOI", "")
            if not _DOI.fullmatch(doi):
                continue
            key = _key_hint(authors, year)
            kind = {"book": "book", "book-chapter": "incollection",
                    "proceedings-article": "inproceedings"}.get(it.get("type"), "article")
            container = (it.get("container-title") or [""])[0]
            out.append({"key_hint": key, "title": title, "authors": authors,
                        "year": year, "id": f"doi:{doi}", "source": "crossref",
                        "bibtex": _bibtex(kind, key, title, authors, year, f"doi:{doi}", names=bib_names,
                            publication={"journal" if kind == "article" else "booktitle": container,
                                "volume": it.get("volume"), "number": it.get("issue"),
                                "pages": it.get("page"), "publisher": it.get("publisher")})})
        return {"items": out}

    if source == "semanticscholar":
        key = get_settings().semantic_scholar_api_key
        return {"items": await _search_semantic_scholar(q, limit, api_key=key.get_secret_value() if key else None)}

    # arXiv（Atom XML）
    identifier = _ARXIV_ID.fullmatch(q.strip())
    params = {"id_list": identifier[1], "max_results": 1} if identifier else {
        "search_query": f"all:{q}", "max_results": limit,
    }
    r = await _scholarly_response("arXiv", ARXIV, params)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as error:
        raise HTTPException(502, "arXiv 返回了无效的文献数据") from error
    out = []
    for e in root.findall("a:entry", _ARXIV_NS):
        title = re.sub(r"\s+", " ", (e.findtext("a:title", "", _ARXIV_NS) or "").strip())
        authors = [a.findtext("a:name", "", _ARXIV_NS) or ""
                   for a in e.findall("a:author", _ARXIV_NS)]
        year = (e.findtext("a:published", "", _ARXIV_NS) or "")[:4]
        aid = (e.findtext("a:id", "", _ARXIV_NS) or "").rsplit("/abs/", 1)[-1]
        key = _key_hint(authors, year)
        out.append({"key_hint": key, "title": title, "authors": authors,
                    "year": year, "id": f"arXiv:{aid}", "source": "arxiv",
                    "bibtex": _bibtex("misc", key, title, authors, year, f"arXiv:{aid}")})
    return {"items": out}
