"""Find unique coincident nodes between two native surface meshes."""

from itertools import product
import math


def matched_nodes(positions, coordinates, tolerance):
    def cell(point):
        try:
            return tuple(math.floor(component/tolerance) for component in point)
        except OverflowError as error:
            raise ValueError('Conformal match tolerance is below coordinate resolution') from error

    buckets = {}
    for node, coordinate in coordinates.items():
        buckets.setdefault(cell(coordinate),[]).append(node)
    offsets = list(product((-1,0,1),repeat=3))
    found = set()
    matched = []
    for point in positions:
        origin = cell(point)
        candidates = [node for offset in offsets
                      for node in buckets.get(tuple(a+b for a,b in zip(origin,offset)),())
                      if math.dist(coordinates[node],point)<=tolerance]
        if len(candidates)!=1 or candidates[0] in found:
            raise ValueError('Conformal point has no unique matching CalculiX node')
        found.add(candidates[0])
        matched.append(candidates[0])
    return matched
