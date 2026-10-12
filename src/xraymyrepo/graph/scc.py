"""Strongly connected components (Tarjan), iterative and deterministic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def strongly_connected(
    nodes: Sequence[str], adjacency: Mapping[str, Sequence[str]]
) -> list[list[str]]:
    """Every strongly connected component, each sorted; deterministic for sorted inputs.

    Iterative, so deep graphs cannot exhaust the recursion limit.
    """
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index_of:
            continue
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            successors = adjacency.get(node, [])
            if i < len(successors):
                work.append((node, i + 1))
                successor = successors[i]
                if successor not in index_of:
                    work.append((successor, 0))
                elif successor in on_stack:
                    low[node] = min(low[node], index_of[successor])
                continue
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return components
