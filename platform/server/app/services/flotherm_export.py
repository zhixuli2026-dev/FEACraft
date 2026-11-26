"""Run a locally installed FloTHERM exporter with a private FloSCRIPT stage."""

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as xml
import zipfile

from .flotherm_output import _ordinary_output


DRIVE_PATH = re.compile(r'^[a-zA-Z]:\\[^"%!&^|<>\r\n]+$')
EXPORTS = {'model.ecxml': 'ECXML', 'model.xtxmla': 'XTXml'}


def wsl_path(option, path):
    result = subprocess.run(['wslpath', option, str(path)], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def windows_path(path):
    value = str(path.resolve()) if os.name=='nt' else wsl_path('-w', path.resolve())
    if not DRIVE_PATH.fullmatch(value):
        raise ValueError('FloTHERM 暂存路径必须位于 Windows 本地盘，且不含命令控制字符')
    return value


def find_launcher(value):
    value = value or os.environ.get('FLOTHERM_BAT')
    if value:
        if os.name!='nt' and PureWindowsPath(value).drive:
            value = wsl_path('-u', value)
        candidate = Path(value).resolve(strict=True)
    else:
        folders = ([Path(os.environ.get('ProgramFiles', r'C:\Program Files'))] if os.name=='nt'
                   else [Path('/mnt/c/Program Files')])
        legacy = ('FloTHERM', 'WinXP', 'bin', 'flotherm.bat')
        candidate = next((folder.joinpath(*legacy) for folder in folders if folder.joinpath(*legacy).is_file()), None)
        if candidate is None:
            raise ValueError('未找到 FloTHERM 运行程序。请用 --flotherm-bat 或 FLOTHERM_BAT 指定已安装的程序')
    if not candidate.is_file() or candidate.suffix.lower() not in {'.bat', '.exe'}:
        raise ValueError('FloTHERM 运行程序必须是已安装的 .bat 或 .exe 文件')
    windows_path(candidate)
    return candidate


def script(stage, products):
    root = xml.Element('xml_log_file', version='1.0')
    xml.SubElement(root, 'project_import', filename=windows_path(stage/'model.pack'), import_type='Pack File')
    for name, format_name in EXPORTS.items():
        xml.SubElement(root, 'project_export', filename=windows_path(products/name), export_type=format_name)
    xml.SubElement(root, 'quit')
    path = stage/'export.xml'
    xml.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path


def run_launcher(launcher, stage, vendor_script, products):
    env = {**os.environ, 'FLOUSERDIR': windows_path(stage/'user')}
    forwarded = ['FLOUSERDIR']
    args = [str(launcher), '-f', windows_path(vendor_script)]
    if launcher.suffix.lower()=='.bat':
        command = stage/'run-flotherm.cmd'
        env.update(VIBEFLOW_FLOTHERM_LAUNCHER=windows_path(launcher), VIBEFLOW_FLOSCRIPT=args[-1])
        forwarded.extend(('VIBEFLOW_FLOTHERM_LAUNCHER', 'VIBEFLOW_FLOSCRIPT'))
        command.write_text('@echo off\ncall "%VIBEFLOW_FLOTHERM_LAUNCHER%" -f "%VIBEFLOW_FLOSCRIPT%"\n'
                           'exit /b %errorlevel%\n', encoding='ascii', newline='\r\n')
        args = ['cmd.exe', '/d', '/v:off', '/c', '.\\'+command.name]
    if os.name!='nt':
        env['WSLENV'] = ':'.join(filter(None, (env.get('WSLENV'), *forwarded)))
    with (products/'flotherm.stdout.log').open('wb') as stdout, (products/'flotherm.stderr.log').open('wb') as stderr:
        return subprocess.run(args, env=env, cwd=stage, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr).returncode


def validate_products(products, return_code):
    invalid = []
    for name in EXPORTS:
        product = products/name
        try:
            if product.is_symlink() or not product.is_file():
                raise ValueError('缺少正常文件')
            root = xml.parse(product).getroot().tag.rsplit('}', 1)[-1]
            expected = 'neutralXML' if name.endswith('.ecxml') else 'xtxml_assembly'
            if root != expected:
                raise ValueError('导出文件根元素与目标格式不符')
        except (OSError, ValueError, xml.ParseError, LookupError):
            invalid.append(name)
    if invalid:
        raise ValueError(f'FloTHERM 退出码 {return_code}，缺少完整有效的导出 {invalid}')


def export_pack(source, destination, *, launcher_path, force, keep_stage, publish, observe_logs=None):
    if os.name!='nt' and shutil.which('wslpath') is None:
        raise ValueError('Pack 导出需要可调用 Windows FloTHERM 的 Windows 或 WSL 环境')
    source = source.resolve(strict=True)
    if source.suffix.lower()!='.pack' or not zipfile.is_zipfile(source):
        raise ValueError('FloTHERM 导出需要可读取的 .pack 归档')
    output = _ordinary_output(destination)
    if output.parent==output or source.is_relative_to(output):
        raise ValueError('导出目录不能是根目录或包含 Pack 原件')
    if output.is_dir() and any(output.iterdir()) and not force:
        raise FileExistsError('导出目录非空，请换空目录或显式使用 --force')
    launcher = find_launcher(launcher_path)
    windows_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.flotherm-export-', dir=output.parent)).resolve()
    try:
        staged_source = stage/'model.pack'
        shutil.copyfile(source, staged_source)
        (stage/'user').mkdir()
        products = stage/'exported'
        products.mkdir()
        vendor_script = script(stage, products)
        try:
            result = run_launcher(launcher, stage, vendor_script, products)
        finally:
            if observe_logs is not None:
                observe_logs(products)
        validate_products(products, result)
        with staged_source.open('rb') as stream:
            sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        shutil.copyfile(vendor_script, products/'export.floscript.xml')
        receipt = {'ok':True,'source':{'name':source.name,'sha256':sha256},'return_code':result,
                   'output':str(output),'exported':list(EXPORTS),'stage':str(stage) if keep_stage else None,
                   'warnings':[f'FloTHERM 返回码 {result}，两个导出文件均通过格式检查'] if result else []}
        (products/'export-manifest.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        publish(products, output, force=force)
        return receipt
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = f'；暂存目录 {stage}' if keep_stage else '；使用 --keep-stage 可保留原生日志'
        raise ValueError(f'{error}{detail}') from error
    finally:
        if not keep_stage:
            shutil.rmtree(stage)
