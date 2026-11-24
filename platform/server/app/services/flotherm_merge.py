"""Combine connected Cartesian solids while retaining object-local heat sources."""

import math

from .flotherm_geometry import IDENTITY, cartesian_parts, make_box_union


def _connected(left, right):
    overlap = [(min(b,d)-max(a,c),(max(b,d)-min(a,c))*1e-12) for a,b,c,d in zip(*left,*right)]
    return all(length>=-tolerance for length,tolerance in overlap) and sum(
        length>tolerance for length,tolerance in overlap)>=2


def merge_boxes(solids):
    eligible = {}
    for name,row in solids.items():
        if row['shape']['kind']=='box' and not {'pcb','thin_plate','two_resistance'} & row.keys():
            eligible.setdefault(row['material'],[]).append(name)
    replacement, consumed = {}, set()
    for siblings in eligible.values():
        pending = dict.fromkeys(siblings)
        while pending:
            head = next(iter(pending))
            del pending[head]
            group = [head]
            for name in group:
                for other in tuple(pending):
                    if _connected(solids[name]['bounds_m'],solids[other]['bounds_m']):
                        group.append(other)
                        del pending[other]
            if len(group)==1:
                continue
            order = {name:index for index,name in enumerate(siblings)}
            group.sort(key=order.get)
            parts = [solids[name] for name in group]
            shape = make_box_union((0,0,0),IDENTITY,[row['bounds_m'] for row in parts])
            low = shape['origin']
            high = [a+b for a,b in zip(low,shape['dimensions'])]
            members = [
                {'id':row['id'],'bounds_m':row['bounds_m'],'power_w':row['power_w'],'cell_zone':f'objectCells{index}'}
                for index,row in enumerate(parts)
            ]
            replacement[head] = {**solids[head],'shape':shape,'bounds_m':[low,high],
                                 'mesh_boxes':cartesian_parts(shape),
                                 'power_w':math.fsum(row['power_w'] for row in parts),
                                 'merged_boxes':{'members':members,'volume_m3':shape['volume_m3']}}
            consumed.update(group[1:])
    return {name:replacement.get(name,row) for name,row in solids.items() if name not in consumed}


def merged_zones(members):
    actions = []
    for row in members:
        low,high = ('('+' '.join(map(str,point))+')' for point in row['bounds_m'])
        zone = row['cell_zone']
        actions.extend((
            f'{{ name {zone}Set; type cellSet; action new; source boxToCell; box {low} {high}; }}',
            f'{{ name {zone}; type cellZoneSet; action new; source setToCellZone; set {zone}Set; }}',
        ))
    return 'actions (\n'+'\n'.join(actions)+'\n);'


def merged_heat_sources(members):
    return [
        f'heat_{row["cell_zone"]} {{ type scalarSemiImplicitSource; '
        f'selectionMode cellZone; cellZone {row["cell_zone"]}; volumeMode absolute; '
        f'injectionRateSuSp {{ h ({row["power_w"]} 0); }} }}'
        for row in members if row['power_w']
    ]
