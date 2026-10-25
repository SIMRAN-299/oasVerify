"""Topological dependency graph (Kahn's algorithm) for ordering OpenAPI operations."""

from collections import defaultdict, deque
import copy

from manager.core.schemas import HTTPMethod, OperationNode, ParameterLocation


class DependencyGraph:
    """Computes producer-consumer operation dependencies and topologically sorts endpoints."""

    def __init__(self) -> None:
        self.nodes: dict[str, OperationNode] = {}
        self.adjacency_list: dict[str, set[str]] = defaultdict(set)
        self.in_degree: dict[str, int] = defaultdict(int)

    def add_operation(self, op: OperationNode) -> None:
        """Registers an OperationNode in the graph."""
        self.nodes[op.operation_id] = op
        if op.operation_id not in self.in_degree:
            self.in_degree[op.operation_id] = 0

    def add_dependency(self, producer_id: str, consumer_id: str) -> None:
        """Adds a directed edge from producer_id to consumer_id."""
        if consumer_id not in self.adjacency_list[producer_id]:
            self.adjacency_list[producer_id].add(consumer_id)
            self.in_degree[consumer_id] += 1

    def build_heuristic_edges(self) -> None:
        """Infers edges between POST/GET producers and downstream consumers sharing path keys."""
        for prod_id, prod_op in self.nodes.items():
            for cons_id, cons_op in self.nodes.items():
                if prod_id == cons_id:
                    continue

                consumed_path_params = {
                    p.name.lower().replace("_", "").replace("-", "")
                    for p in cons_op.parameters
                    if p.location == ParameterLocation.PATH
                }

                produced_keys = {
                    k.lower().replace("_", "").replace("-", "")
                    for k in prod_op.produces_keys
                }

                if consumed_path_params.intersection(produced_keys):
                    prod_is_collection_get = (
                        prod_op.method == HTTPMethod.GET
                        and not prod_op.path.rstrip("/").endswith("}")
                    )
                    if (
                        prod_op.method == HTTPMethod.POST
                        and cons_op.method != HTTPMethod.POST
                    ):
                        self.add_dependency(prod_id, cons_id)
                    elif prod_is_collection_get and (
                        cons_op.method != HTTPMethod.POST
                        or "{" in cons_op.path
                    ):
                        self.add_dependency(prod_id, cons_id)
                    elif (
                        prod_op.method == HTTPMethod.GET
                        and cons_op.method == HTTPMethod.DELETE
                    ):
                        self.add_dependency(prod_id, cons_id)

    def topological_sort(self) -> list[str]:
        """Returns operation_ids ordered topologically with CRUD method priority tie-breaking."""
        in_deg = copy.deepcopy(self.in_degree)
        queue = deque(
            sorted(
                [node_id for node_id, deg in in_deg.items() if deg == 0],
                key=lambda x: self._node_priority(self.nodes[x]),
            )
        )
        sorted_nodes: list[str] = []

        while queue:
            curr = queue.popleft()
            sorted_nodes.append(curr)

            ready_neighbors: list[str] = []
            for neighbor in self.adjacency_list[curr]:
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    ready_neighbors.append(neighbor)
            for neighbor in sorted(
                ready_neighbors, key=lambda x: self._node_priority(self.nodes[x])
            ):
                queue.append(neighbor)

        if len(sorted_nodes) != len(self.nodes):
            remaining = set(self.nodes.keys()) - set(sorted_nodes)
            sorted_nodes.extend(
                sorted(
                    list(remaining),
                    key=lambda x: self._node_priority(self.nodes[x]),
                )
            )

        return sorted_nodes

    @staticmethod
    def _method_priority(method: HTTPMethod) -> int:
        priority_map = {
            HTTPMethod.POST: 0,
            HTTPMethod.GET: 1,
            HTTPMethod.PUT: 2,
            HTTPMethod.PATCH: 3,
            HTTPMethod.DELETE: 4,
        }
        return priority_map.get(method, 99)

    @classmethod
    def _node_priority(cls, node: OperationNode) -> tuple[int, str]:
        path = node.path.rstrip("/").lower()
        has_param = "{" in path
        ends_with_param = path.endswith("}")
        last_seg = path.split("/")[-1] if path else ""
        is_action_post = ends_with_param or (
            has_param
            and (
                last_seg
                in {
                    "cancel",
                    "restock",
                    "apply-promotion",
                    "apply_promotion",
                    "activate",
                    "deactivate",
                    "archive",
                    "restore",
                    "verify",
                    "reset",
                }
                or last_seg.startswith("apply-")
            )
        )
        if node.method == HTTPMethod.GET and not has_param:
            rank = 0
        elif node.method == HTTPMethod.POST and not has_param:
            rank = 1
        elif node.method == HTTPMethod.GET and has_param and not ends_with_param:
            rank = 2
        elif node.method == HTTPMethod.POST and has_param and not is_action_post:
            rank = 3
        elif node.method == HTTPMethod.GET and ends_with_param:
            rank = 4
        elif node.method == HTTPMethod.PUT:
            rank = 5
        elif node.method == HTTPMethod.PATCH:
            rank = 6
        elif node.method == HTTPMethod.POST and not path.endswith("/cancel"):
            rank = 7
        elif node.method == HTTPMethod.POST and path.endswith("/cancel"):
            rank = 8
        elif node.method == HTTPMethod.DELETE:
            rank = 9
        else:
            rank = cls._method_priority(node.method)
        return (rank, node.operation_id)
