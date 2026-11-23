from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .board import Board
from .footprint import Footprint

SelectionCriteria = Literal[
    "max_connection",  # max r_ij к уже размещённым (3.3.1)
    "sum_placed",      # сумма r_ij к уже размещённым (3.3.3)
    "common_nets",     # число цепей, общих с размещённой группой (3.3.4)
    "absolute",        # сумма placed минус сумма unplaced (3.3.5)
    "relative",        # доля связности с placed от общей связности (3.3.6)
]
AnchorPosition = Literal["center", "top_left"]
ComponentPriority = Literal["max_connections", "max_size"]


@dataclass
class PlacementResult:
    execution_time_sec: float
    component_count: int
    placed_count: int
    total_wire_length_mm: float
    placement_order: list[str]
    failed_to_place: list[str] = field(default_factory=list)


class AutoPlacer:
    def __init__(
        self,
        board: Board,
        grid_step_mm: float = 10.0,
        anchor: AnchorPosition = "center",
        criteria: SelectionCriteria = "absolute",
        component_priority: ComponentPriority = "max_connections",
        max_workers: int | None = None,
        neighbor_offset_mm: float = 20.0,
        padding_mm: float = 1.5,
        min_center_dist_mm: float = 0.0,
        max_search_expansions: int = 3,
        chain_lambda: int = 0,
    ):
        if grid_step_mm <= 0:
            raise ValueError("grid_step_mm должен быть больше нуля")
        if anchor not in ("center", "top_left"):
            raise ValueError("anchor должен быть 'center' или 'top_left'")
        if criteria not in (
            "max_connection", "sum_placed", "common_nets", "absolute", "relative"
        ):
            raise ValueError(f"Неизвестный критерий выбора элемента: {criteria}")
        if component_priority not in ("max_connections", "max_size"):
            raise ValueError("component_priority должен быть 'max_connections' или 'max_size'")
        if max_workers is None:
            max_workers = min(4, max(1, os.cpu_count() or 1))
        if not 1 <= int(max_workers) <= 10:
            raise ValueError("max_workers должен быть в диапазоне от 1 до 10")
        if int(chain_lambda) < -1:
            raise ValueError("chain_lambda должен быть не меньше -1")

        self.board = board
        self.grid_step = float(grid_step_mm)
        self.anchor = anchor
        self.criteria = criteria
        self.component_priority = component_priority
        self.max_workers = int(max_workers)
        self.neighbor_offset = float(neighbor_offset_mm)
        self.padding = float(padding_mm)
        self.min_center_dist = float(min_center_dist_mm)
        self.max_search_expansions = max(1, int(max_search_expansions))
        self.chain_lambda = int(chain_lambda)

        self.modules = board.modules
        self.n = len(self.modules)
        self.C = np.zeros((self.n, self.n), dtype=np.float64)
        self.nets_per_module: list[set[int]] = [set() for _ in range(self.n)]
        self.positions: list[tuple[float, float]] = [
            (float(fp.po_x), float(fp.po_y)) for fp in self.modules
        ]
        self.placed_indices: list[int] = []
        self.unplaced_indices: list[int] = list(range(self.n))
        self.placement_order: list[str] = []
        self.failed_to_place: list[str] = []

        self.anchor_index: int | None = None
        self.fixed_indices: set[int] = set()
        self._sizes: list[float | None] = [None] * self.n
        self._local_bbox_cache: list[tuple[float, float, float, float] | None] = [
            None
        ] * self.n
        self._bbox_computed: list[bool] = [False] * self.n

    # ------------------------------------------------------------------
    # Матрица связности — формула (3.3.2)
    # ------------------------------------------------------------------
    def _build_connectivity_matrix(self) -> None:
        """Построить симметричную матрицу r_ij.

        rho_s — число разных компонентов, подключённых к цепи s.
        Вклад цепи: ((rho_s + lambda) / rho_s) * omega_s.
        omega_s пока принимается равным 1: формат .brd не содержит
        отдельного веса цепи, используемого алгоритмом размещения.
        """
        self.nets_per_module = [
            {int(p.net) for p in fp.pads if p.net > 0}
            for fp in self.modules
        ]
        net_to_modules: dict[int, list[int]] = {}
        for module_idx, nets in enumerate(self.nets_per_module):
            for net in nets:
                net_to_modules.setdefault(net, []).append(module_idx)

        self.C.fill(0.0)
        for module_indices in net_to_modules.values():
            rho = len(module_indices)
            if rho < 2:
                continue
            weight = ((rho + self.chain_lambda) / rho) * 1.0
            for offset, i in enumerate(module_indices):
                for j in module_indices[offset + 1:]:
                    self.C[i, j] += weight
                    self.C[j, i] += weight

    # ------------------------------------------------------------------
    # Границы платы
    # ------------------------------------------------------------------
    def _get_board_bounds(self) -> tuple[float, float, float, float]:
        edge = [d for d in self.board.drawings if d.layer == "Edge.Cuts"]
        if not edge:
            if self.modules:
                xs = [float(fp.po_x) for fp in self.modules]
                ys = [float(fp.po_y) for fp in self.modules]
                min_x, max_x = min(xs), max(xs)
                min_y, max_y = min(ys), max(ys)
                # В отсутствие контура и разброса координат не сжимаем
                # плату до нулевого прямоугольника.
                if math.isclose(min_x, max_x):
                    min_x -= 50.0
                    max_x += 50.0
                if math.isclose(min_y, max_y):
                    min_y -= 50.0
                    max_y += 50.0
                return min_x, min_y, max_x, max_y
            return 0.0, 0.0, 100.0, 100.0

        xs: list[float] = []
        ys: list[float] = []
        for drawing in edge:
            if drawing.shape in (1, 2):
                radius = math.hypot(drawing.x2 - drawing.x1, drawing.y2 - drawing.y1)
                xs.extend((drawing.x1 - radius, drawing.x1 + radius))
                ys.extend((drawing.y1 - radius, drawing.y1 + radius))
            else:
                xs.extend((drawing.x1, drawing.x2))
                ys.extend((drawing.y1, drawing.y2))
        return min(xs), min(ys), max(xs), max(ys)

    # ------------------------------------------------------------------
    # Геометрия компонентов
    # ------------------------------------------------------------------
    @staticmethod
    def _transform_point(
        lx: float, ly: float, angle_deg: float, mirrored: bool
    ) -> tuple[float, float]:
        if mirrored:
            ly = -ly
        if angle_deg:
            angle = math.radians(angle_deg)
            c, s = math.cos(angle), math.sin(angle)
            return lx * c + ly * s, -lx * s + ly * c
        return lx, ly

    def _module_angle_mirrored(self, idx: int) -> tuple[float, bool]:
        fp = self.modules[idx]
        return fp.po_rotation / 10.0, fp.po_layer == 0

    def _local_points(self, idx: int) -> list[tuple[float, float]]:
        fp = self.modules[idx]
        points: list[tuple[float, float]] = []
        for pad in fp.pads:
            points.append((pad.x, pad.y))
        for line in fp.lines:
            points.extend(((line.x1, line.y1), (line.x2, line.y2)))
        for circle in fp.circles:
            radius = circle.radius
            points.extend((
                (circle.cx - radius, circle.cy - radius),
                (circle.cx + radius, circle.cy + radius),
            ))
        for arc in fp.arcs:
            radius = math.hypot(arc.sx - arc.cx, arc.sy - arc.cy)
            points.extend((
                (arc.cx - radius, arc.cy - radius),
                (arc.cx + radius, arc.cy + radius),
            ))
        return points

    def _local_bbox(self, idx: int) -> tuple[float, float, float, float] | None:
        if self._bbox_computed[idx]:
            return self._local_bbox_cache[idx]
        local = self._local_points(idx)
        if not local:
            self._bbox_computed[idx] = True
            self._local_bbox_cache[idx] = None
            return None
        angle, mirrored = self._module_angle_mirrored(idx)
        transformed = [self._transform_point(x, y, angle, mirrored) for x, y in local]
        xs = [p[0] for p in transformed]
        ys = [p[1] for p in transformed]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        self._bbox_computed[idx] = True
        self._local_bbox_cache[idx] = bbox
        return bbox

    def _get_component_bbox(
        self, idx: int, x: float, y: float
    ) -> tuple[float, float, float, float]:
        bbox = self._local_bbox(idx)
        if bbox is None:
            return x - 5.0 - self.padding, y - 5.0 - self.padding, \
                   x + 5.0 + self.padding, y + 5.0 + self.padding
        x1, y1, x2, y2 = bbox
        return (
            x + x1 - self.padding,
            y + y1 - self.padding,
            x + x2 + self.padding,
            y + y2 + self.padding,
        )

    def _get_component_size(self, idx: int) -> float:
        cached = self._sizes[idx]
        if cached is not None:
            return cached
        bbox = self._local_bbox(idx)
        if bbox is None:
            self._sizes[idx] = 0.0
        else:
            x1, y1, x2, y2 = bbox
            self._sizes[idx] = max(0.0, (x2 - x1) * (y2 - y1))
        return float(self._sizes[idx] or 0.0)

    def _check_overlap(self, idx: int, x: float, y: float) -> bool:
        candidate = self._get_component_bbox(idx, x, y)
        for placed_idx in self.placed_indices:
            if placed_idx == idx:
                continue
            px, py = self.positions[placed_idx]
            other = self._get_component_bbox(placed_idx, px, py)
            if (candidate[2] <= other[0] or candidate[0] >= other[2]
                    or candidate[3] <= other[1] or candidate[1] >= other[3]):
                continue
            return True
        return False

    # ------------------------------------------------------------------
    # Начальный компонент (фиксированный anchor)
    # ------------------------------------------------------------------
    def _place_anchor(self) -> None:
        if self.n == 0:
            return
        if self.component_priority == "max_connections":
            scores = self.C.sum(axis=1)
            anchor_idx = int(np.argmax(scores))
        else:
            sizes = [self._get_component_size(i) for i in range(self.n)]
            anchor_idx = int(np.argmax(sizes))

        self.anchor_index = anchor_idx
        self.fixed_indices.add(anchor_idx)
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        board_cx = (b_min_x + b_max_x) / 2.0
        board_cy = (b_min_y + b_max_y) / 2.0
        bbox = self._local_bbox(anchor_idx)

        if bbox is None:
            x, y = ((board_cx, board_cy) if self.anchor == "center"
                    else (b_min_x + self.padding, b_min_y + self.padding))
        else:
            x1, y1, x2, y2 = bbox
            if self.anchor == "center":
                x = board_cx - (x1 + x2) / 2.0
                y = board_cy - (y1 + y2) / 2.0
            else:
                x = b_min_x - x1 + self.padding
                y = b_min_y - y1 + self.padding
        self._commit_placement(anchor_idx, x, y)

    # ------------------------------------------------------------------
    # Выбор следующего компонента — формулы (3.3.1), (3.3.3)–(3.3.6)
    # ------------------------------------------------------------------
    def _calculate_J_all(self) -> np.ndarray:
        if not self.unplaced_indices:
            return np.array([], dtype=np.float64)

        placed_mask = np.zeros(self.n, dtype=bool)
        placed_mask[self.placed_indices] = True
        unplaced_mask = ~placed_mask
        unplaced = np.asarray(self.unplaced_indices, dtype=np.intp)
        c_sub = self.C[unplaced]
        sum_placed = c_sub[:, placed_mask].sum(axis=1)

        if self.criteria == "max_connection":
            if not self.placed_indices:
                return np.zeros(len(unplaced), dtype=np.float64)
            return c_sub[:, placed_mask].max(axis=1)
        if self.criteria == "sum_placed":
            return sum_placed
        if self.criteria == "common_nets":
            placed_nets: set[int] = set()
            for idx in self.placed_indices:
                placed_nets.update(self.nets_per_module[idx])
            return np.fromiter(
                (len(self.nets_per_module[idx] & placed_nets) for idx in unplaced),
                dtype=np.float64,
                count=len(unplaced),
            )
        if self.criteria == "absolute":
            sum_unplaced = c_sub[:, unplaced_mask].sum(axis=1)
            return sum_placed - sum_unplaced
        if self.criteria == "relative":
            total = c_sub.sum(axis=1)
            result = np.zeros(len(unplaced), dtype=np.float64)
            nonzero = total > 0
            result[nonzero] = sum_placed[nonzero] / total[nonzero]
            return result
        raise ValueError(f"Неизвестный критерий: {self.criteria}")

    # ------------------------------------------------------------------
    # Генерация соседних допустимых позиций
    # ------------------------------------------------------------------
    def _generate_candidates(
        self,
        chosen_idx: int,
        neighbor_offset: float | None = None,
        check_overlap: bool = True,
        grid_step: float | None = None,
    ) -> np.ndarray:
        if not self.placed_indices:
            return np.empty((0, 2), dtype=np.float64)
        offset = self.neighbor_offset if neighbor_offset is None else neighbor_offset
        step = self.grid_step if grid_step is None else grid_step
        if step <= 0:
            return np.empty((0, 2), dtype=np.float64)

        placed_pos = np.asarray([self.positions[i] for i in self.placed_indices])
        min_x, min_y = placed_pos.min(axis=0) - offset
        max_x, max_y = placed_pos.max(axis=0) + offset
        bbox = self._local_bbox(chosen_idx)
        if bbox is None:
            return np.empty((0, 2), dtype=np.float64)
        l_min_x, l_min_y, l_max_x, l_max_y = bbox
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        min_x = max(min_x, b_min_x - l_min_x + self.padding)
        min_y = max(min_y, b_min_y - l_min_y + self.padding)
        max_x = min(max_x, b_max_x - l_max_x - self.padding)
        max_y = min(max_y, b_max_y - l_max_y - self.padding)
        if max_x < min_x or max_y < min_y:
            return np.empty((0, 2), dtype=np.float64)

        eps = 1e-9
        x_coords = np.arange(min_x, max_x + eps, step)
        y_coords = np.arange(min_y, max_y + eps, step)
        if not len(x_coords) or not len(y_coords):
            return np.empty((0, 2), dtype=np.float64)
        xx, yy = np.meshgrid(x_coords, y_coords)
        candidates = np.column_stack((xx.ravel(), yy.ravel()))

        if check_overlap and self.min_center_dist > 0 and len(placed_pos):
            distances = np.linalg.norm(
                candidates[:, None, :] - placed_pos[None, :, :], axis=2
            )
            candidates = candidates[(distances > self.min_center_dist).all(axis=1)]
        if not len(candidates):
            return candidates
        if check_overlap:
            valid = [
                (float(x), float(y)) for x, y in candidates
                if not self._check_overlap(chosen_idx, float(x), float(y))
            ]
            return np.asarray(valid, dtype=np.float64).reshape((-1, 2))
        return candidates

    # ------------------------------------------------------------------
    # Выбор позиции — минимизация F = sum(r_ij * distance)
    # ------------------------------------------------------------------
    @staticmethod
    def _calculate_F_chunk(
        args: tuple[np.ndarray, np.ndarray, np.ndarray]
    ) -> np.ndarray:
        chunk, placed_pos, weights = args
        diff = chunk[:, None, :] - placed_pos[None, :, :]
        distances = np.linalg.norm(diff, axis=2)
        return np.dot(distances, weights)

    def _find_best_position(
        self, chosen_idx: int, candidates: np.ndarray
    ) -> tuple[float, float] | None:
        if len(candidates) == 0:
            return None
        placed_pos = np.asarray([self.positions[i] for i in self.placed_indices])
        weights = self.C[chosen_idx, self.placed_indices]
        if len(candidates) < 64 or self.max_workers == 1:
            values = self._calculate_F_chunk((candidates, placed_pos, weights))
        else:
            workers = min(self.max_workers, len(candidates))
            chunks = [part for part in np.array_split(candidates, workers) if len(part)]
            tasks = [(part, placed_pos, weights) for part in chunks]
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                values = np.concatenate(list(pool.map(self._calculate_F_chunk, tasks)))
        if not len(values):
            return None
        best_value = values.min()
        best_idx = int(np.flatnonzero(np.isclose(values, best_value))[0])
        return float(candidates[best_idx, 0]), float(candidates[best_idx, 1])

    # ------------------------------------------------------------------
    # Фиксация размещения и метрики
    # ------------------------------------------------------------------
    def _commit_placement(self, idx: int, x: float, y: float) -> None:
        self.modules[idx].po_x = float(x)
        self.modules[idx].po_y = float(y)
        self.positions[idx] = (float(x), float(y))
        if idx not in self.placed_indices:
            self.placed_indices.append(idx)
        if idx in self.unplaced_indices:
            self.unplaced_indices.remove(idx)
        if self.modules[idx].name not in self.placement_order:
            self.placement_order.append(self.modules[idx].name)

    def _calculate_total_wire_length(self) -> float:
        if self.n == 0:
            return 0.0
        rows, cols = np.where(np.triu(self.C, k=1) > 0)
        if not len(rows):
            return 0.0
        positions = np.asarray(self.positions)
        distances = np.linalg.norm(positions[rows] - positions[cols], axis=1)
        return float(np.dot(distances, self.C[rows, cols]))

    def _wire_contribution(self, idx: int) -> float:
        row = self.C[idx]
        mask = row > 0
        if not mask.any():
            return 0.0
        pos = np.asarray(self.positions)
        origin = np.asarray(self.positions[idx])
        distances = np.linalg.norm(pos[mask] - origin, axis=1)
        return float(np.dot(row[mask], distances))

    def _in_bounds(self, idx: int, x: float, y: float) -> bool:
        bbox = self._get_component_bbox(idx, x, y)
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        eps = 0.01
        return (bbox[0] >= b_min_x - eps and bbox[1] >= b_min_y - eps
                and bbox[2] <= b_max_x + eps and bbox[3] <= b_max_y + eps)

    def _try_swap(self, i: int, j: int) -> bool:
        """Обмен позиций; фиксированные компоненты не затрагиваются."""
        if i in self.fixed_indices or j in self.fixed_indices:
            return False
        pi, pj = self.positions[i], self.positions[j]
        old_value = self._wire_contribution(i) + self._wire_contribution(j)
        self.positions[i], self.positions[j] = pj, pi
        self.modules[i].po_x, self.modules[i].po_y = pj
        self.modules[j].po_x, self.modules[j].po_y = pi

        valid = self._in_bounds(i, *pj) and self._in_bounds(j, *pi)
        if valid:
            valid = not self._check_overlap(i, *pj) and not self._check_overlap(j, *pi)
        new_value = (self._wire_contribution(i) + self._wire_contribution(j)) if valid else math.inf
        if valid and new_value < old_value - 0.01:
            return True

        self.positions[i], self.positions[j] = pi, pj
        self.modules[i].po_x, self.modules[i].po_y = pi
        self.modules[j].po_x, self.modules[j].po_y = pj
        return False

    def _try_relocate(self, idx: int) -> bool:
        """Локальное улучшение позиции, кроме зафиксированного anchor."""
        if idx in self.fixed_indices:
            return False
        saved_placed = list(self.placed_indices)
        self.placed_indices = [i for i in saved_placed if i != idx]
        if not self.placed_indices:
            self.placed_indices = saved_placed
            return False

        placed_pos = np.asarray([self.positions[i] for i in self.placed_indices])
        weights = np.asarray([self.C[idx, i] for i in self.placed_indices])
        old_pos = np.asarray(self.positions[idx])
        old_value = float(np.dot(np.linalg.norm(placed_pos - old_pos, axis=1), weights))
        best_pos = None
        best_value = old_value
        try:
            for grid_div in (1, 2):
                for attempt in range(2):
                    candidates = self._generate_candidates(
                        idx,
                        neighbor_offset=self.neighbor_offset * (1 + attempt),
                        check_overlap=True,
                        grid_step=self.grid_step / grid_div,
                    )
                    if not len(candidates):
                        continue
                    distances = np.linalg.norm(
                        candidates[:, None, :] - placed_pos[None, :, :], axis=2
                    )
                    values = np.dot(distances, weights)
                    candidate_idx = int(np.argmin(values))
                    value = float(values[candidate_idx])
                    if value < best_value - 0.01:
                        best_value = value
                        best_pos = (float(candidates[candidate_idx, 0]),
                                    float(candidates[candidate_idx, 1]))
                        break
                if best_pos is not None:
                    break
        finally:
            self.placed_indices = saved_placed

        if best_pos is None:
            return False
        self.positions[idx] = best_pos
        self.modules[idx].po_x, self.modules[idx].po_y = best_pos
        return True

    def _find_any_free_position(self, idx: int) -> tuple[float, float] | None:
        bbox = self._local_bbox(idx)
        if bbox is None:
            return None
        l_min_x, l_min_y, l_max_x, l_max_y = bbox
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        x_min = b_min_x - l_min_x + self.padding
        y_min = b_min_y - l_min_y + self.padding
        x_max = b_max_x - l_max_x - self.padding
        y_max = b_max_y - l_max_y - self.padding
        if x_max < x_min or y_max < y_min:
            return None
        step = max(0.1, min(self.grid_step / 4.0, 2.5))
        for y in np.arange(y_min, y_max + 1e-9, step):
            for x in np.arange(x_min, x_max + 1e-9, step):
                if not self._check_overlap(idx, float(x), float(y)):
                    return float(x), float(y)
        return None

    def _iterative_improve(self, max_passes: int = 10) -> int:
        if self.n < 2:
            return 0
        improvements = 0
        for _ in range(max_passes):
            changed = False
            placed = list(self.placed_indices)
            for a in range(len(placed)):
                for b in range(a + 1, len(placed)):
                    if self._try_swap(placed[a], placed[b]):
                        changed = True
                        improvements += 1
            for idx in list(self.placed_indices):
                if self._try_relocate(idx):
                    changed = True
                    improvements += 1
            if not changed:
                break
        return improvements

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------
    def run(self) -> PlacementResult:
        started = time.perf_counter()
        if self.n == 0:
            return PlacementResult(0.0, 0, 0, 0.0, [], [])

        self._build_connectivity_matrix()
        self._place_anchor()
        if self.n == 1:
            elapsed = time.perf_counter() - started
            return PlacementResult(
                elapsed, 1, 1, 0.0, list(self.placement_order), []
            )

        # ВАЖНО: все компоненты после anchor выбираются по одному
        # выбранному пользователем критерию J. Отдельного этапа, который
        # принудительно ставит крупные корпуса вне этого критерия, нет.
        pending: list[int] = []
        while self.unplaced_indices:
            j_values = self._calculate_J_all()
            if not len(j_values):
                break
            selected_local = int(np.argmax(j_values))
            chosen_idx = self.unplaced_indices[selected_local]
            best_pos = None

            for grid_div in (1, 2, 4):
                for attempt in range(self.max_search_expansions):
                    candidates = self._generate_candidates(
                        chosen_idx,
                        neighbor_offset=self.neighbor_offset * (1 + attempt),
                        check_overlap=True,
                        grid_step=self.grid_step / grid_div,
                    )
                    if len(candidates):
                        best_pos = self._find_best_position(chosen_idx, candidates)
                        if best_pos is not None:
                            break
                if best_pos is not None:
                    break

            if best_pos is None:
                pending.append(chosen_idx)
                self.unplaced_indices.remove(chosen_idx)
            else:
                self._commit_placement(chosen_idx, *best_pos)

        # Повторная попытка для отложенных компонентов после одного прохода
        # локального улучшения. Фиксированный anchor при этом не двигается.
        if pending:
            self._iterative_improve(max_passes=1)
            remaining: list[int] = []
            for idx in pending:
                best_pos = None
                for grid_div in (4, 8):
                    for attempt in range(self.max_search_expansions):
                        candidates = self._generate_candidates(
                            idx,
                            neighbor_offset=self.neighbor_offset * (2 + attempt),
                            check_overlap=True,
                            grid_step=self.grid_step / grid_div,
                        )
                        if len(candidates):
                            best_pos = self._find_best_position(idx, candidates)
                            if best_pos is not None:
                                break
                    if best_pos is not None:
                        break
                if best_pos is None:
                    remaining.append(idx)
                else:
                    self._commit_placement(idx, *best_pos)
            pending = remaining

        self._iterative_improve(max_passes=1)

        # Если локальный поиск рядом с группой не помог, ищем любое свободное
        # место по всей плате. Пересечения не допускаются.
        if pending:
            remaining = []
            for idx in pending:
                position = self._find_any_free_position(idx)
                if position is None:
                    remaining.append(idx)
                else:
                    self._commit_placement(idx, *position)
            pending = remaining

        for idx in pending:
            self.failed_to_place.append(self.modules[idx].name)

        elapsed = time.perf_counter() - started
        return PlacementResult(
            execution_time_sec=elapsed,
            component_count=self.n,
            placed_count=len(self.placed_indices),
            total_wire_length_mm=self._calculate_total_wire_length(),
            placement_order=list(self.placement_order),
            failed_to_place=list(self.failed_to_place),
        )


def auto_place_board(
    board: Board,
    grid_step_mm: float = 10.0,
    anchor: AnchorPosition = "center",
    criteria: SelectionCriteria = "absolute",
    component_priority: ComponentPriority = "max_connections",
    max_workers: int | None = None,
    neighbor_offset_mm: float = 20.0,
    padding_mm: float = 1.5,
    min_center_dist_mm: float = 0.0,
    chain_lambda: int = 0,
) -> PlacementResult:
    """Удобная обёртка над :class:`AutoPlacer`."""
    return AutoPlacer(
        board=board,
        grid_step_mm=grid_step_mm,
        anchor=anchor,
        criteria=criteria,
        component_priority=component_priority,
        max_workers=max_workers,
        neighbor_offset_mm=neighbor_offset_mm,
        padding_mm=padding_mm,
        min_center_dist_mm=min_center_dist_mm,
        chain_lambda=chain_lambda,
    ).run()
