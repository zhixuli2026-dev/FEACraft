"""Disposable real PDF.js/API fixture with equal-length replacement PDFs."""
from io import BytesIO
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

PLATFORM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM/'server'))
from testsupport import IsolatedServer

api = IsolatedServer('pdf-version-browser', app='pdf_version_fixture:app')
writer = PdfWriter()
font = writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),
    NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')}))
for number in range(1, 61):
    page = writer.add_blank_page(width=400, height=500)
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'):font})})
    content = DecodedStreamObject()
    content.set_data(f'BT /F1 16 Tf 30 440 Td (PDFVersionOne page {number}) Tj ET\n'.encode()
                     + b'%'+b'x'*70000+b'\n')
    page[NameObject('/Contents')] = writer._add_object(content)
output = BytesIO()
writer.write(output)
first = output.getvalue()
second = first.replace(b'PDFVersionOne', b'PDFVersionTwo')
assert first != second and len(first) == len(second) and len(first) > 4_000_000
(api.root/'one.pdf').write_bytes(first)
(api.root/'two.pdf').write_bytes(second)
(api.root/'pdf_version_fixture.py').write_text('''
from pathlib import Path
from fastapi import HTTPException,Response
from app.main import app
@app.get('/fixture/pdf/{version}')
def pdf(version:str):
    if version not in {'one','two'}:raise HTTPException(404)
    return Response(Path(__file__).with_name(version+'.pdf').read_bytes(),media_type='application/pdf')
''', encoding='utf8')
with api:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    with (api.root/'web.log').open('w',encoding='utf8') as log:
        web = subprocess.Popen(['node',str(PLATFORM/'web/node_modules/vite/bin/vite.js'),
            '--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=PLATFORM/'web',
            env=dict(api.env,VITE_API_TARGET=api.base),stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            url=f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    urllib.request.urlopen(url,timeout=1).close()
                    break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Disposable UI unavailable')
            print(f'UI_URL={url} API_URL={api.base} PDF_BYTES={len(first)} STOP_FILE={api.root/"stop"}',flush=True)
            for _ in range(1800):
                if (api.root/'stop').exists():break
                if web.poll() is not None:raise RuntimeError('UI stopped')
                time.sleep(1)
            else:raise TimeoutError('Browser acceptance did not stop its owned server')
        finally:
            web.terminate()
            web.wait(timeout=10)
