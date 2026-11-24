#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
innosplit.py - Extractor for Inno Setup 6.7 installers (revision-2 loader).

Format references:
  - issrc is-6_7_0 Projects/Src/Shared.Struct.pas      (record layouts)
  - issrc is-6_7_0 Projects/Src/Compression.Base.pas   (TCompressedBlockReader)
  - issrc is-6_7_0 Projects/Src/Shared.SetupEntFunc.pas(string storage)
  - issrc is-6_7_0 Projects/Src/Setup.FileExtractor.pas(chunk/zlb format)
  - innoextract master src/loader/offsets.cpp          (loader table)
"""
import struct, zlib, lzma, json, os, sys, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP_PATH = ROOT / 'VibeFlowStudio-0.11.0-win-x64-setup.exe'
OUT_DIR = ROOT / 'extracted'
MANIFEST = ROOT / 'extracted-manifest.json'

# ---------- binary reader ----------
class R:
    def __init__(self, data, pos=0):
        self.d = data; self.p = pos
    def take(self, n):
        b = self.d[self.p:self.p+n]
        if len(b) != n: raise EOFError(f'need {n} bytes at {self.p}, got {len(b)}')
        self.p += n
        return b
    def u8(self):  return self.take(1)[0]
    def u16(self): return struct.unpack('<H', self.take(2))[0]
    def u32(self): return struct.unpack('<I', self.take(4))[0]
    def i16(self): return struct.unpack('<h', self.take(2))[0]
    def i32(self): return struct.unpack('<i', self.take(4))[0]
    def i64(self): return struct.unpack('<q', self.take(8))[0]
    def ustr(self):  # Delphi String: u32 byte length + UTF-16LE
        n = self.u32()
        if n == 0: return ''
        return self.take(n).decode('utf-16-le', 'replace')
    def astr(self):  # Delphi AnsiString: u32 byte length + raw bytes
        n = self.u32()
        if n == 0: return ''
        return self.take(n).decode('utf-8', 'replace')

def read_version_data(r):
    return {'win': r.u32(), 'nt': r.u32(), 'nt_sp': r.u16()}

# ---------- LZMA helpers ----------
def parse_lzma1_props(b):
    lc = b % 9; rem = b // 9; lp = rem % 5; pb = rem // 5
    return lc, lp, pb

def lzma1_decompress(buf):
    """Inno LZMA1 stream: 1B props + 4B dict_size + raw LZMA1 (with EOS marker)."""
    props = buf[0]; dict_size = struct.unpack('<I', buf[1:5])[0]
    lc, lp, pb = parse_lzma1_props(props)
    d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
        'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}])
    out = d.decompress(buf[5:])
    return out, d.eof

def lzma2_decompress(buf):
    """Inno LZMA2 stream: 1B dict-size prop + raw LZMA2 (end marker 0x00)."""
    prop = buf[0]
    dict_size = (2 | (prop & 1)) << (prop // 2 + 11)
    d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
        'id': lzma.FILTER_LZMA2, 'dict_size': dict_size}])
    out = d.decompress(buf[1:])
    return out, d.eof

# ---------- compressed block (header blocks) ----------
def read_compressed_block(f, pos):
    """TCompressedBlockReader: u32 crc, i64 stored_size, u8 compressed,
    then [u32 crc][<=4096 data]* chunks. Returns (payload, next_file_pos)."""
    f.seek(pos)
    hdr_crc = struct.unpack('<I', f.read(4))[0]
    hdr = f.read(9)
    stored_size, compressed = struct.unpack('<qB', hdr)
    assert zlib.crc32(hdr) & 0xffffffff == hdr_crc, 'block header CRC mismatch'
    raw = bytearray()
    left = stored_size
    while left > 0:
        crc = struct.unpack('<I', f.read(4))[0]; left -= 4
        n = min(4096, left)
        chunk = f.read(n)
        assert len(chunk) == n
        assert zlib.crc32(chunk) & 0xffffffff == crc, 'subblock CRC mismatch'
        raw += chunk; left -= n
    next_pos = f.tell()
    if compressed:
        payload, eof = lzma1_decompress(bytes(raw))
        print(f'    block at {pos:#x}: stored={stored_size} lzma1 -> {len(payload)} bytes (eos={eof})')
        return payload, next_pos
    return bytes(raw), next_pos

# ---------- entry parsers (field order per Shared.Struct.pas 6.7.0) ----------
def parse_language(r):
    e = {'name': r.ustr(), 'language_name': r.ustr(), 'dialog_font': r.ustr(), 'welcome_font': r.ustr()}
    for _ in range(4): r.astr()  # Data, LicenseText, InfoBeforeText, InfoAfterText
    e['language_id'] = r.u16()
    e['dialog_font_size'] = r.i32(); r.i32(); r.i32()  # base scale h/w
    e['welcome_font_size'] = r.i32()
    e['rtl'] = r.u8()
    return e

def parse_custom_message(r):
    return {'name': r.ustr(), 'value': r.ustr(), 'lang_index': r.i32()}

def parse_permission(r):
    r.astr()
    return {}

def parse_type(r):
    e = {'name': r.ustr(), 'desc': r.ustr(), 'languages': r.ustr(), 'check_once': r.ustr()}
    read_version_data(r); read_version_data(r)
    e['options'] = r.u8(); e['typ'] = r.u8(); e['size'] = r.i64()
    return e

def parse_component(r):
    e = {'name': r.ustr(), 'desc': r.ustr(), 'types': r.ustr(), 'languages': r.ustr(), 'check_once': r.ustr()}
    e['extra_disk'] = r.i64(); e['level'] = r.u8(); e['used'] = r.u8()
    read_version_data(r); read_version_data(r)
    e['options'] = r.u8(); e['size'] = r.i64()
    return e

def parse_task(r):
    e = {'name': r.ustr(), 'desc': r.ustr(), 'group_desc': r.ustr(),
         'components': r.ustr(), 'languages': r.ustr(), 'check': r.ustr()}
    e['level'] = r.u8(); e['used'] = r.u8()
    read_version_data(r); read_version_data(r)
    e['options'] = r.u8()
    return e

def parse_dir(r):
    e = {'dir': r.ustr(), 'components': r.ustr(), 'tasks': r.ustr(), 'languages': r.ustr(),
         'check': r.ustr(), 'after_install': r.ustr(), 'before_install': r.ustr()}
    e['attribs'] = r.i32()
    read_version_data(r); read_version_data(r)
    e['permissions_entry'] = r.i16(); e['options'] = r.u8()
    return e

def parse_issig_key(r):
    return {'public_x': r.ustr(), 'public_y': r.ustr(), 'runtime_id': r.ustr()}

def parse_file(r):
    names = ['source', 'dest', 'install_font_name', 'strong_assembly_name', 'components',
             'tasks', 'languages', 'check', 'after_install', 'before_install', 'excludes',
             'download_issig_source', 'download_user', 'download_password', 'extract_archive_password']
    e = {n: r.ustr() for n in names}
    e['issig_allowed_keys'] = r.astr()          # Verification.ISSigAllowedKeys (ansi)
    e['sha256'] = r.take(32).hex()              # Verification.Hash
    e['verification_typ'] = r.u8()              # 0 none, 1 hash, 2 issig
    read_version_data(r); read_version_data(r)  # MinVersion, OnlyBelowVersion
    e['location_entry'] = r.i32()
    e['attribs'] = r.i32()
    e['external_size'] = r.i64()
    e['permissions_entry'] = r.i16()
    e['options'] = r.take(8).hex()              # 57-flag set
    e['file_type'] = r.u8()                     # 0 user file, 1 uninstaller exe
    return e

def parse_location(r):
    e = {'first_slice': r.i32(), 'last_slice': r.i32(),
         'start_offset': r.i64(), 'chunk_suboffset': r.i64(),
         'original_size': r.i64(), 'chunk_compressed_size': r.i64(),
         'sha256': r.take(32).hex(),
         'timestamp_filetime': r.i64(),
         'file_version_ms': r.u32(), 'file_version_ls': r.u32(),
         'flags': r.u8()}
    # flags: 1 version info valid, 2 timestamp UTC, 4 call-instr optimized,
    #        8 chunk encrypted, 16 chunk compressed
    return e

# ---------- header ----------
HEADER_STRINGS = ['app_name','app_ver_name','app_id','app_copyright','app_publisher',
    'app_publisher_url','app_support_phone','app_support_url','app_updates_url','app_version',
    'default_dir_name','default_group_name','base_filename','uninstall_files_dir',
    'uninstall_display_name','uninstall_display_icon','app_mutex','default_user_info_name',
    'default_user_info_org','default_user_info_serial','app_readme_file','app_contact',
    'app_comments','app_modify_path','create_uninstall_reg_key','uninstallable',
    'close_applications_filter','setup_mutex','changes_environment','changes_associations',
    'architectures_allowed','architectures_install_64bit','close_applications_filter_excludes',
    'sevenzip_library_name','use_previous_app_dir','use_previous_group','use_previous_setup_type',
    'use_previous_tasks','use_previous_user_info']
HEADER_COUNTS = ['num_language','num_custom_message','num_permission','num_type','num_component',
    'num_task','num_dir','num_issig_key','num_file','num_file_location','num_icon','num_ini',
    'num_registry','num_install_delete','num_uninstall_delete','num_run','num_uninstall_run']

def parse_header(r):
    h = {}
    for n in HEADER_STRINGS: h[n] = r.ustr()
    h['license_text_len'] = len(r.astr()); h['info_before_len'] = len(r.astr())
    h['info_after_len'] = len(r.astr()); h['compiled_code_len'] = len(r.astr())
    for n in HEADER_COUNTS: h[n] = r.i32()
    h['min_version'] = read_version_data(r)
    h['only_below_version'] = read_version_data(r)
    h['wizard_size_x'] = r.i32(); h['wizard_size_y'] = r.i32()
    h['wizard_dark_style'] = r.u8(); h['wizard_image_alpha_format'] = r.u8()
    for _ in range(6): r.i32()                    # wizard colors
    r.u8(); r.u8()                                # opacities
    r.u8()                                        # light control styling
    h['extra_disk_required'] = r.i64()
    h['slices_per_disk'] = r.i32()
    h['uninstall_log_mode'] = r.u8(); h['dir_exists_warning'] = r.u8()
    h['privileges_required'] = r.u8(); h['privileges_overrides'] = r.u8()
    h['show_language_dialog'] = r.u8(); h['language_detection'] = r.u8()
    h['compress_method'] = r.u8()                 # 0 stored 1 zlib 2 bzip2 3 lzma1 4 lzma2
    h['disable_dir_page'] = r.u8(); h['disable_group_page'] = r.u8()
    h['uninstall_display_size'] = r.i64()
    h['options'] = r.take(8).hex()
    return h

# ---------- main ----------
def main():
    f = open(SETUP_PATH, 'rb')

    # 1. loader table (found by magic scan; revision 2, i64 offsets)
    magic = b'rDlPtS\xcd\xe6\xd7{\x0b*'
    head = open(SETUP_PATH,'rb').read(1024*1024)
    pos = head.find(magic)
    assert pos >= 0, 'loader magic not found'
    f.seek(pos + 12)
    version = struct.unpack('<I', f.read(4))[0]
    total_size, offset_exe = struct.unpack('<qq', f.read(16))
    uncmp_exe, crc_exe = struct.unpack('<Ii', f.read(8))
    offset0, offset1 = struct.unpack('<qq', f.read(16))
    print(f'loader: rev={version} total={total_size} offset0={offset0:#x} (headers) offset1={offset1:#x} (file data)')

    # 2. setup id + encryption header
    f.seek(offset0)
    setup_id = f.read(64).split(b'\0')[0].decode()
    enc_crc = struct.unpack('<I', f.read(4))[0]
    enc = f.read(49)
    assert zlib.crc32(enc) & 0xffffffff == enc_crc, 'encryption header CRC mismatch'
    enc_use = enc[0]
    print(f'setup id: {setup_id!r}  encryption_use={enc_use} (0=none)')
    assert enc_use == 0, 'encrypted installer not supported'

    # 3. block 1: header + entry tables
    block1, block2_pos = read_compressed_block(f, offset0 + 64 + 4 + 49)
    r = R(block1)
    h = parse_header(r)
    print(f'app: {h["app_name"]!r} {h["app_ver_name"]!r} id={h["app_id"]!r}')
    print(f'counts: ' + ' '.join(f'{n.replace("num_","")}={h[n]}' for n in HEADER_COUNTS))
    print(f'compress_method={h["compress_method"]} default_dir={h["default_dir_name"]!r}')

    langs     = [parse_language(r)       for _ in range(h['num_language'])]
    messages  = [parse_custom_message(r) for _ in range(h['num_custom_message'])]
    for _ in range(h['num_permission']): parse_permission(r)
    types     = [parse_type(r)           for _ in range(h['num_type'])]
    comps     = [parse_component(r)      for _ in range(h['num_component'])]
    tasks     = [parse_task(r)           for _ in range(h['num_task'])]
    dirs      = [parse_dir(r)            for _ in range(h['num_dir'])]
    issigkeys = [parse_issig_key(r)      for _ in range(h['num_issig_key'])]
    files     = [parse_file(r)           for _ in range(h['num_file'])]
    print(f'parsed: {len(langs)} langs, {len(messages)} messages, {len(files)} files')

    # 4. block 2: file location entries
    block2, _ = read_compressed_block(f, block2_pos)
    r2 = R(block2)
    locations = [parse_location(r2) for _ in range(h['num_file_location'])]
    print(f'locations: {len(locations)} (block2 bytes used: {r2.p}/{len(block2)})')

    # 5. manifest
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = {'setup': str(SETUP_PATH), 'setup_id': setup_id, 'header': h,
                'languages': langs, 'types': types, 'components': comps,
                'tasks': tasks, 'dirs': dirs, 'files': files, 'locations': locations}
    with open(MANIFEST, 'w', encoding='utf-8') as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=1)
    print(f'manifest written: {MANIFEST}')

    # 6. extract files
    method_names = {0:'stored',1:'zlib',2:'bzip2',3:'lzma1',4:'lzma2'}
    method = h['compress_method']
    print(f'extracting with method={method_names.get(method, method)} ...')
    chunk_cache = {}
    n_ok = n_skip = n_fail = 0
    for i, fe in enumerate(files):
        li = fe['location_entry']
        dest = fe['dest'] or fe['source']
        if li < 0:
            n_skip += 1
            continue
        loc = locations[li]
        try:
            key = (loc['first_slice'], loc['start_offset'], loc['chunk_compressed_size'])
            if key not in chunk_cache:
                chunk_cache.clear()   # files are ordered by chunk; keep only the active one
                if loc['first_slice'] != 0:
                    raise RuntimeError('external slice not supported')
                f.seek(offset1 + loc['start_offset'])
                magic_b = f.read(4)
                assert magic_b == b'zlb\x1a', f'bad chunk magic {magic_b!r}'
                comp = f.read(loc['chunk_compressed_size'])
                if loc['flags'] & 16:  # chunk compressed
                    if method == 4:
                        data, eof = lzma2_decompress(comp)
                    elif method == 3:
                        data, eof = lzma1_decompress(comp)
                    elif method == 1:
                        data = zlib.decompress(comp)
                    else:
                        raise RuntimeError(f'method {method} unsupported')
                else:
                    data = comp
                chunk_cache[key] = data
            data = chunk_cache[key]
            sub = loc['chunk_suboffset']
            payload = data[sub:sub + loc['original_size']]
            if len(payload) != loc['original_size']:
                raise RuntimeError(f'short read: {len(payload)} != {loc["original_size"]}')
            digest = hashlib.sha256(payload).hexdigest()
            if digest != loc['sha256']:
                # call-instruction filter may need reversing
                if loc['flags'] & 4:
                    payload = unfilter(payload)
                    digest = hashlib.sha256(payload).hexdigest()
                assert digest == loc['sha256'], f'sha256 mismatch: {digest}'
            # sanitize dest path
            rel = dest.replace('{app}', '').replace('{cf}', '').replace('{tmp}', '')
            rel = rel.replace('\\', '/').lstrip('/')
            rel = rel.replace('..', '__')
            out = os.path.join(OUT_DIR, rel if rel else f'file_{i}')
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as fp:
                fp.write(payload)
            n_ok += 1
        except Exception as ex:
            n_fail += 1
            print(f'  FAIL [{i}] {dest!r}: {ex}')
    print(f'done: {n_ok} extracted, {n_skip} skipped (no location), {n_fail} failed')

def unfilter(data):
    """Reverse Inno's x86 call/jmp instruction optimization.
    Port of TransformCallInstructions(..., Encode=False, AddrOffset) from
    issrc 6.7.0 Compression.Base.pas - must process in 64KB buffers exactly
    like Inno does, with AddrOffset = absolute position within the file."""
    buf = bytearray(data)
    n = len(buf)
    addr_offset = 0
    pos = 0
    while pos < n:
        size = min(65536, n - pos)
        limit = size - 4              # Inno: while I < Size-4 (opcode fully inside buffer)
        if limit <= 0:
            break
        i = 0
        base = pos
        wend = pos + limit
        while i < limit:
            j8 = buf.find(0xE8, base + i, wend)
            j9 = buf.find(0xE9, base + i, wend)
            if j8 < 0: j = j9
            elif j9 < 0: j = j8
            else: j = min(j8, j9)
            if j < 0:
                break
            i = j - base              # local index of opcode
            i1 = j + 1                # abs index of rel32 field
            b3 = buf[i1 + 3]
            if b3 == 0 or b3 == 0xFF:
                addr = (addr_offset + (i + 1) + 4) & 0xFFFFFF
                rel = (buf[i1] | (buf[i1+1] << 8) | (buf[i1+2] << 16)) & 0xFFFFFFFF
                rel = (rel - addr) & 0xFFFFFFFF
                if rel & 0x800000:
                    buf[i1 + 3] = b3 ^ 0xFF
                buf[i1] = rel & 0xFF
                buf[i1+1] = (rel >> 8) & 0xFF
                buf[i1+2] = (rel >> 16) & 0xFF
            i += 5                    # past opcode+rel32, even if not transformed
        addr_offset = (addr_offset + size) & 0xFFFFFFFF
        pos += size
    return bytes(buf)

if __name__ == '__main__':
    main()
