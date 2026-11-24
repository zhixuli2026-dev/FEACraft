"""Read source and filter definitions from the installed ParaView runtime."""

import json
from pathlib import Path
import sys

from paraview import servermanager, simple
from plugin_support import PLUGIN_PROXIES, load_plugin, plugin_definitions, registered_plugins


EDITABLE_ATTRIBUTES = ('name','label','panel_visibility','panel_widget','default_values','default_mode',
                       'number_of_elements','number_of_components','repeat_command','repeatable',
                       'number_of_elements_per_command','information_only','animateable','is_internal',
                       'optional','min','max','value','text','type','group','proxygroup','proxyname',
                       'attribute_type','input_domain_name','function','composite_data_supported')


def attributes(element):
    return {key:value for key in EDITABLE_ATTRIBUTES if (value := element.GetAttribute(key)) is not None}


def xml_record(element):
    result = {'type': element.GetName(), 'attributes': attributes(element)}
    children = [xml_record(element.GetNestedElement(index)) for index in range(element.GetNumberOfNestedElements())]
    if children:
        result['children'] = children
    return result


def resolved_definition(manager, group, name):
    seen = set()
    while (group,name) not in seen:
        seen.add((group,name))
        definition = manager.GetCollapsedProxyDefinition(group,name,None)
        if definition is None or not definition.GetAttribute('group') or definition.GetAttribute('class'):
            return definition
        group, name = definition.GetAttribute('group'), definition.GetAttribute('name')
    return None


def proxy_choices(manager, definitions, element, branch):
    if len(branch) >= 4:
        return []
    targets = []
    for index in range(element.GetNumberOfNestedElements()):
        child = element.GetNestedElement(index)
        if child.GetName() == 'Proxy':
            targets.append((child.GetAttribute('group'), child.GetAttribute('name')))
        elif child.GetName() == 'Group':
            group = child.GetAttribute('name')
            iterator = manager.NewDefinitionIterator(group)
            for _ in iterator:
                targets.append((group, iterator.GetProxyName()))
    choices = []
    for key in dict.fromkeys(targets):
        if key in branch or None in key:
            continue
        group, name = key
        prototype = manager.GetPrototypeProxy(group, name)
        definition = resolved_definition(definitions, group, name)
        if prototype and definition:
            label = prototype.GetXMLLabel() or name
            choices.append({'label':label, 'type':label,
                            'properties':properties(manager, definitions, prototype, definition, (*branch,key))})
    return choices


def properties(manager, definitions, prototype, definition, branch):
    rows = []
    for index in range(definition.GetNumberOfNestedElements()):
        item = definition.GetNestedElement(index)
        name = item.GetAttribute('name')
        kind = item.GetName()
        if not name or not kind.endswith('Property'):
            continue
        domains = [item.GetNestedElement(index) for index in range(item.GetNumberOfNestedElements())]
        row = {'name':name, 'type':kind, 'attributes':attributes(item),
               'domains':[xml_record(domain) for domain in domains if domain.GetName().endswith('Domain')]}
        if kind == 'InputProperty' and (port := prototype.GetProperty(name)):
            row['multiple'] = bool(port.GetRepeatable())
        if kind == 'ProxyProperty':
            for domain in domains:
                if domain.GetName() == 'ProxyListDomain':
                    row['proxy_choices'] = proxy_choices(manager, definitions, domain, branch)
                    break
        rows.append(row)
    return rows


def proxy_record(proxy_manager, definitions, group, name):
    prototype = proxy_manager.GetPrototypeProxy(group,name)
    definition = resolved_definition(definitions,group,name)
    if not prototype or not definition:
        return None
    docs = prototype.GetDocumentation()
    return {'group':group,'name':name,'label':prototype.GetXMLLabel() or name,
            'description':docs.GetDescription() if docs is not None else '',
            'properties':properties(proxy_manager,definitions,prototype,definition,((group,name),))}


def catalogue():
    proxy_manager = servermanager.ProxyManager()
    definitions = servermanager.ActiveConnection.Session.GetProxyDefinitionManager()
    entries = {}
    for group in ('sources', 'filters'):
        iterator = proxy_manager.NewDefinitionIterator(group)
        entries[group] = {}
        for _ in iterator:
            name = iterator.GetProxyName()
            record = proxy_record(proxy_manager,definitions,group,name)
            if record:
                entries[group][name] = record
    return {'schema':'vibeflow.paraview.catalog.v1','version':str(simple.GetParaViewVersion()),
            'groups':entries,'plugins':registered_plugins()}


def plugin_detail(name):
    before = {group:plugin_definitions(group) for group in PLUGIN_PROXIES}
    plugin = load_plugin(name)
    plugin_manager = servermanager.ProxyManager()
    definitions = servermanager.ActiveConnection.Session.GetProxyDefinitionManager()
    proxies = []
    for group in PLUGIN_PROXIES:
        for proxy in sorted(plugin_definitions(group)-before[group]):
            record = proxy_record(plugin_manager,definitions,group,proxy)
            if record:
                proxies.append(record)
    return {'schema':'vibeflow.paraview.plugin.v1','plugin':plugin,'proxies':proxies}


def input_matches(path):
    reader = simple.OpenDataFile(str(Path(path).resolve()))
    if reader is None:
        raise ValueError('ParaView cannot read this input')
    reader.UpdatePipeline()
    manager = servermanager.ProxyManager()
    definitions = manager.NewDefinitionIterator('filters')
    matches = {}
    for _ in definitions:
        name = definitions.GetProxyName()
        proxy = manager.NewProxy('filters', name)
        if proxy is None:
            continue
        iterator = proxy.NewPropertyIterator()
        iterator.Begin()
        ports = []
        while not iterator.IsAtEnd():
            prop = iterator.GetProperty()
            if prop.IsA('vtkSMInputProperty') and not prop.GetIsInternal() and not prop.GetInformationOnly():
                prop.AddUncheckedInputConnection(reader.SMProxy, 0)
                if prop.IsInDomains():
                    ports.append(iterator.GetKey())
                prop.RemoveAllUncheckedProxies()
            iterator.Next()
        if ports:
            matches[name] = sorted(ports)
    return {'dataset':reader.GetDataInformation().GetDataSetTypeAsString(), 'filters':matches}


if __name__=='__main__':
    if len(sys.argv)>3 and sys.argv[2]=='--input':
        value = input_matches(sys.argv[3])
    else:
        value = plugin_detail(sys.argv[2]) if len(sys.argv)>2 else catalogue()
    Path(sys.argv[1]).write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding='utf-8')
