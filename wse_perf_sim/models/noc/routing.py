"""WSE40 routing/bitmap primitives; independent of core captures."""
from collections import deque
from wse_perf_sim.errors import ModelError


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ModelError(f'Invalid {name}: {value!r}')
    return value


def node_id(value):
    if integer(value, 'node') >= 40:
        raise ModelError('WSE40 node must be in 0..39')
    return value


def neighbors(node):
    row, col = divmod(node, 8)
    return tuple(n for n in (node - 8 if row else -1, node + 8 if row < 4 else -1,
                            node - 1 if col else -1, node + 1 if col < 7 else -1) if n >= 0)


def route_bitmap(passing, landing):
    passing, landing = set(passing), set(landing)
    if not landing <= passing:
        raise ModelError('Landing nodes must be passing nodes')
    return sum((3 if n in landing else 2) << (2 * node_id(n)) for n in passing)


def route_tree(source, bits):
    node_id(source)
    if integer(bits, 'route_bits') >= 1 << 80:
        raise ModelError('Route bitmap exceeds 80 bits')
    pairs = [(bits >> (2 * n)) & 3 for n in range(40)]
    active = {n for n, p in enumerate(pairs) if p & 2}
    landing = {n for n, p in enumerate(pairs) if p & 1}
    if 1 in pairs or source not in active or not landing:
        raise ModelError('Illegal route: 01, absent source, or no landing node')
    children, distance, seen = {}, {source: 0}, {source}
    queue = deque([(source, None)])
    while queue:
        node, parent = queue.popleft()
        children[node] = tuple(n for n in neighbors(node) if n in active and n != parent)
        for child in children[node]:
            if child in seen:
                raise ModelError('Route induced subgraph contains a cycle')
            seen.add(child)
            distance[child] = distance[node] + 1
            queue.append((child, node))
    if seen != active:
        raise ModelError('Disconnected route')
    return children, landing, distance


def multicast_routes(source, destinations):
    """Cover destinations once, merging shortest paths into induced trees."""
    node_id(source)
    if not destinations or len(set(destinations)) != len(destinations):
        raise ModelError('Provide unique nonempty destinations')
    for destination in destinations:
        node_id(destination)
    parents, queue = {source: None}, deque([source])
    while queue:
        node = queue.popleft()
        for peer in sorted(neighbors(node)):
            if peer not in parents:
                parents[peer] = node
                queue.append(peer)
    trees = []
    for destination in sorted(destinations):
        path, node = set(), destination
        while node is not None:
            path.add(node)
            node = parents[node]
        for passing, landing in trees:
            union = passing | path
            edges = sum(peer in union for node in union for peer in neighbors(node)) // 2
            # The union of source-rooted paths is connected; n-1 edges proves a tree.
            if edges == len(union) - 1:
                passing.update(path)
                landing.add(destination)
                break
        else:
            trees.append((path, {destination}))
    return [route_bitmap(passing, landing) for passing, landing in trees]


def transmissions(entry, parameters):
    """Normalize explicit single-window and automatically planned multi-tree sends."""
    if 'routes' in entry:
        return entry['routes']
    start = entry['window'] * parameters['slot_cycles']
    return [dict(route_bits=entry['route_bits'], start_cycle=start,
                 end_cycle=start + parameters['slot_cycles'])]
