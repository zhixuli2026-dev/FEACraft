"""Read OpenFOAM scalar result fields and match their boundary face counts."""
from dataclasses import dataclass
import math
import re

from .flotherm_heat_balance import NUMBER, _items


_SCALAR = NUMBER.encode()
_WORD = rb'[A-Za-z0-9_.:-]+'
_VECTOR = rb'\(\s*('+_SCALAR+rb')\s+('+_SCALAR+rb')\s+('+_SCALAR+rb')\s*\)'


@dataclass(frozen=True)
class Patch:
    kind: str
    first: int
    count: int


def mesh_patches(source, *, internal_faces, total_faces):
    rows = _items(source.text(), rf'({_WORD.decode()})\s*\{{([^{{}}]*)\}}', 'boundary')
    patches = {}
    position = internal_faces
    for name, body in rows:
        kind = re.findall(r'\btype\s+(\S+)\s*;', body)
        start = re.findall(r'\bstartFace\s+(\d+)\s*;', body)
        counts = re.findall(r'\bnFaces\s+(\d+)\s*;', body)
        if (name in patches or len(kind) != 1 or len(start) != 1
                or len(counts) != 1 or int(start[0]) != position):
            raise ValueError(f'OpenFOAM 边界面编号或类型无效：{name}')
        count = int(counts[0])
        patches[name] = Patch(kind[0], position, count)
        position += count
    if not patches or position != total_faces:
        raise ValueError('OpenFOAM 边界面与网格面总数不一致')
    return patches


@dataclass(frozen=True)
class FieldValues:
    uniform: float | tuple[float, float, float] | None
    entries: tuple
    declared_count: int | None = None

    def verify_count(self, count):
        described = len(self.entries) if self.uniform is None else self.declared_count
        if described is not None and described != count:
            raise ValueError('OpenFOAM 边界场数量与网格面数不一致')

    def faces(self, count):
        self.verify_count(count)
        return self.entries if self.uniform is None else (self.uniform,)*count


def _require(source, pattern, cursor, message):
    match = source.match(pattern, cursor)
    if match is None:
        raise ValueError(f'{message}：{source.path}')
    return match


def _entries(source, cursor):
    # Native writers can use compact repeated values for an ASCII list.
    repeated = source.match(rb'([0-9]{1,9})\s*\{\s*('+_SCALAR+rb')\s*\}', cursor)
    if repeated:
        scalar = float(repeated[2])
        if not math.isfinite(scalar):
            raise ValueError(f'OpenFOAM 标量不是有限值：{source.path}')
        return FieldValues(scalar, (), int(repeated[1])), repeated.end()
    if source.binary:
        values, end = source.raw_list(source.scalar, cursor)
        entries = tuple(value for (value,) in values)
    else:
        standard = _require(source, rb'([0-9]{1,9})\s*\(([^(){}]*)\)', cursor, 'OpenFOAM 标量列表无效')
        entries = tuple(map(float, standard[2].split()))
        if len(entries) != int(standard[1]):
            raise ValueError(f'OpenFOAM 标量列表数量不符：{source.path}')
        end = standard.end()
    if not all(map(math.isfinite, entries)):
        raise ValueError(f'OpenFOAM 标量列表包含非有限值：{source.path}')
    return FieldValues(None, entries), end


def _value(source, cursor):
    one = source.match(rb'uniform\s+('+_SCALAR+rb')\s*;', cursor)
    if one:
        value = float(one[1])
        if not math.isfinite(value):
            raise ValueError(f'OpenFOAM 标量不是有限值：{source.path}')
        return FieldValues(value, ()), one.end()
    marker = _require(source, rb'nonuniform\s+List<scalar>', cursor, 'OpenFOAM 标量场格式无效')
    values, end = _entries(source, marker.end())
    after = _require(source, rb';', end, 'OpenFOAM 标量列表缺少终止符')
    return values, after.end()


def scalar_field(source, *, class_name, object_name, patches, internal_count=None):
    if source.class_name != class_name or source.object_name != object_name:
        raise ValueError(f'OpenFOAM 场对象类型或名称不符：{source.path}')
    dims = _require(source, rb'dimensions\s+\[\s*((?:[+-]?\d+\s+){6}[+-]?\d+)\s*\]\s*;',
                    None, 'OpenFOAM 场量纲缺失')
    cursor = dims.end()
    oriented = source.match(rb'oriented\s+(oriented|unoriented|true|false|1|0)\s*;', cursor)
    if oriented:
        if class_name == b'surfaceScalarField' and oriented[1] in (b'unoriented', b'false', b'0'):
            raise ValueError(f'通量场需要带方向的面值：{source.path}')
        cursor = oriented.end()
    marker = _require(source, rb'internalField\s+', cursor, 'OpenFOAM 内部场缺失')
    internal, cursor = _value(source, marker.end())
    if internal_count is not None:
        internal.verify_count(internal_count)
    marker = _require(source, rb'boundaryField\s*\{', cursor, 'OpenFOAM 边界场缺失')
    cursor = marker.end()
    values = {}
    while not source.match(rb'\}', cursor):
        marker = _require(source, rb'('+_WORD+rb')\s*\{', cursor, 'OpenFOAM 边界域名称无效')
        name = marker[1].decode('ascii')
        if name in values or name not in patches:
            raise ValueError(f'OpenFOAM 边界场名称重复或不属于网格：{name}')
        patch = patches[name]
        kind = _require(source, rb'type\s+('+_WORD+rb')\s*;', marker.end(), 'OpenFOAM 边界场类型缺失')
        entry = source.match(rb'value\s+', kind.end())
        if entry:
            parsed, cursor = _value(source, entry.end())
            values[name] = parsed.faces(patch.count)
        elif (patch.kind == 'empty' and kind[1] == b'empty') or patch.count == 0:
            values[name], cursor = (0.0,)*patch.count, kind.end()
        else:
            raise ValueError(f'OpenFOAM 边界缺少求解后的面值：{name}')
        end = _require(source, rb'\}', cursor, 'OpenFOAM 边界场包含未知或未闭合内容')
        cursor = end.end()
    if values.keys() != patches.keys() or source.text(source.match(rb'\}', cursor).end()):
        raise ValueError(f'OpenFOAM 边界场覆盖不完整或包含额外内容：{source.path}')
    return tuple(map(int,dims[1].split())), values


def _vector_value(source, cursor):
    uniform = source.match(rb'uniform\s+'+_VECTOR, cursor)
    if uniform:
        vectors = FieldValues(tuple(map(float, uniform.groups())), ())
        end = uniform.end()
    else:
        marker = _require(source, rb'nonuniform\s+List<vector>', cursor, 'OpenFOAM 向量场格式无效')
        repeated = source.match(rb'([0-9]{1,9})\s*\{\s*'+_VECTOR+rb'\s*\}', marker.end())
        if repeated:
            vectors = FieldValues(tuple(map(float, repeated.groups()[1:])), (), int(repeated[1]))
            end = repeated.end()
        elif source.binary:
            values, end = source.raw_list(source.scalar*3, marker.end())
            vectors = FieldValues(None, tuple(values))
        else:
            count = _require(source, rb'([0-9]{1,9})\s*\(', marker.end(), 'OpenFOAM 向量场缺少数量')
            end, values = count.end(), []
            for _ in range(int(count[1])):
                entry = _require(source, _VECTOR, end, 'OpenFOAM 向量场列表无效')
                values.append(tuple(map(float, entry.groups())))
                end = entry.end()
            end = _require(source, rb'\)', end, 'OpenFOAM 向量场列表长度不符').end()
            vectors = FieldValues(None, tuple(values))
    return vectors, _require(source, rb';', end, 'OpenFOAM 向量场缺少终止符').end()


def port_velocities(source, patches, names):
    if source.class_name!=b'volVectorField' or source.object_name!=b'U':
        raise ValueError(f'流道动能对账需要 OpenFOAM 速度场 U：{source.path}')
    dimensions = _require(source, rb'dimensions\s+\[\s*0\s+1\s+-1\s+0\s+0\s+0\s+0\s*\]\s*;',
                          None, 'OpenFOAM 速度场量纲无效')
    marker = _require(source, rb'internalField\s+', dimensions.end(), 'OpenFOAM 速度场缺少内部场')
    _, end = _vector_value(source, marker.end())
    boundary = _require(source, rb'boundaryField\s*\{', end, 'OpenFOAM 速度场缺少边界场')
    results = {}
    for name in names:
        pattern = re.compile(rb'(?m)^\s*'+re.escape(name.encode('ascii'))+rb'\s*\{')
        entries = list(pattern.finditer(source.data, boundary.end()))
        if len(entries)!=1:
            raise ValueError(f'OpenFOAM 速度场缺少唯一的边界 {name}')
        kind = _require(source, rb'type\s+'+_WORD+rb'\s*;', entries[0].end(), 'OpenFOAM 速度边界缺少类型')
        value = _require(source, rb'value\s+', kind.end(), 'OpenFOAM 速度边界缺少面值')
        velocities, end = _vector_value(source, value.end())
        _require(source, rb'\}', end, 'OpenFOAM 速度边界没有闭合')
        results[name] = velocities.faces(patches[name].count)
        if not all(math.isfinite(value) for vector in results[name] for value in vector):
            raise ValueError(f'OpenFOAM 速度边界含非有限值：{name}')
    return results
