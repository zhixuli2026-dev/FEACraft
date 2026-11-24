"""Collect the original public NOAA CO-OPS hourly CSV for a case."""
import asyncio
from collections import Counter
from csv import Error as CSVError, writer
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
from io import StringIO
import re
from urllib.parse import urlencode

import aiohttp

from .postprocess_series import MAX_SERIES_BYTES, read_rows
from .webfetch import FetchSecurityError, PublicOnlyResolver


_SOURCE = 'NOAA/NOS Center for Operational Oceanographic Products and Services (CO-OPS)'
_DATA = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
_TERMS = 'https://www.tidesandcurrents.noaa.gov/disclaimers.html'
_PRODUCTS = {'hourly_height': 'Water Level', 'predictions': 'Prediction'}
_DATUMS = {'MLLW', 'MHHW', 'MSL', 'NAVD'}
_UNITS = {'metric': 'm', 'english': 'ft'}
_WIND_TUNNEL = 'https://tmbwg.github.io/turbmodels/Backstep_validation/profiles.exp.dat'
_WIND_REFERENCE = 'https://tmbwg.github.io/turbmodels/backstep_val.html'
_WIND_SECTIONS = {-4, 1, 4, 6, 10}


class ObservationSourceError(ValueError):
    pass


async def fetch_noaa(station, begin, end, product, datum, units):
    if (not re.fullmatch(r'\d{7}', station, re.ASCII) or datum not in _DATUMS or
            product not in _PRODUCTS or units not in _UNITS):
        raise ValueError('NOAA 站点、产品、基准或单位不受支持')
    if not isinstance(begin, date) or not isinstance(end, date) or not 0 <= (end - begin).days <= 30:
        raise ValueError('NOAA 按小时请求需要不超过 31 天的日期范围')
    params = {'begin_date': begin.strftime('%Y%m%d'), 'end_date': end.strftime('%Y%m%d'),
              'station': station, 'datum': datum, 'time_zone': 'gmt', 'units': units,
              'application': 'VibeFlow', 'format': 'csv', 'product': product}
    if product == 'predictions':
        params['interval'] = 'h'
    url = _DATA + '?' + urlencode(params)
    data = await _fetch_data(url, ('text/csv', 'text/comma-separated-values'), 'NOAA')

    value_column = _PRODUCTS[product]
    try:
        times, rows = read_rows(data, 'Date Time', value_column, assume_utc=True)
        if any(not begin <= moment.date() <= end or moment.minute or moment.second or moment.microsecond
               for moment in times):
            raise ValueError('NOAA 返回的记录超出请求时段或不在整点')
        quality = ({name: dict(Counter(row[name] for row in rows)) for name in ('I', 'L')}
                   if product == 'hourly_height' else {})
    except (ValueError, KeyError, CSVError) as error:
        raise ObservationSourceError('NOAA 时序数据未通过 CSV 与时刻校验') from error

    digest = hashlib.sha256(data).hexdigest()
    version = hashlib.sha256(f'{url}\n{digest}'.encode()).hexdigest()
    prefix = f'D-post/observations/{version}'
    receipt = {'schema': 'vibeflow.noaa-observations.v1', 'provider': 'noaa-coops', 'source': _SOURCE,
               'source_url': url, 'license_url': _TERMS, 'station': station, 'product': product,
               'begin': begin.isoformat(), 'end': end.isoformat(), 'path': prefix + '.csv',
               'receipt': prefix + '.json', 'sha256': digest, 'size': len(data), 'http_status': 200,
               'timeColumn': 'Date Time', 'valueColumn': value_column, 'timeZone': 'UTC',
               'unit': _UNITS[units], 'datum': datum, 'records': len(times), 'quality_flags': quality,
               'first_utc': min(times).isoformat(), 'last_utc': max(times).isoformat()}
    return data, receipt


async def _fetch_data(url, allowed_types, source):
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=15, sock_connect=5, sock_read=8)
    body = bytearray()
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(), headers={'Accept-Encoding': 'identity'}) as client:
            async with client.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ObservationSourceError(f'{source} 数据请求返回 HTTP {response.status}')
                if (response.content_type not in allowed_types or
                        (response.content_length is not None and response.content_length > MAX_SERIES_BYTES)):
                    raise ObservationSourceError(f'{source} 返回了不支持的内容或超过大小限制')
                async for chunk in response.content.iter_chunked(16384):
                    if len(body) + len(chunk) > MAX_SERIES_BYTES:
                        raise ObservationSourceError(f'{source} 文件超过 2 MiB')
                    body.extend(chunk)
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError, FetchSecurityError) as error:
        raise ObservationSourceError(f'{source} 官方数据请求未完成') from error
    return bytes(body)


def _wind_rows(data, section):
    body = data.decode('ascii')
    if not re.search(r'^variables="ID","y","u",', body, re.M):
        raise ValueError('Unexpected archive columns')
    labels = (block.partition('\n') for block in body.split('zone,t=')[1:])
    sections = [content.splitlines() for label, _, content in labels
                if label.rstrip('\r') == f'"exp, x/H={section}"']
    if len(sections) != 1:
        raise ValueError('Missing or duplicate section')
    height = Decimal('0.0127')
    records = {}
    for line in sections[0]:
        if not line.strip():
            continue
        _, y, u, *_ = line.split()
        position, velocity = Decimal(y), Decimal(u)
        if (not position.is_finite() or not velocity.is_finite() or not 0 < position < 9
                or abs(velocity) > 10 or position in records):
            raise ValueError('Invalid or duplicate measurement')
        records[position] = velocity
    if not 2 <= len(records) <= 1000:
        raise ValueError('Invalid measurement count')
    output = StringIO(newline='')
    stream = writer(output, lineterminator='\n')
    stream.writerow(['x', 'y', 'z', 'value'])
    stream.writerows((section*height, y*height, height/2, value) for y,value in sorted(records.items()))
    return output.getvalue().encode('ascii'), len(records)


async def fetch_wind_tunnel(section):
    """Acquire a fixed experimental archive and materialize a spatial profile in SI coordinates."""
    if type(section) is not int or section not in _WIND_SECTIONS:
        raise ValueError('后向台阶实验不包含该 x/H 截面')
    raw = await _fetch_data(_WIND_TUNNEL, ('application/octet-stream', 'text/plain'), 'TMR')
    try:
        profile, samples = _wind_rows(raw, section)
    except (ValueError, InvalidOperation) as error:
        raise ObservationSourceError('TMR 后向台阶档案未通过坐标和速度校验') from error
    digest, profile_digest = (hashlib.sha256(data).hexdigest() for data in (raw, profile))
    version = hashlib.sha256(f'{_WIND_TUNNEL}\n{section}\n{digest}\n{profile_digest}'.encode()).hexdigest()
    prefix = f'D-post/observations/{version}'
    receipt = {'schema': 'vibeflow.wind-tunnel-profile.v1', 'provider': 'turbulence-modeling-resource',
               'source': 'Driver and Seegmiller (1985), 2D backward-facing step', 'source_url': _WIND_TUNNEL,
               'citation_url': 'https://doi.org/10.2514/3.8890', 'reference_url': _WIND_REFERENCE,
               'http_status': 200, 'section': section, 'height_m': 0.0127, 'unit': '1', 'coordinateUnit': 'm',
               'quantity': 'U/U_ref', 'records': samples, 'path': prefix + '.csv', 'sourcePath': prefix + '.dat',
               'receipt': prefix + '.json', 'sha256': profile_digest, 'size': len(profile),
               'source_sha256': digest, 'source_size': len(raw)}
    return {receipt['path']: (profile, 'text/csv; charset=utf-8'),
            receipt['sourcePath']: (raw, 'application/octet-stream')}, receipt
