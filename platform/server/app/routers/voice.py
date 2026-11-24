"""Short, case-scoped dictation through an optional server-side Whisper CLI."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services.async_ops import finish_before_cancel
from ..services.request_lifecycle import wait
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1', tags=['voice'])
MAX_SECONDS = 30
SAMPLE_RATE = 16000
PCM_HEADER = struct.Struct('<4sI4s4sIHHIIHH4sI')
MAX_BYTES = PCM_HEADER.size + MAX_SECONDS * SAMPLE_RATE * 2
_SLOTS = asyncio.Semaphore(2)


def runtime():
    cfg = get_settings()
    paths = tuple(Path(name) for name in (cfg.voice_transcriber_path,cfg.voice_model_path))
    return paths if all(path.is_absolute() and path.is_file() for path in paths) else None


@router.get('/voice/capabilities')
async def capabilities(user:User=Depends(get_current_user)):
    return {'server_transcription':runtime() is not None,'max_seconds':MAX_SECONDS}


def validate_wave(audio):
    if len(audio)<PCM_HEADER.size:
        raise HTTPException(422,'录音为空或格式不正确')
    header=PCM_HEADER.unpack_from(audio)
    if (header!=(b'RIFF',len(audio)-8,b'WAVE',b'fmt ',16,1,1,SAMPLE_RATE,SAMPLE_RATE*2,
                 2,16,b'data',len(audio)-PCM_HEADER.size)
            or (len(audio)-PCM_HEADER.size)%2 or len(audio)<PCM_HEADER.size+SAMPLE_RATE//4):
        raise HTTPException(422,'请提供 16 kHz 单声道录音')
    if not any(abs(sample)>96 for sample in memoryview(audio)[PCM_HEADER.size:].cast('h')):
        raise HTTPException(422,'录音中未检测到声音，请检查麦克风')


async def recognize(audio,paths,language):
    cli,model=paths
    with tempfile.TemporaryDirectory(prefix='vf-voice-') as directory:
        work=Path(directory)
        incoming,output=work/'input.wav',work/'result'
        incoming.write_bytes(audio)
        launch=None
        try:
            async with asyncio.timeout(24):
                launch=asyncio.create_task(asyncio.create_subprocess_exec(str(cli),'-m',str(model),'-f',str(incoming),
                    '-l',language,'-ng','-t','2','-nt','-oj','-of',str(output),cwd=work,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {})))
                process=await asyncio.shield(launch)
                if await process.wait():
                    raise ValueError('Transcription failed')
            result=output.with_suffix('.json')
            with result.open('rb') as stream:
                raw=stream.read(65537)
            if len(raw)>65536:
                raise ValueError('Transcription response too large')
            segments=json.loads(raw)['transcription']
            value=' '.join(item['text'].strip() for item in segments if item['text'].strip()).strip()
            value=value.replace('[BLANK_AUDIO]','').strip()
            return value[:4000]
        except TimeoutError:
            raise HTTPException(504,'语音转写超时，请缩短录音后重试') from None
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            raise HTTPException(502,'语音转写暂不可用，请重试或输入文字') from None
        finally:
            if launch is not None:
                await finish_before_cancel(stop(launch))


async def stop(launch):
    try:
        process=await launch
    except OSError:
        return
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


@router.put('/cases/{case_id}/voice/transcription')
async def transcription(case_id:int,request:Request,language:Literal['zh','en','auto']='auto',
                        user:User=Depends(get_current_user),db=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    await db.rollback()
    paths=runtime()
    if paths is None:
        raise HTTPException(503,'服务器语音转写未启用')
    if _SLOTS.locked():
        raise HTTPException(429,'语音转写正忙，请稍后重试')
    async with _SLOTS:
        audio=bytearray()
        async for chunk in request.stream():
            if len(audio)+len(chunk)>MAX_BYTES:
                raise HTTPException(413,'录音长度不能超过 30 秒')
            audio.extend(chunk)
        validate_wave(audio)
        value=await wait(recognize(audio,paths,language),request=request)
    if not value:
        raise HTTPException(422,'未识别到可转写语音，请重新录音')
    return {'text':value}
