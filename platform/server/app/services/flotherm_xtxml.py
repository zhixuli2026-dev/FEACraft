"""XTXML/XTXMLA assembly instances for paired PDML geometry provenance."""

import math
import xml.etree.ElementTree as xml

from .flotherm_ecxml import child, tag, text


def _multiply(a, b):
    return [sum(x*y for x, y in zip(row, b)) for row in a]


def _transformation(element):
    def attribute(item, key, default):
        value = float(item.get(key, default)) if item is not None else default
        if not math.isfinite(value):
            raise ValueError(f'XTXML 变换的 {key} 不是有限数值')
        return value

    position = child(element, 'position')
    rotation = child(element, 'rotation')
    p = [attribute(position, axis, 0.) for axis in ('x', 'y', 'z')]
    r = [[attribute(rotation, f'r{i}{j}', float(i == j)) for j in range(3)] for i in range(3)]
    for i in range(3):
        observed = _multiply(r, r[i])
        if any(not math.isclose(value, float(i == j), abs_tol=1e-7) for j, value in enumerate(observed)):
            raise ValueError('XTXML rotation 必须是正交刚体变换')
    cross = [r[0][1]*r[1][2]-r[0][2]*r[1][1], r[0][2]*r[1][0]-r[0][0]*r[1][2],
             r[0][0]*r[1][1]-r[0][1]*r[1][0]]
    if not math.isclose(_multiply([r[2]], cross)[0], 1., abs_tol=1e-7):
        raise ValueError('XTXML rotation 不支持镜像变换')
    return p, r


def assemblies(data, solids):
    try:
        root = xml.fromstring(data)
    except (xml.ParseError, LookupError) as error:
        raise ValueError('XTXML 装配文件不是有效 XML') from error
    if tag(root) != 'xtxml_assembly':
        raise ValueError('XTXML 根元素必须为 xtxml_assembly')
    definitions = {}
    documents = child(root, 'documents')
    if documents is not None:
        for item in documents:
            name = item.get('name') or text(item, 'name')
            if not name or name in definitions:
                raise ValueError('XTXML 文档名称为空或重复')
            definitions[name] = item

    root_assembly = child(root, 'root_assembly')
    if root_assembly is None:
        raise ValueError('XTXML 缺少 root_assembly')
    geometry_names = {}
    for obj in solids:
        geometry_names.setdefault(obj['name'], []).append(obj['id'])
    observed_paths = set()
    instances = []

    def visit(container, trail, origin, basis, chain):
        if len(trail) > 128:
            raise ValueError('XTXML 装配引用嵌套超过 128 层')
        for item in container:
            kind = tag(item)
            if kind in ('assembly', 'child_components'):
                nested = trail
                if kind == 'assembly':
                    nested = (*trail, item.get('name') or text(item, 'name') or 'assembly')
                visit(item, nested, origin, basis, chain)
                continue
            if kind != 'component':
                continue
            name = item.get('name') or text(item, 'name')
            document = item.get('document') or text(item, 'document')
            path = (*trail, name)
            if not name or path in observed_paths:
                raise ValueError('XTXML 装配实例路径为空或不唯一')
            if len(instances) >= 100000:
                raise ValueError('XTXML 装配展开超过 100,000 个实例')
            observed_paths.add(path)
            translation, rotation = _transformation(item)
            world = [a+b for a, b in zip(origin, _multiply(basis, translation))]
            placed = [list(row) for row in zip(*(_multiply(basis, column) for column in zip(*rotation)))]
            if not all(math.isfinite(value) for value in world):
                raise ValueError('XTXML 累积装配坐标超出范围')
            target = definitions.get(document)
            if target is None:
                raise ValueError(f'XTXML 实例 {name} 的文档引用无法解析：{document}')
            match = geometry_names.get(name, ())
            instances.append({'path': list(path), 'document': document, 'translation': world, 'rotation': placed,
                              'name_match': match[0] if len(match) == 1 else None})
            if tag(target) == 'assembly':
                if document in chain:
                    raise ValueError(f'XTXML 装配引用成环：{document}')
                visit(target, path, world, placed, (*chain, document))
            visit(item, path, world, placed, chain)

    visit(root_assembly, (), [0., 0., 0.], [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]], ())
    if not instances:
        raise ValueError('XTXML 未发现可核验的装配实例')
    return {'documents': sorted(definitions), 'instances': instances, 'geometry_source': 'PDML'}
