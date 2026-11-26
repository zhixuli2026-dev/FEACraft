"""Case-owned ParaView Web Visualizer using the installed native wslink stack."""

import argparse
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


INPUT_TYPES = frozenset(('.vtk','.vtp','.vtu','.vti','.vtr','.vts','.stl','.foam'))
OUTPUT_TYPES = INPUT_TYPES- {'.foam','.stl'} | {'.png','.csv','.pvsm'}
RAYTRACE_PROPERTIES = {'EnableRayTracing', 'OSPRayMaterial', 'BackEnd'}


def load_simple():
    """Keep the stock material catalog available without decoding textures on startup."""
    if sys.platform not in {'linux', 'win32'} or 'PV_MATERIALS_PATH' in os.environ:
        from paraview import simple
        return simple, None

    from paraview import servermanager

    version = servermanager.vtkSMProxyManager
    release = f'paraview-{version.GetVersionMajor()}.{version.GetVersionMinor()}'
    installation = Path(sys.executable).resolve().parents[1]
    materials = installation / 'materials' if sys.platform == 'win32' else installation / 'share' / release / 'materials'
    stock = materials / 'ospray_mats.json'
    try:
        catalog = json.loads(stock.read_text(encoding='utf-8'))
        if catalog['family'] != 'OSPRay' or catalog['version'] != '0.0':
            raise ValueError('Unsupported material catalog')
        for material in catalog['materials'].values():
            material.pop('textures', None)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        from paraview import simple
        return simple, None

    with TemporaryDirectory(prefix='vibeflow-paraview-materials-') as directory:
        (Path(directory) / stock.name).write_text(json.dumps(catalog), encoding='utf-8')
        os.environ['PV_MATERIALS_PATH'] = directory
        try:
            from paraview import simple
        finally:
            del os.environ['PV_MATERIALS_PATH']
    return simple, stock


def case_path(root, value):
    if not isinstance(value,str) or not value or '\x00' in value or '\\' in value or ':' in value:
        raise ValueError('Invalid relative case path')
    supplied = Path(value)
    if supplied.is_absolute() or '..' in supplied.parts:
        raise ValueError('Invalid relative case path')
    resolved = (root/supplied).resolve()
    if not resolved.is_relative_to(root) or any(part.startswith('.') for part in resolved.relative_to(root).parts):
        raise ValueError('Visualization files must stay in the visible case folder')
    return resolved


def safe_property(name):
    if not isinstance(name,str):
        return False
    normalized = ''.join(ch for ch in name.casefold() if ch.isalnum())
    return not any(part in normalized for part in ('file','script','shader','program','directory','path','command'))


def start():
    simple, material_textures = load_simple()
    from paraview.web import protocols as pv
    from paraview.web._default_proxies import getDefaultProxies
    from paraview.web.pv_wslink import PVServerProtocol
    from wslink import register as rpc, server

    parser = argparse.ArgumentParser()
    server.add_arguments(parser)
    parser.add_argument('--case-root',required=True)
    parser.add_argument('--input',required=True)
    parser.add_argument('--session-nonce',required=True)
    parser.add_argument('--profile-sha256',required=True)
    arguments = parser.parse_args()
    root = Path(arguments.case_root).resolve(strict=True)
    source = case_path(root,arguments.input)
    if not source.is_file() or source.suffix.casefold() not in INPUT_TYPES:
        raise ValueError('Unsupported native ParaView input')
    options = getDefaultProxies()
    options['readers'] = [entry for entry in options['readers'] if entry['name']=='LegacyVTKReader']
    for group in ('sources','filters'):
        options[group] = [entry for entry in options[group] if safe_property(entry['name']) and 'Python' not in entry['name']]

    def load_material_textures():
        nonlocal material_textures
        if material_textures is not None:
            library = simple.GetMaterialLibrary()
            if library is not None:
                library.SMProxy.LoadMaterials(str(material_textures))
            material_textures = None

    class CaseFiles(pv.ParaViewWebFileListing):
        @rpc('file.server.directory.list')
        def listServerDirectory(self, relativeDir='.'):
            relative = ('.' if relativeDir=='Home' else relativeDir.removeprefix('Home/')) if isinstance(relativeDir,str) else '.'
            try:
                selected = case_path(root,relative)
            except (OSError,ValueError):
                return {'label':'Home','files':[],'dirs':[],'groups':[],'path':['Home']}
            directories, files = [], []
            if selected.is_dir():
                for child in sorted(selected.iterdir(),key=lambda entry:entry.name.casefold()):
                    if child.name.startswith('.') or child.is_symlink():
                        continue
                    if child.is_dir():
                        directories.append(child.name)
                    elif child.is_file() and child.suffix.casefold() in INPUT_TYPES:
                        files.append({'label':child.name})
            return {'label':selected.name if selected!=root else 'Home','files':files,'dirs':directories,
                    'groups':[],'path':['Home',*selected.relative_to(root).parts]}

    class CasePipeline(pv.ParaViewWebProxyManager):
        def getAbsolutePath(self, relativePath):
            try:
                item = case_path(root,relativePath.removeprefix('Home/') if isinstance(relativePath,str) else relativePath)
            except (OSError,ValueError):
                return None
            return str(item) if item.is_file() and item.suffix.casefold() in INPUT_TYPES else None

        @rpc('pv.proxy.manager.create')
        def create(self, functionName, parentId, initialValues=None, skipDomain=False, subProxyValues=None):
            initialValues, subProxyValues = initialValues or {}, subProxyValues or {}
            if (not all(safe_property(key) for key in initialValues)
                    or any(not safe_property(key) or not all(safe_property(item) for item in values)
                           for key,values in subProxyValues.items())):
                return {'success':False,'reason':'Choose properties declared for this operator'}
            return super().create(functionName,parentId,initialValues,skipDomain,subProxyValues)

        @rpc('pv.proxy.manager.update')
        def update(self, changes):
            if not isinstance(changes,list) or not all(isinstance(item,dict) and safe_property(item.get('name')) for item in changes):
                return {'success':False,'errorList':['Use the case file picker for filesystem inputs']}
            if any(item['name'].replace(' ', '') in RAYTRACE_PROPERTIES for item in changes):
                load_material_textures()
            return super().update(changes)

    class CaseSave(pv.ParaViewWebSaveData):
        @rpc('pv.data.save')
        def saveData(self, filePath, options=None):
            output = root/'D-post'/'visualizer'/arguments.session_nonce[:32]
            try:
                if (not output.resolve().is_relative_to(root) or Path(filePath).name!=filePath
                        or (target := case_path(output.resolve(),filePath)).suffix.casefold() not in OUTPUT_TYPES
                        or target.exists()):
                    raise ValueError('Invalid visualization export name')
                if options is not None and 'size' in options:
                    size = options['size']
                    if len(size)!=2 or any(not 1<=int(x)<=2048 for x in size):
                        raise ValueError('Visualization image dimensions exceed the limit')
                output.mkdir(parents=True,exist_ok=True)
            except (OSError,TypeError,ValueError):
                return {'success':False,'message':'Choose a new output filename in the session folder'}
            self.baseSavePath = str(output)
            return super().saveData(filePath,options)

    class CaseImageDelivery(pv.ParaViewWebPublishImageDelivery):
        @rpc('viewport.image.push')
        def imagePush(self, options):
            view = self.getView(options['view'])
            view_id = view.GetGlobalIDAsString()
            observer = self.trackingViews.get(view_id)
            if observer is None or not observer['enabled']:
                return super().imagePush(options)
            # A requested refresh needs an image even if the encoder has only
            # the previously published frame ready at this instant.
            observer['mtime'] = 0
            self.pushRender(view_id, ignoreAnimation=True)

    class Protocol(PVServerProtocol):
        def initialize(self):
            self.registerVtkWebProtocol(CaseFiles(str(root),'Home'))
            self.registerVtkWebProtocol(CasePipeline(options,str(root),source.relative_to(root).as_posix(),
                                                    allowUnconfiguredReaders=True))
            self.registerVtkWebProtocol(pv.ParaViewWebColorManager())
            self.registerVtkWebProtocol(pv.ParaViewWebMouseHandler())
            self.registerVtkWebProtocol(pv.ParaViewWebViewPort(1.0,2048,1440))
            self.registerVtkWebProtocol(CaseImageDelivery(decode=False))
            self.registerVtkWebProtocol(pv.ParaViewWebLocalRendering())
            self.registerVtkWebProtocol(pv.ParaViewWebTimeHandler())
            self.registerVtkWebProtocol(pv.ParaViewWebSelectionHandler())
            self.registerVtkWebProtocol(pv.ParaViewWebWidgetManager())
            self.registerVtkWebProtocol(pv.ParaViewWebKeyValuePairStore())
            self.registerVtkWebProtocol(CaseSave())
            self.updateSecret(arguments.authKey)
            self.getApplication().SetImageEncoding(0)
            view = simple.GetRenderView()
            if getattr(view, 'EnableRayTracing', False):
                load_material_textures()
            view.EnableRenderOnInteraction = 0
            simple.Render(view)

        def port_callback(self, port):
            if port != arguments.port:
                raise RuntimeError('Native visualization bound an unexpected port')
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            from process_identity_native import observe

            ready = {'schema':'vibeflow.viz-ready.v1','nonce':arguments.session_nonce,
                     'profile_sha256':arguments.profile_sha256,'port':port,'process':observe(os.getpid())}
            print('\nVIBEFLOW_VIZ_READY_V1 '+json.dumps(ready,sort_keys=True),flush=True)

    server.start_webserver(options=arguments,protocol=Protocol)


if __name__ == '__main__':
    start()
