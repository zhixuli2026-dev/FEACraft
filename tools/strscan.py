import re, sys

def scan(path, keywords, limit=40, minlen=6):
    data = open(path, 'rb').read()
    strs = re.findall(rb'[\x20-\x7e]{%d,}' % minlen, data)
    seen = set()
    out = []
    for s in strs:
        sl = s.lower()
        for k in keywords:
            if k in sl and s not in seen:
                seen.add(s)
                out.append(s.decode('utf-8', 'replace'))
                break
    print(f'=== {path} ({len(data)} bytes) ===')
    for s in sorted(out)[:limit]:
        print('  ', s[:160])
    print()

if __name__ == '__main__':
    scan(sys.argv[1], [k.encode() for k in sys.argv[2].split(',')],
         limit=int(sys.argv[3]) if len(sys.argv) > 3 else 40)
