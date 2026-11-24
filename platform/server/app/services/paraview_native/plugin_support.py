"""Read and load the plugins registered by the installed ParaView build."""

from pathlib import Path

from paraview import servermanager as sm, simple


def registered_plugins():
    manager = sm.vtkSMProxyManager.GetProxyManager().GetPluginManager()
    info = manager.GetLocalInformation()
    return {info.GetPluginName(i): {
        'name': info.GetPluginName(i), 'loaded': bool(info.GetPluginLoaded(i)),
        'autoload': bool(info.GetAutoLoad(i)), 'version': info.GetPluginVersion(i) or None,
        'requires': (info.GetRequiredPlugins(i) or '').split(';') if info.GetRequiredPlugins(i) else [],
        'library': Path(info.GetPluginFileName(i)).name if info.GetPluginFileName(i) else None,
    } for i in range(info.GetNumberOfPlugins())}


def load_plugin(name):
    original = registered_plugins().get(name)
    if original is None:
        raise ValueError(f'ParaView runtime does not register plugin {name}')
    if not original['loaded']:
        simple.LoadDistributedPlugin(name,remote=False)
    item = registered_plugins().get(name)
    if item is None or not item['loaded']:
        raise ValueError(f'ParaView could not activate plugin {name}')
    return item


PLUGIN_PROXIES = ('sources','filters','views','representations','writers','readers','extract_writers')


def plugin_definitions(group):
    pm = sm.ProxyManager()
    iterator = pm.NewDefinitionIterator(group)
    return {iterator.GetProxyName() for _ in iterator}
