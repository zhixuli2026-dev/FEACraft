"""生成最小合法 PDF（xref 表动态计算）并上传到 case"""
import json
import urllib.request

parts = []           # (offset_in_body, bytes)
body = b""
objects = [
    b"<</Type/Catalog/Pages 2 0 R>>",
    b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
    b"<</Length 70>>stream\nBT /F1 28 Tf 80 720 Td (VibeFlow PDF OK - hello viewer) Tj ET\nendstream",
    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
]

header = b"%PDF-1.4\n"
body += header
offsets = []
for i, obj in enumerate(objects, 1):
    offsets.append(len(body))
    body += f"{i} 0 obj".encode() + obj + b"endobj\n"

xref_pos = len(body)
n = len(objects) + 1
xref = f"xref\n0 {n}\n0000000000 65535 f \n".encode()
for off in offsets:
    xref += f"{off:010d} 00000 n \n".encode()
body += xref
body += (f"trailer<</Size {n}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF").encode()

tok = json.loads(urllib.request.urlopen(urllib.request.Request(
    "http://localhost:8000/api/v1/auth/login", method="POST",
    data=json.dumps({"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
    headers={"Content-Type": "application/json"})).read())["token"]

boundary = "----vf2"
payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"test.pdf\"\r\n"
           "Content-Type: application/pdf\r\n\r\n").encode() + body + f"\r\n--{boundary}--\r\n".encode()
req = urllib.request.Request(
    "http://localhost:8000/api/v1/cases/1/upload?path=Ref/test.pdf", method="POST",
    data=payload, headers={"Authorization": f"Bearer {tok}",
                           "Content-Type": f"multipart/form-data; boundary={boundary}"})
print(json.loads(urllib.request.urlopen(req).read()), "pdf bytes:", len(body))
