"""預算科目流用的計算核心；與輸入檔案和使用介面無關。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice
import math
import time

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix


class InputError(ValueError):
    """輸入檔案或資料不符合規格。"""


class SolveError(RuntimeError):
    """求解器未能證明完整最佳解。"""


class SearchStopped(RuntimeError):
    """本組的搜尋時間或規模已達上限。"""


@dataclass(frozen=True)
class BudgetRow:
    line: int
    code: str
    name: str
    before: int
    after: int
    delta: int

    @property
    def group(self) -> str:
        return self.code.rsplit("-", 1)[0]


@dataclass(frozen=True)
class Transfer:
    source: BudgetRow
    target: BudgetRow
    amount: int


@dataclass(frozen=True)
class GroupResult:
    group: str
    rows: tuple[BudgetRow, ...]
    transfers: tuple[Transfer, ...]
    split_count: int
    edge_status: str = "已證明最少"
    split_status: str = "已證明最少"

    @property
    def transfer_count(self) -> int:
        return len(self.transfers)

    @property
    def optimal(self) -> bool:
        return self.edge_status == "已證明最少" and self.split_status == "已證明最少"


def validate_groups(rows: list[BudgetRow]) -> dict[str, list[BudgetRow]]:
    groups: dict[str, list[BudgetRow]] = defaultdict(list)
    for row in rows:
        groups[row.group].append(row)
    errors = []
    for group, members in groups.items():
        difference = sum(row.delta for row in members)
        if difference:
            errors.append(f"{group}：差異合計 {difference:+,} 元")
    if errors:
        raise InputError("以下組別不平衡，無法流用：" + "；".join(errors))
    return dict(groups)


def _build_model(sources: list[BudgetRow], targets: list[BudgetRow]):
    """建立整數金流、是否轉移、是否拆分的混合整數模型。"""
    m, n = len(sources), len(targets)
    edges = m * n
    variable_count = 2 * edges + m + n
    lower = np.zeros(variable_count)
    upper = np.ones(variable_count)
    upper[:edges] = [min(-s.delta, t.delta) for s in sources for t in targets]
    integrality = np.ones(variable_count)
    data: list[float] = []
    indices: list[int] = []
    indptr: list[int] = [0]
    constraint_lower: list[float] = []
    constraint_upper: list[float] = []

    def add(terms: list[tuple[int, float]], low: float, high: float) -> None:
        for index, coefficient in terms:
            indices.append(index)
            data.append(coefficient)
        indptr.append(len(indices))
        constraint_lower.append(low)
        constraint_upper.append(high)

    for i, source in enumerate(sources):
        add([(i * n + j, 1) for j in range(n)], -source.delta, -source.delta)
    for j, target in enumerate(targets):
        add([(i * n + j, 1) for i in range(m)], target.delta, target.delta)
    for i, source in enumerate(sources):
        for j, target in enumerate(targets):
            flow = i * n + j
            edge = edges + flow
            capacity = min(-source.delta, target.delta)
            add([(flow, 1), (edge, -capacity)], -np.inf, 0)
            add([(flow, -1), (edge, 1)], -np.inf, 0)
    for i in range(m):
        terms = [(edges + i * n + j, 1) for j in range(n)]
        terms.append((2 * edges + i, -(n - 1)))
        add(terms, -np.inf, 1)
    for j in range(n):
        terms = [(edges + i * n + j, 1) for i in range(m)]
        terms.append((2 * edges + m + j, -(m - 1)))
        add(terms, -np.inf, 1)
    matrix = coo_matrix(
        (
            np.asarray(data, dtype=float),
            (
                np.repeat(np.arange(len(indptr) - 1), np.diff(indptr)),
                 np.asarray(indices)),
        ),
        shape=(len(indptr) - 1, variable_count),
    ).tocsr()
    constraints = LinearConstraint(matrix, constraint_lower, constraint_upper)
    return edges, variable_count, Bounds(lower, upper), constraints


def _subset_sums(values: list[int]) -> list[int]:
    sums = [0] * (1 << len(values))
    for mask in range(1, len(sums)):
        bit = mask & -mask
        sums[mask] = sums[mask ^ bit] + values[bit.bit_length() - 1]
    return sums


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise SearchStopped("時間上限")
    return left


def _best_partitions(sources: list[BudgetRow], targets: list[BudgetRow], deadline: float):
    """以最小平衡子集合求最多獨立部分，避免列舉所有分組。"""
    m, n = len(sources), len(targets)
    if m > 16 or n > 16:
        raise SearchStopped("科目數超過精確分組範圍")
    _remaining(deadline)
    values = [-row.delta for row in sources] + [row.delta for row in targets]
    if len(set(values)) == 1:
        atoms = [(1 << i) | (1 << (m + j)) for i in range(m) for j in range(n)]
    else:
        source_sums = _subset_sums(values[:m])
        target_sums = _subset_sums(values[m:])
        targets_by_sum: dict[int, list[int]] = defaultdict(list)
        for mask in range(1, 1 << n):
            if mask % 256 == 0:
                _remaining(deadline)
            targets_by_sum[target_sums[mask]].append(mask)
        balanced = []
        for source_mask in range(1, 1 << m):
            if source_mask % 256 == 0:
                _remaining(deadline)
            candidates = targets_by_sum.get(source_sums[source_mask], ())
            if len(balanced) + len(candidates) > 200_000:
                raise SearchStopped("平衡子集合過多")
            balanced.extend(source_mask | (target_mask << m) for target_mask in candidates)
        balanced.sort(key=lambda mask: (mask.bit_count(), mask))
        atoms = []
        atom_targets_by_source: dict[int, list[int]] = defaultdict(list)
        for index, mask in enumerate(balanced):
            if index % 256 == 0:
                _remaining(deadline)
            source_bits = mask & ((1 << m) - 1)
            target_bits = mask >> m
            subset = source_bits
            composite = False
            while subset and not composite:
                for atom_target in atom_targets_by_source.get(subset, ()):
                    if atom_target & target_bits == atom_target:
                        composite = True
                        break
                subset = (subset - 1) & source_bits
            if not composite:
                atoms.append(mask)
                atom_targets_by_source[source_bits].append(target_bits)
    full = (1 << (m + n)) - 1
    by_first: dict[int, list[int]] = defaultdict(list)
    for mask in atoms:
        first = (mask & -mask).bit_length() - 1
        by_first[first].append(mask)

    @lru_cache(None)
    def count(remaining: int) -> int:
        _remaining(deadline)
        if remaining == 0:
            return 0
        first = (remaining & -remaining).bit_length() - 1
        return max(
            (1 + count(remaining ^ part) for part in by_first[first]
             if part & remaining == part),
            default=-10**6,
        )

    best_count = count(full)

    def visit(remaining: int, parts: tuple[int, ...]):
        _remaining(deadline)
        if remaining == 0:
            yield parts
            return
        first = (remaining & -remaining).bit_length() - 1
        for part in by_first[first]:
            if part & remaining == part and 1 + count(remaining ^ part) == count(remaining):
                yield from visit(remaining ^ part, parts + (part,))

    return best_count, visit(full, ())


def _greedy(group: str, sources: list[BudgetRow],
            targets: list[BudgetRow]) -> GroupResult:
    supply = [-row.delta for row in sources]
    demand = [row.delta for row in targets]
    i = j = 0
    transfers = []
    while i < len(sources) and j < len(targets):
        amount = min(supply[i], demand[j])
        transfers.append(Transfer(sources[i], targets[j], amount))
        supply[i] -= amount
        demand[j] -= amount
        if supply[i] == 0:
            i += 1
        if demand[j] == 0:
            j += 1
    result = GroupResult(group, tuple(sources + targets), tuple(transfers), 0)
    splits = _split_count(result.transfers)
    result = GroupResult(group, result.rows, result.transfers, splits)
    verify_result(result)
    return result


def _split_count(transfers: tuple[Transfer, ...]) -> int:
    degree: dict[int, int] = defaultdict(int)
    for transfer in transfers:
        degree[transfer.source.line] += 1
        degree[transfer.target.line] += 1
    return sum(value > 1 for value in degree.values())


def _extract_equal_pairs(sources: list[BudgetRow], targets: list[BudgetRow]
                         ) -> tuple[tuple[Transfer, ...], list[BudgetRow], list[BudgetRow]]:
    by_amount: dict[int, list[BudgetRow]] = defaultdict(list)
    for target in targets:
        by_amount[target.delta].append(target)
    paired_sources: set[int] = set()
    paired_targets: set[int] = set()
    direct = []
    for source in sources:
        matches = by_amount.get(-source.delta)
        if matches:
            target = matches.pop(0)
            direct.append(Transfer(source, target, -source.delta))
            paired_sources.add(source.line)
            paired_targets.add(target.line)
    rest_sources = [row for row in sources if row.line not in paired_sources]
    rest_targets = [row for row in targets if row.line not in paired_targets]
    return tuple(direct), rest_sources, rest_targets


def _equal_pair_greedy(group: str, sources: list[BudgetRow],
                       targets: list[BudgetRow]) -> GroupResult:
    """先取一對一等額科目，再對剩餘科目做確定性配對。"""
    direct, rest_sources, rest_targets = _extract_equal_pairs(sources, targets)
    remaining = (_greedy(group, rest_sources, rest_targets).transfers
                 if rest_sources else ())
    transfers = direct + remaining
    result = GroupResult(group, tuple(sources + targets), transfers,
                         _split_count(transfers))
    verify_result(result)
    return result


def _model_candidate(group: str, sources: list[BudgetRow],
                     targets: list[BudgetRow], vector) -> GroupResult | None:
    if vector is None:
        return None
    transfers = tuple(
        Transfer(source, target, round(float(vector[i * len(targets) + j])))
        for i, source in enumerate(sources)
        for j, target in enumerate(targets)
        if round(float(vector[i * len(targets) + j])) > 0
    )
    result = GroupResult(group, tuple(sources + targets), transfers,
                         _split_count(transfers))
    try:
        verify_result(result)
    except SolveError:
        return None
    return result


def _whole_group_milp(group: str, sources: list[BudgetRow],
                      targets: list[BudgetRow], deadline: float,
                      incumbent: GroupResult) -> tuple[GroupResult, bool, bool, str]:
    """精確分組太大時，用剩餘時間直接改善整組的可行解。"""
    if len(sources) * len(targets) > 900:
        return incumbent, False, False, "流出×流入超過 900，未進行整組整數規劃"
    if deadline - time.monotonic() < 0.02:
        return incumbent, False, False, "時間上限"
    edges, size, bounds, constraints = _build_model(sources, targets)
    try:
        left = _remaining(deadline)
    except SearchStopped:
        return incumbent, False, False, "時間上限"
    edge_objective = np.zeros(size)
    edge_objective[edges:2 * edges] = 1
    first = milp(edge_objective, integrality=np.ones(size), bounds=bounds,
                 constraints=constraints,
                 options={"time_limit": max(0.001, left * 0.65), "mip_rel_gap": 0})
    candidate = _model_candidate(group, sources, targets, first.x)
    best = incumbent
    if candidate and (candidate.transfer_count, candidate.split_count) < (
        best.transfer_count, best.split_count
    ):
        best = candidate
    edge_proven = (first.status == 0 and candidate is not None and
                   candidate.transfer_count == best.transfer_count)
    if edge_proven and best.split_count == 0:
        return best, True, True, ""
    try:
        left = _remaining(deadline)
    except SearchStopped:
        return best, edge_proven, False, "時間上限"
    edge_row = np.zeros(size)
    edge_row[edges:2 * edges] = 1
    split_objective = np.zeros(size)
    split_objective[2 * edges:] = 1
    target_edges = best.transfer_count
    second = milp(split_objective, integrality=np.ones(size), bounds=bounds,
                  constraints=(constraints, LinearConstraint(edge_row, target_edges, target_edges)),
                  options={"time_limit": max(0.001, left), "mip_rel_gap": 0})
    candidate = _model_candidate(group, sources, targets, second.x)
    if candidate and (candidate.transfer_count, candidate.split_count) < (
        best.transfer_count, best.split_count
    ):
        best = candidate
    split_proven = edge_proven and second.status == 0 and candidate is not None
    reason = "" if edge_proven and split_proven else "整組整數規劃未在時間內完成證明"
    return best, edge_proven, split_proven, reason


def _solve_component(group: str, sources: list[BudgetRow],
                     targets: list[BudgetRow], deadline: float) -> tuple[GroupResult, bool]:
    fallback = _greedy(group, sources, targets)
    if len(sources) == 1 or len(targets) == 1:
        return fallback, True
    try:
        left = _remaining(deadline)
    except SearchStopped:
        return fallback, False
    edges, variable_count, bounds, constraints = _build_model(sources, targets)
    min_edges = len(sources) + len(targets) - 1
    edge_count_row = np.zeros(variable_count)
    edge_count_row[edges:2 * edges] = 1
    objective = np.zeros(variable_count)
    objective[2 * edges:] = 1
    solved = milp(objective, integrality=np.ones(variable_count), bounds=bounds,
                  constraints=(constraints, LinearConstraint(edge_count_row, min_edges, min_edges)),
                  options={"time_limit": max(0.001, left), "mip_rel_gap": 0})
    if solved.x is None:
        return fallback, False
    transfers = []
    for i, source in enumerate(sources):
        for j, target in enumerate(targets):
            amount = round(float(solved.x[i * len(targets) + j]))
            if amount > 0:
                transfers.append(Transfer(source, target, amount))
    result = GroupResult(group, tuple(sources + targets), tuple(transfers),
                         _split_count(tuple(transfers)))
    try:
        verify_result(result)
        if result.transfer_count != min_edges:
            return fallback, False
    except SolveError:
        return fallback, False
    if (result.split_count, tuple((t.source.line, t.target.line) for t in result.transfers)) >= (
        fallback.split_count, tuple((t.source.line, t.target.line) for t in fallback.transfers)
    ) and solved.status != 0:
        return fallback, False
    return result, solved.status == 0


def solve_group(group: str, rows: list[BudgetRow], time_limit: float = 30) -> GroupResult:
    sources = [row for row in rows if row.delta < 0]
    targets = [row for row in rows if row.delta > 0]
    if not sources and not targets:
        return GroupResult(group, tuple(rows), (), 0)
    deadline = time.monotonic() + time_limit
    greedy = _greedy(group, sources, targets)
    equal_first = _equal_pair_greedy(group, sources, targets)
    chosen = min((greedy, equal_first), key=lambda result: (
        result.transfer_count, result.split_count
    ))
    best = GroupResult(group, tuple(rows), chosen.transfers, chosen.split_count)
    direct, rest_sources, rest_targets = _extract_equal_pairs(sources, targets)
    # 等額一對一可成為某個最少筆數分組的獨立部分；固定它們不影響主要目標。
    fixed = ()
    search_sources, search_targets = sources, targets
    if direct and (len(sources) > 16 or len(targets) > 16):
        fixed = direct
        search_sources, search_targets = rest_sources, rest_targets
    if fixed and not search_sources:
        final = GroupResult(group, tuple(rows), fixed, 0)
        verify_result(final)
        return final
    max_components = None
    partition_list = []
    complete_list = False
    reason = "時間上限"
    try:
        max_components, partitions = _best_partitions(search_sources, search_targets, deadline)
        if max_components < 1:
            raise SolveError(f"{group} 找不到平衡分組")
        for partition in islice(partitions, 101):
            partition_list.append(partition)
        complete_list = len(partition_list) <= 100
    except SearchStopped as stopped:
        reason = str(stopped)
        if not fixed and direct and rest_sources and rest_targets and reason != "時間上限":
            try:
                reduced_count, reduced_partitions = _best_partitions(
                    rest_sources, rest_targets, deadline
                )
                reduced_list = list(islice(reduced_partitions, 101))
                fixed = direct
                search_sources, search_targets = rest_sources, rest_targets
                max_components = reduced_count
                partition_list = reduced_list
                complete_list = len(reduced_list) <= 100
            except SearchStopped as reduced_stop:
                reason = str(reduced_stop)

    whole_edge_proven = whole_split_proven = False
    if max_components is None and reason != "時間上限":
        improved, whole_edge_proven, whole_split_proven, model_reason = _whole_group_milp(
            group, sources, targets, deadline, best
        )
        best = GroupResult(group, tuple(rows), improved.transfers, improved.split_count)
        if model_reason:
            reason = model_reason

    solved_components: dict[int, tuple[GroupResult, bool]] = {}
    examined = 0
    all_proven = True
    for partition in partition_list[:100]:
        if time.monotonic() >= deadline:
            reason = "時間上限"
            break
        components = []
        for mask in partition:
            if mask not in solved_components:
                part_sources = [row for i, row in enumerate(search_sources) if mask & (1 << i)]
                part_targets = [row for j, row in enumerate(search_targets)
                                if mask & (1 << (len(search_sources) + j))]
                solved_components[mask] = _solve_component(
                    group, part_sources, part_targets, deadline
                )
            components.append(solved_components[mask])
        all_proven &= all(proven for _, proven in components)
        transfers = fixed + tuple(
            transfer for component, _ in components for transfer in component.transfers
        )
        candidate = GroupResult(group, tuple(rows), transfers, _split_count(transfers))
        verify_result(candidate)
        examined += 1
        if (candidate.transfer_count, candidate.split_count) < (best.transfer_count, best.split_count):
            best = candidate

    edge_proven = (best.transfer_count == max(len(sources), len(targets)) or
                   whole_edge_proven or
                   (max_components is not None and
                    best.transfer_count == len(fixed) + len(search_sources) +
                    len(search_targets) - max_components))
    split_proven = (whole_split_proven or
                    (edge_proven and best.split_count == 0) or
                    (edge_proven and not fixed and complete_list and
                     examined == len(partition_list) and all_proven))
    if edge_proven:
        edge_status = "已證明最少"
    else:
        edge_status = f"未證明（{reason}）"
    if split_proven:
        split_status = "已證明最少"
    elif edge_proven and fixed:
        split_status = "未證明（固定等額配對後未比較全體方案）"
    elif edge_proven and len(partition_list) > 100 and examined == 100 and all_proven:
        split_status = "在前 100 種分組中最少，未證明全體最少"
    else:
        split_status = f"未證明（{reason}）"
    final = GroupResult(group, tuple(rows), best.transfers, best.split_count,
                        edge_status, split_status)
    verify_result(final)
    return final


def verify_result(result: GroupResult) -> None:
    actual = {row.line: 0 for row in result.rows}
    degree = {row.line: 0 for row in result.rows}
    known = set(actual)
    for transfer in result.transfers:
        if transfer.amount <= 0 or transfer.source.line not in known or transfer.target.line not in known:
            raise SolveError("求解器產生不合法的轉移")
        if transfer.source.group != result.group or transfer.target.group != result.group:
            raise SolveError("求解器產生跨組轉移")
        if transfer.source.delta >= 0 or transfer.target.delta <= 0:
            raise SolveError("求解器產生方向錯誤的轉移")
        actual[transfer.source.line] -= transfer.amount
        actual[transfer.target.line] += transfer.amount
        degree[transfer.source.line] += 1
        degree[transfer.target.line] += 1
    if any(actual[row.line] != row.delta for row in result.rows):
        raise SolveError("求解器結果未完全核對各科目的增減")
    if sum(value > 1 for value in degree.values()) != result.split_count:
        raise SolveError("求解器結果的拆分科目數不一致")


def solve(rows: list[BudgetRow], time_limit: float = 30) -> list[GroupResult]:
    if not math.isfinite(time_limit) or time_limit <= 0:
        raise InputError("求解時間上限必須為大於 0 的有限數字")
    return [solve_group(group, members, time_limit)
            for group, members in validate_groups(rows).items()]
