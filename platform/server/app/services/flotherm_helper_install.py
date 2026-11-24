"""Install versioned FloTHERM OpenFOAM helpers into the selected user library directory."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4


HELPERS = {'libvibeflowFlothermTwoR.so', 'libvibeflowFlothermFixedFlow.so'}
STORE = '.vibeflow-flotherm-helpers'
MAX_BYTES = 32 * 1024 * 1024


def read_library(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES+1)
    if not data.startswith(b'\x7fELF') or len(data)>MAX_BYTES:
        raise ValueError('辅助库需要可核验大小的原生 ELF 文件')
    return data


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked_root(directory):
    if not sys.platform.startswith('linux'):
        raise ValueError('辅助库安装在已加载 OpenFOAM 的 Linux 或 WSL 中运行')
    import fcntl

    configured = directory or os.environ.get('FOAM_USER_LIBBIN')
    if not configured:
        raise ValueError('先加载 OpenFOAM 环境或明确提供 --library-dir')
    root=Path(configured).expanduser().resolve()
    root.mkdir(parents=True,exist_ok=True)
    store=root/STORE
    store.mkdir(exist_ok=True)
    if store.is_symlink() or not store.is_dir():
        raise ValueError('辅助库版本目录必须是本地普通目录')
    fd=os.open(store/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'rb+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield root,store


def save_version(store, name, data):
    sha=hashlib.sha256(data).hexdigest()
    releases=store/name
    releases.mkdir(exist_ok=True)
    version=releases/sha
    if releases.is_symlink() or version.is_symlink():
        raise ValueError('辅助库版本不能经过符号链接')
    target=version/name
    if version.exists():
        if target.is_symlink() or read_library(target)!=data:
            raise ValueError('已存辅助库版本与摘要不符')
        return sha,target
    with tempfile.TemporaryDirectory(prefix='.stage-',dir=releases) as temporary:
        stage=Path(temporary)/'prepared'
        stage.mkdir()
        with (stage/name).open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(stage)
        stage.rename(version)
        sync_directory(releases)
    return sha,target


def verify_load(path):
    libbin=os.environ.get('FOAM_LIBBIN')
    if not libbin or not os.environ.get('WM_PROJECT_VERSION'):
        raise ValueError('安装需要已加载原生 OpenFOAM 环境')
    libbin=Path(libbin)
    try:
        dependencies=subprocess.run(['readelf','-d',str(path)],capture_output=True,text=True,timeout=20)
    except subprocess.TimeoutExpired as error:
        raise ValueError('读取辅助库 ELF 依赖超时') from error
    if dependencies.returncode!=0:
        raise ValueError('readelf 无法读取原生辅助库依赖')
    needed=re.findall(r'\(NEEDED\).*?\[([^]]+)\]',dependencies.stdout)
    if 'libOpenFOAM.so' not in needed:
        raise ValueError('辅助库没有链接原生 OpenFOAM')
    runtime=[libbin/name for name in ('libOpenFOAM.so','libfiniteVolume.so','libmeshTools.so','libfileFormats.so',
                                      'libsurfMesh.so','sys-openmpi/libPstream.so')]
    if not all(item.is_file() for item in runtime):
        raise ValueError('当前 OpenFOAM 的辅助库运行依赖不完整')
    env=dict(os.environ,LD_PRELOAD=':'.join(map(str,runtime)))
    try:
        loader=subprocess.run([sys.executable,'-I','-S','-c',
            'import ctypes, sys; ctypes.CDLL(sys.argv[1], mode=ctypes.RTLD_GLOBAL)',str(path)],
            env=env,capture_output=True,text=True,timeout=30)
    except subprocess.TimeoutExpired as error:
        raise ValueError('原生辅助库加载超时') from error
    if loader.returncode!=0:
        message=loader.stderr.strip().rsplit('\n',1)[-1][:500] or 'exit '+str(loader.returncode)
        raise ValueError('原生加载器拒绝辅助库：'+message)
    return sorted(set(needed))


def active_hash(target, store):
    if not os.path.lexists(target):
        return None
    if target.is_symlink():
        source=target.resolve(strict=True)
        expected=store/target.name
        if source.parent.parent!=expected or source.name!=target.name:
            raise ValueError('现有辅助库链接由其它位置管理')
        sha=hashlib.sha256(read_library(source)).hexdigest()
        if sha!=source.parent.name:
            raise ValueError('现有辅助库链接与版本摘要不符')
        return sha
    if not target.is_file():
        raise ValueError('辅助库安装位置需要普通文件或已登记版本')
    sha,_=save_version(store,target.name,read_library(target))
    return sha


def activate(name, directory, *, source=None, version=None):
    if name not in HELPERS:
        raise ValueError('请选择已登记的 FloTHERM 2R 或定流量风扇辅助库')
    with locked_root(directory) as (root,store):
        target=root/name
        if source is not None:
            data=read_library(source)
            with tempfile.TemporaryDirectory(prefix='.check-',dir=store) as temporary:
                candidate=Path(temporary)/name
                candidate.write_bytes(data)
                needed=verify_load(candidate)
            sha,path=save_version(store,name,data)
        else:
            if not isinstance(version,str) or not re.fullmatch('[0-9a-f]{64}',version):
                raise ValueError('回退需要原版本的完整 SHA-256')
            sha=version
            path=store/name/sha/name
            if path.resolve(strict=True)!=path or hashlib.sha256(read_library(path)).hexdigest()!=sha:
                raise ValueError('回退版本的原生库与摘要不符')
            needed=verify_load(path)
        previous=active_hash(target,store)
        temporary=root/('.flotherm-helper-'+uuid4().hex)
        temporary.symlink_to(path.relative_to(root))
        try:
            os.replace(temporary,target)
            sync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)
    return {'ok':True,'library':name,'path':str(target),'sha256':sha,'previous_sha256':previous,
            'openfoam':os.environ['WM_PROJECT_VERSION'],'linked_libraries':needed,
            'operation':'install' if source is not None else 'rollback'}
