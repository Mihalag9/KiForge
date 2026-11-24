"""Алгоритм авторазмещения корпусов на legacy-плате KiCad.

Поддерживает:
- критерии выбора следующего корпуса (3.3.1, 3.3.3, 3.3.4, 3.3.5, 3.3.6);
- вес цепи omega_s = P_s / P_avg в матрице r_ij;
- евклидову и манхэттенскую метрики расстояния d;
- независимые параметры A (отступ), B (зазор) и C (шаг сетки);
- ограничение числа кандидатных позиций;
- многопоточную обработку J, R_k и F;
- локальную оптимизацию посредством swap и relocate.

total_weighted_connection_length_mm — алгоритмическая взвешенная оценка
расстояний между корпусами, а не фактическая длина медных дорожек.
"""
from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .board import Board

SelectionCriteria = Literal[
    "max_connection",
    "sum_placed",
    "common_nets",
    "absolute",
    "relative",
]
AnchorPosition = Literal["center", "top_left"]
ComponentPriority = Literal["max_connections", "max_size"]
DistanceMetric = Literal["euclidean", "manhattan"]


@dataclass
class PlacementResult:
    """Итоги размещения и значения целевой функции на разных этапах."""

    execution_time_sec: float
    component_count: int
    placed_count: int
    connection_count: int
    total_weighted_connection_length_mm: float
    placement_order: list[str]
    failed_to_place: list[str] = field(default_factory=list)
    initial_weighted_connection_length_mm: float = 0.0
    after_placement_weighted_connection_length_mm: float = 0.0

    @property
    def after_local_optimization_weighted_connection_length_mm(self) -> float:
        """Оценка после локальной оптимизации."""
        return self.total_weighted_connection_length_mm

    @property
    def placement_reduction_mm(self) -> float:
        """Сокращение оценки от исходного состояния до окончания размещения."""
        return (
            self.initial_weighted_connection_length_mm
            - self.after_placement_weighted_connection_length_mm
        )

    @property
    def placement_improvement_percent(self) -> float:
        initial = self.initial_weighted_connection_length_mm
        if abs(initial) < 1e-12:
            return 0.0
        return self.placement_reduction_mm / initial * 100.0

    @property
    def local_optimization_reduction_mm(self) -> float:
        """Сокращение оценки за счёт локальной оптимизации."""
        return (
            self.after_placement_weighted_connection_length_mm
            - self.after_local_optimization_weighted_connection_length_mm
        )

    @property
    def local_optimization_improvement_percent(self) -> float:
        before = self.after_placement_weighted_connection_length_mm
        if abs(before) < 1e-12:
            return 0.0
        return self.local_optimization_reduction_mm / before * 100.0

    @property
    def total_wire_length_mm(self) -> float:
        """Устаревший псевдоним; значение не является длиной дорожек."""
        return self.total_weighted_connection_length_mm


class AutoPlacer:
    """Последовательное размещение корпусов с локальным улучшением."""

    def __init__(
        self,
        board: Board,
        grid_step_mm: float = 1.27,
        anchor: AnchorPosition = "center",
        criteria: SelectionCriteria = "absolute",
        component_priority: ComponentPriority = "max_connections",
        max_workers: int | None = None,
        neighbor_offset_mm: float = 20.0,
        min_center_dist_mm: float = 0.0,
        max_search_expansions: int = 3,
        chain_lambda: int = 0,
        distance_metric: DistanceMetric = "euclidean",
        board_margin_mm: float = 5.0,
        min_clearance_mm: float = 1.0,
        max_grid_positions: int = 100_000,
        padding_mm: float | None = None,
    ):
        if not math.isfinite(float(grid_step_mm)) or grid_step_mm <= 0:
            raise ValueError("grid_step_mm должен быть конечным числом больше нуля")
        if anchor not in ("center", "top_left"):
            raise ValueError("anchor должен быть 'center' или 'top_left'")
        if criteria not in (
            "max_connection", "sum_placed", "common_nets", "absolute", "relative"
        ):
            raise ValueError(f"Неизвестный критерий выбора элемента: {criteria}")
        if component_priority not in ("max_connections", "max_size"):
            raise ValueError(
                "component_priority должен быть 'max_connections' или 'max_size'"
            )
        if distance_metric not in ("euclidean", "manhattan"):
            raise ValueError(
                "distance_metric должен быть 'euclidean' или 'manhattan'"
            )
        if max_workers is None:
            max_workers = min(4, max(1, os.cpu_count() or 1))
        if not 1 <= int(max_workers) <= 10:
            raise ValueError("max_workers должен быть в диапазоне от 1 до 10")
        if int(chain_lambda) < -1:
            raise ValueError("chain_lambda должен быть не меньше -1")
        if not math.isfinite(float(board_margin_mm)) or board_margin_mm < 0:
            raise ValueError("board_margin_mm должен быть неотрицательным числом")
        if not math.isfinite(float(min_clearance_mm)) or min_clearance_mm < 0:
            raise ValueError("min_clearance_mm должен быть неотрицательным числом")
        if int(max_grid_positions) < 1:
            raise ValueError("max_grid_positions должен быть не меньше 1")
        if not math.isfinite(float(neighbor_offset_mm)) or neighbor_offset_mm <= 0:
            raise ValueError("neighbor_offset_mm должен быть больше нуля")
        if not math.isfinite(float(min_center_dist_mm)) or min_center_dist_mm < 0:
            raise ValueError("min_center_dist_mm должен быть неотрицательным числом")

        # Обратная совместимость со старым API: padding_mm раньше применялся
        # к внешним границам каждого корпуса. Теперь его эквивалентом являются
        # отступ A и удвоенный межкорпусный зазор B.
        if padding_mm is not None:
            if not math.isfinite(float(padding_mm)) or padding_mm < 0:
                raise ValueError("padding_mm должен быть неотрицательным числом")
            board_margin_mm = float(padding_mm)
            min_clearance_mm = 2.0 * float(padding_mm)

        self.board = board
        self.modules = board.modules
        self.n = len(self.modules)

        self.grid_step = float(grid_step_mm)       # C
        self.board_margin = float(board_margin_mm)  # A
        self.min_clearance = float(min_clearance_mm)  # B
        self.max_grid_positions = int(max_grid_positions)
        self.anchor = anchor
        self.criteria = criteria
        self.component_priority = component_priority
        self.distance_metric = distance_metric
        self.max_workers = int(max_workers)
        self.neighbor_offset = float(neighbor_offset_mm)
        self.min_center_dist = float(min_center_dist_mm)
        self.max_search_expansions = max(1, int(max_search_expansions))
        self.chain_lambda = int(chain_lambda)

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
        self._local_bbox_cache: list[
            tuple[float, float, float, float] | None
        ] = [None] * self.n
        self._bbox_computed: list[bool] = [False] * self.n

    # ------------------------------------------------------------------
    # Матрица связности R — формула (3.3.2)
    # ------------------------------------------------------------------
    def _build_connectivity_matrix(self) -> None:
        """Построить симметричную матрицу r_ij.

        rho_s — количество различных корпусов, подключённых к цепи s.
        P_s — количество падов цепи s на плате.
        omega_s = P_s / P_avg, где P_avg — среднее P_s по непустым цепям.
        Вклад цепи: ((rho_s + lambda) / rho_s) * omega_s.

        Один корпус учитывается в rho_s один раз, даже если к цепи подключено
        несколько его падов. При этом все эти пады учитываются в P_s.
        """
        net_to_modules: dict[int, list[int]] = {}
        net_pad_count: dict[int, int] = {}
        self.nets_per_module = []

        for module_idx, footprint in enumerate(self.modules):
            module_nets: set[int] = set()
            for pad in footprint.pads:
                net = int(pad.net)
                if net <= 0:
                    continue
                module_nets.add(net)
                net_pad_count[net] = net_pad_count.get(net, 0) + 1

            self.nets_per_module.append(module_nets)
            for net in module_nets:
                net_to_modules.setdefault(net, []).append(module_idx)

        pad_counts = [count for count in net_pad_count.values() if count > 0]
        average_pads = (
            float(sum(pad_counts)) / len(pad_counts)
            if pad_counts else 0.0
        )

        self.C.fill(0.0)
        for net, module_indices in net_to_modules.items():
            rho = len(module_indices)
            if rho < 2:
                continue

            omega = (
                net_pad_count[net] / average_pads
                if average_pads > 0 else 1.0
            )
            chain_weight = ((rho + self.chain_lambda) / rho) * omega

            for position, i in enumerate(module_indices):
                for j in module_indices[position + 1:]:
                    self.C[i, j] += chain_weight
                    self.C[j, i] += chain_weight

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
                radius = math.hypot(
                    drawing.x2 - drawing.x1,
                    drawing.y2 - drawing.y1,
                )
                xs.extend((drawing.x1 - radius, drawing.x1 + radius))
                ys.extend((drawing.y1 - radius, drawing.y1 + radius))
            else:
                xs.extend((drawing.x1, drawing.x2))
                ys.extend((drawing.y1, drawing.y2))
        return min(xs), min(ys), max(xs), max(ys)

    # ------------------------------------------------------------------
    # Геометрия корпусов
    # ------------------------------------------------------------------
    @staticmethod
    def _transform_point(
        x: float, y: float, angle_deg: float, mirrored: bool
    ) -> tuple[float, float]:
        if mirrored:
            y = -y
        if angle_deg:
            angle = math.radians(angle_deg)
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            return (
                x * cos_a + y * sin_a,
                -x * sin_a + y * cos_a,
            )
        return x, y

    def _module_angle_mirrored(self, idx: int) -> tuple[float, bool]:
        footprint = self.modules[idx]
        return footprint.po_rotation / 10.0, footprint.po_layer == 0

    def _local_graphic_points(self, idx: int) -> list[tuple[float, float]]:
        """Вернуть характерные точки графики корпуса без падов."""
        footprint = self.modules[idx]
        points: list[tuple[float, float]] = []

        for line in footprint.lines:
            points.extend(((line.x1, line.y1), (line.x2, line.y2)))

        for circle in footprint.circles:
            radius = circle.radius
            points.extend((
                (circle.cx - radius, circle.cy - radius),
                (circle.cx + radius, circle.cy + radius),
            ))

        for arc in footprint.arcs:
            radius = math.hypot(
                arc.sx - arc.cx,
                arc.sy - arc.cy,
            )
            points.extend((
                (arc.cx - radius, arc.cy - radius),
                (arc.cx + radius, arc.cy + radius),
            ))

        return points

    @staticmethod
    def _pad_half_extents(pad) -> tuple[float, float]:
        """Полуразмеры пада в координатах платы с учётом его угла.

        Угол pad_angle в legacy .brd хранится в десятых долях градуса.
        Для прямоугольных, овальных и трапециевидных падов используется
        ограничивающий прямоугольник повернутых width/height. Для круглого
        пада ориентация не влияет на габариты.
        """
        width = max(abs(float(pad.width)), 0.01)
        height = max(abs(float(pad.height)), 0.01)
        shape = str(pad.shape).upper()

        stroke_half = max(min(width, height) * 0.05, 0.02) / 2.0
        if shape == "C":
            radius = min(width, height) / 2.0
            extent = radius + stroke_half
            return extent, extent

        angle = math.radians(float(pad.pad_angle) / 10.0)
        cos_a = abs(math.cos(angle))
        sin_a = abs(math.sin(angle))
        half_x = (cos_a * width + sin_a * height) / 2.0
        half_y = (sin_a * width + cos_a * height) / 2.0
        return half_x + stroke_half, half_y + stroke_half

    def _local_bbox(
        self, idx: int
    ) -> tuple[float, float, float, float] | None:
        """Bounding Box корпуса относительно origin после transform.

        Центры падов проходят через трансформацию корпуса. Размеры падов
        учитываются отдельно в координатах платы, поскольку pad_angle
        является абсолютным углом пада на плате.
        """
        if self._bbox_computed[idx]:
            return self._local_bbox_cache[idx]

        footprint = self.modules[idx]
        module_angle, mirrored = self._module_angle_mirrored(idx)

        points = [
            self._transform_point(x, y, module_angle, mirrored)
            for x, y in self._local_graphic_points(idx)
        ]

        for pad in footprint.pads:
            center_x, center_y = self._transform_point(
                float(pad.x),
                float(pad.y),
                module_angle,
                mirrored,
            )
            half_x, half_y = self._pad_half_extents(pad)
            points.extend((
                (center_x - half_x, center_y - half_y),
                (center_x + half_x, center_y + half_y),
            ))

        if not points:
            self._bbox_computed[idx] = True
            self._local_bbox_cache[idx] = None
            return None

        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        self._bbox_computed[idx] = True
        self._local_bbox_cache[idx] = bbox
        return bbox

    def _get_component_bbox(
        self,
        idx: int,
        x: float,
        y: float,
        include_clearance: bool = True,
    ) -> tuple[float, float, float, float]:
        """Абсолютный bbox.

        При include_clearance=True расширяет bbox на B/2 с каждой стороны.
        Поэтому два таких bbox не пересекаются, только если фактический зазор
        между корпусами не меньше B.
        """
        half_clearance = self.min_clearance / 2.0 if include_clearance else 0.0
        bbox = self._local_bbox(idx)

        if bbox is None:
            extent = 5.0 + half_clearance
            return (
                x - extent,
                y - extent,
                x + extent,
                y + extent,
            )

        x1, y1, x2, y2 = bbox
        return (
            x + x1 - half_clearance,
            y + y1 - half_clearance,
            x + x2 + half_clearance,
            y + y2 + half_clearance,
        )

    def _get_component_size(self, idx: int) -> float:
        """Площадь Bounding Box без межкорпусного зазора B."""
        cached = self._sizes[idx]
        if cached is not None:
            return cached

        bbox = self._local_bbox(idx)
        if bbox is None:
            size = 0.0
        else:
            x1, y1, x2, y2 = bbox
            size = max(0.0, (x2 - x1) * (y2 - y1))

        self._sizes[idx] = size
        return size

    def _check_overlap(self, idx: int, x: float, y: float) -> bool:
        candidate = self._get_component_bbox(
            idx, x, y, include_clearance=True
        )
        for placed_idx in self.placed_indices:
            if placed_idx == idx:
                continue
            px, py = self.positions[placed_idx]
            other = self._get_component_bbox(
                placed_idx, px, py, include_clearance=True
            )
            if (
                candidate[2] <= other[0]
                or candidate[0] >= other[2]
                or candidate[3] <= other[1]
                or candidate[1] >= other[3]
            ):
                continue
            return True
        return False

    def _in_bounds(self, idx: int, x: float, y: float) -> bool:
        """Проверка физического bbox с отступом A, без расширения на B."""
        bbox = self._get_component_bbox(
            idx, x, y, include_clearance=False
        )
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        margin = self.board_margin
        eps = 0.01
        return (
            bbox[0] >= b_min_x + margin - eps
            and bbox[1] >= b_min_y + margin - eps
            and bbox[2] <= b_max_x - margin + eps
            and bbox[3] <= b_max_y - margin + eps
        )

    # ------------------------------------------------------------------
    # Начальный фиксированный корпус
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
        center_x = (b_min_x + b_max_x) / 2.0
        center_y = (b_min_y + b_max_y) / 2.0
        bbox = self._local_bbox(anchor_idx)

        if bbox is None:
            if self.anchor == "center":
                x, y = center_x, center_y
            else:
                x = b_min_x + self.board_margin + 5.0
                y = b_min_y + self.board_margin + 5.0
        else:
            x1, y1, x2, y2 = bbox
            if self.anchor == "center":
                x = center_x - (x1 + x2) / 2.0
                y = center_y - (y1 + y2) / 2.0
            else:
                x = b_min_x + self.board_margin - x1
                y = b_min_y + self.board_margin - y1

        if not self._in_bounds(anchor_idx, x, y):
            raise ValueError(
                "Фиксированный корпус не помещается в рабочую область платы "
                "с выбранным отступом A."
            )

        self._commit_placement(anchor_idx, x, y)

    # ------------------------------------------------------------------
    # Вычисление J — формулы (3.3.1), (3.3.3)–(3.3.6)
    # ------------------------------------------------------------------
    @staticmethod
    def _calculate_J_chunk(args) -> np.ndarray:
        (
            indices,
            connectivity_rows,
            placed_mask,
            unplaced_mask,
            criteria,
            nets_per_module,
            placed_nets,
        ) = args

        sum_placed = connectivity_rows[:, placed_mask].sum(axis=1)

        if criteria == "max_connection":
            if not placed_mask.any():
                return np.zeros(len(indices), dtype=np.float64)
            return connectivity_rows[:, placed_mask].max(axis=1)

        if criteria == "sum_placed":
            return sum_placed

        if criteria == "common_nets":
            return np.fromiter(
                (len(nets_per_module[i] & placed_nets) for i in indices),
                dtype=np.float64,
                count=len(indices),
            )

        if criteria == "absolute":
            sum_unplaced = connectivity_rows[:, unplaced_mask].sum(axis=1)
            return sum_placed - sum_unplaced

        if criteria == "relative":
            total = connectivity_rows.sum(axis=1)
            result = np.zeros(len(indices), dtype=np.float64)
            nonzero = total > 0
            result[nonzero] = sum_placed[nonzero] / total[nonzero]
            return result

        raise ValueError(f"Неизвестный критерий: {criteria}")

    def _calculate_J_all(self) -> np.ndarray:
        """Рассчитать J для всех неразмещённых корпусов.

        На достаточно крупных платах кандидаты разбиваются между потоками.
        """
        if not self.unplaced_indices:
            return np.array([], dtype=np.float64)

        placed_mask = np.zeros(self.n, dtype=bool)
        placed_mask[self.placed_indices] = True
        unplaced_mask = ~placed_mask

        indices = np.asarray(self.unplaced_indices, dtype=np.intp)
        connectivity_rows = self.C[indices]

        placed_nets: set[int] = set()
        if self.criteria == "common_nets":
            for idx in self.placed_indices:
                placed_nets.update(self.nets_per_module[idx])

        worker_count = min(self.max_workers, len(indices))
        chunks = np.array_split(np.arange(len(indices)), worker_count)
        tasks = [
            (
                indices[chunk],
                connectivity_rows[chunk],
                placed_mask,
                unplaced_mask,
                self.criteria,
                self.nets_per_module,
                placed_nets,
            )
            for chunk in chunks if len(chunk)
        ]

        if self.max_workers == 1 or len(indices) < 32 or len(tasks) < 2:
            parts = [self._calculate_J_chunk(task) for task in tasks]
        else:
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                parts = list(pool.map(self._calculate_J_chunk, tasks))

        return (
            np.concatenate(parts)
            if parts else np.array([], dtype=np.float64)
        )

    # ------------------------------------------------------------------
    # Генерация сетки и фильтрация R_k
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_grid_points(
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        step: float,
        max_positions: int,
    ) -> np.ndarray:
        """Сформировать равномерную сетку, не превышая лимит позиций.

        Если исходная сетка слишком велика, количество точек уменьшается
        пропорционально по осям. Крайние границы остаются включёнными.
        """
        if (
            step <= 0
            or max_positions < 1
            or x_max < x_min
            or y_max < y_min
        ):
            return np.empty((0, 2), dtype=np.float64)

        x_range = x_max - x_min
        y_range = y_max - y_min

        nx = (
            1 if math.isclose(x_range, 0.0)
            else max(2, int(math.ceil(x_range / step)) + 1)
        )
        ny = (
            1 if math.isclose(y_range, 0.0)
            else max(2, int(math.ceil(y_range / step)) + 1)
        )

        if nx * ny > max_positions:
            if max_positions == 1:
                nx = ny = 1
            elif max_positions < 4 and nx > 1 and ny > 1:
                # При лимите 2–3 невозможно сохранить все четыре угла.
                # Сохраняем охват по более длинной оси.
                if x_range >= y_range:
                    nx, ny = max_positions, 1
                else:
                    nx, ny = 1, max_positions
            else:
                scale = math.sqrt(max_positions / float(nx * ny))
                min_nx = 2 if x_range > 0 else 1
                min_ny = 2 if y_range > 0 else 1
                nx = max(min_nx, int(math.floor((nx - 1) * scale)) + 1)
                ny = max(min_ny, int(math.floor((ny - 1) * scale)) + 1)

                while nx * ny > max_positions:
                    if nx > min_nx and (
                        ny == min_ny
                        or (nx / max(min_nx, 1)) >= (ny / max(min_ny, 1))
                    ):
                        nx -= 1
                    elif ny > min_ny:
                        ny -= 1
                    elif nx > min_nx:
                        nx -= 1
                    else:
                        break

        x_coords = (
            np.linspace(x_min, x_max, nx, dtype=np.float64)
            if nx > 1 else np.array([(x_min + x_max) / 2.0])
        )
        y_coords = (
            np.linspace(y_min, y_max, ny, dtype=np.float64)
            if ny > 1 else np.array([(y_min + y_max) / 2.0])
        )
        xx, yy = np.meshgrid(x_coords, y_coords)
        return np.column_stack((xx.ravel(), yy.ravel()))

    @staticmethod
    def _filter_candidate_chunk(args) -> np.ndarray:
        (
            candidates,
            local_bbox,
            half_clearance,
            placed_boxes,
            placed_positions,
            min_center_dist,
        ) = args

        if not len(candidates) or not len(placed_boxes):
            return candidates

        x1, y1, x2, y2 = local_bbox
        candidate_boxes = np.column_stack((
            candidates[:, 0] + x1 - half_clearance,
            candidates[:, 1] + y1 - half_clearance,
            candidates[:, 0] + x2 + half_clearance,
            candidates[:, 1] + y2 + half_clearance,
        ))

        overlaps = (
            (candidate_boxes[:, None, 2] > placed_boxes[None, :, 0])
            & (candidate_boxes[:, None, 0] < placed_boxes[None, :, 2])
            & (candidate_boxes[:, None, 3] > placed_boxes[None, :, 1])
            & (candidate_boxes[:, None, 1] < placed_boxes[None, :, 3])
        )
        valid = ~overlaps.any(axis=1)

        # min_center_dist — физическое ограничение между центрами,
        # поэтому оно остаётся евклидовым независимо от целевой метрики d.
        if min_center_dist > 0:
            distances = np.linalg.norm(
                candidates[:, None, :] - placed_positions[None, :, :],
                axis=2,
            )
            valid &= (distances > min_center_dist).all(axis=1)

        return candidates[valid]

    def _filter_candidates(
        self, chosen_idx: int, candidates: np.ndarray
    ) -> np.ndarray:
        """Отфильтровать пересечения и минимальный зазор B в R_k.

        Кандидаты делятся на блоки, которые можно проверять параллельно.
        """
        if not len(candidates) or not self.placed_indices:
            return candidates

        local_bbox = self._local_bbox(chosen_idx) or (-5.0, -5.0, 5.0, 5.0)
        placed_boxes = np.asarray(
            [
                self._get_component_bbox(
                    i, *self.positions[i], include_clearance=True
                )
                for i in self.placed_indices
            ],
            dtype=np.float64,
        )
        placed_positions = np.asarray(
            [self.positions[i] for i in self.placed_indices],
            dtype=np.float64,
        )

        chunk_size = 512
        tasks = [
            (
                candidates[start:start + chunk_size],
                local_bbox,
                self.min_clearance / 2.0,
                placed_boxes,
                placed_positions,
                self.min_center_dist,
            )
            for start in range(0, len(candidates), chunk_size)
        ]

        if self.max_workers == 1 or len(tasks) < 2:
            parts = [self._filter_candidate_chunk(task) for task in tasks]
        else:
            worker_count = min(self.max_workers, len(tasks))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                parts = list(pool.map(self._filter_candidate_chunk, tasks))

        non_empty = [part for part in parts if len(part)]
        if not non_empty:
            return np.empty((0, 2), dtype=np.float64)
        return np.concatenate(non_empty, axis=0)

    def _generate_candidates(
        self,
        chosen_idx: int,
        neighbor_offset: float | None = None,
        check_overlap: bool = True,
        grid_step: float | None = None,
    ) -> np.ndarray:
        """Построить кандидатную сетку в области вокруг уже размещённых корпусов."""
        if not self.placed_indices:
            return np.empty((0, 2), dtype=np.float64)

        offset = (
            self.neighbor_offset
            if neighbor_offset is None else float(neighbor_offset)
        )
        step = self.grid_step if grid_step is None else float(grid_step)
        if step <= 0:
            return np.empty((0, 2), dtype=np.float64)

        placed_positions = np.asarray(
            [self.positions[i] for i in self.placed_indices],
            dtype=np.float64,
        )
        min_x, min_y = placed_positions.min(axis=0) - offset
        max_x, max_y = placed_positions.max(axis=0) + offset

        bbox = self._local_bbox(chosen_idx)
        if bbox is None:
            return np.empty((0, 2), dtype=np.float64)

        local_min_x, local_min_y, local_max_x, local_max_y = bbox
        board_min_x, board_min_y, board_max_x, board_max_y = (
            self._get_board_bounds()
        )

        # A — отступ корпуса от края рабочей области.
        # B применяется отдельно при фильтрации пересечений.
        min_x = max(
            min_x,
            board_min_x + self.board_margin - local_min_x,
        )
        min_y = max(
            min_y,
            board_min_y + self.board_margin - local_min_y,
        )
        max_x = min(
            max_x,
            board_max_x - self.board_margin - local_max_x,
        )
        max_y = min(
            max_y,
            board_max_y - self.board_margin - local_max_y,
        )

        if max_x < min_x or max_y < min_y:
            return np.empty((0, 2), dtype=np.float64)

        candidates = self._generate_grid_points(
            min_x,
            max_x,
            min_y,
            max_y,
            step,
            self.max_grid_positions,
        )

        if check_overlap:
            return self._filter_candidates(chosen_idx, candidates)
        return candidates

    # ------------------------------------------------------------------
    # Метрика d и оценка F
    # ------------------------------------------------------------------
    @staticmethod
    def _distance_values(
        left: np.ndarray,
        right: np.ndarray,
        metric: DistanceMetric,
    ) -> np.ndarray:
        """Вычислить d для евклидовой или манхэттенской метрики."""
        delta = np.asarray(left, dtype=np.float64) - np.asarray(
            right, dtype=np.float64
        )
        if metric == "manhattan":
            return np.abs(delta).sum(axis=-1)
        return np.linalg.norm(delta, axis=-1)

    @staticmethod
    def _calculate_F_chunk(args) -> np.ndarray:
        candidates, placed_positions, weights, distance_metric = args
        distances = AutoPlacer._distance_values(
            candidates[:, None, :],
            placed_positions[None, :, :],
            distance_metric,
        )
        return np.dot(distances, weights)

    def _find_best_position(
        self, chosen_idx: int, candidates: np.ndarray
    ) -> tuple[float, float] | None:
        if len(candidates) == 0:
            return None

        placed_positions = np.asarray(
            [self.positions[i] for i in self.placed_indices],
            dtype=np.float64,
        )
        weights = self.C[chosen_idx, self.placed_indices]

        if len(candidates) < 64 or self.max_workers == 1:
            values = self._calculate_F_chunk((
                candidates,
                placed_positions,
                weights,
                self.distance_metric,
            ))
        else:
            chunks = [
                part for part in np.array_split(
                    candidates,
                    min(self.max_workers, len(candidates)),
                )
                if len(part)
            ]
            tasks = [
                (part, placed_positions, weights, self.distance_metric)
                for part in chunks
            ]
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                values = np.concatenate(
                    list(pool.map(self._calculate_F_chunk, tasks))
                )

        if not len(values):
            return None

        best_value = values.min()
        best_index = int(np.flatnonzero(np.isclose(values, best_value))[0])
        return (
            float(candidates[best_index, 0]),
            float(candidates[best_index, 1]),
        )

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

    def _calculate_connection_count(self) -> int:
        """Количество пар корпусов с хотя бы одной общей цепью."""
        if self.n < 2:
            return 0
        return int(np.count_nonzero(np.triu(self.C > 0.0, k=1)))

    def _calculate_total_weighted_connection_length(self) -> float:
        """Сумма r_ij * d_ij; не является длиной физических дорожек."""
        if self.n < 2:
            return 0.0

        rows, cols = np.where(np.triu(self.C, k=1) > 0)
        if not len(rows):
            return 0.0

        positions = np.asarray(self.positions, dtype=np.float64)
        distances = self._distance_values(
            positions[rows],
            positions[cols],
            self.distance_metric,
        )
        return float(np.dot(distances, self.C[rows, cols]))

    def _calculate_total_wire_length(self) -> float:
        """Совместимый псевдоним старого имени метрики."""
        return self._calculate_total_weighted_connection_length()

    def _wire_contribution(self, idx: int) -> float:
        """Локальный вклад корпуса в общую взвешенную оценку."""
        if idx < 0 or idx >= self.n:
            return 0.0

        row = self.C[idx]
        mask = row > 0
        if not mask.any():
            return 0.0

        positions = np.asarray(self.positions, dtype=np.float64)
        distances = self._distance_values(
            positions[mask],
            positions[idx],
            self.distance_metric,
        )
        return float(np.dot(row[mask], distances))

    # ------------------------------------------------------------------
    # Локальная оптимизация
    # ------------------------------------------------------------------
    def _try_swap(self, i: int, j: int) -> bool:
        """Попытаться обменять позиции двух корпусов."""
        if i in self.fixed_indices or j in self.fixed_indices:
            return False

        old_i = self.positions[i]
        old_j = self.positions[j]
        old_value = self._wire_contribution(i) + self._wire_contribution(j)

        self.positions[i], self.positions[j] = old_j, old_i
        self.modules[i].po_x, self.modules[i].po_y = old_j
        self.modules[j].po_x, self.modules[j].po_y = old_i

        valid = self._in_bounds(i, *old_j) and self._in_bounds(j, *old_i)
        if valid:
            valid = (
                not self._check_overlap(i, *old_j)
                and not self._check_overlap(j, *old_i)
            )

        new_value = (
            self._wire_contribution(i) + self._wire_contribution(j)
            if valid else math.inf
        )

        if valid and new_value < old_value - 0.01:
            return True

        self.positions[i], self.positions[j] = old_i, old_j
        self.modules[i].po_x, self.modules[i].po_y = old_i
        self.modules[j].po_x, self.modules[j].po_y = old_j
        return False

    def _try_relocate(self, idx: int) -> bool:
        """Попытаться переместить корпус в позицию с меньшей оценкой F."""
        if idx in self.fixed_indices:
            return False

        saved_placed = list(self.placed_indices)
        self.placed_indices = [i for i in saved_placed if i != idx]
        if not self.placed_indices:
            self.placed_indices = saved_placed
            return False

        placed_positions = np.asarray(
            [self.positions[i] for i in self.placed_indices],
            dtype=np.float64,
        )
        weights = np.asarray(
            [self.C[idx, i] for i in self.placed_indices],
            dtype=np.float64,
        )
        old_position = np.asarray(self.positions[idx], dtype=np.float64)
        old_distances = self._distance_values(
            placed_positions,
            old_position,
            self.distance_metric,
        )
        best_value = float(np.dot(old_distances, weights))
        best_position: tuple[float, float] | None = None

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

                    distances = self._distance_values(
                        candidates[:, None, :],
                        placed_positions[None, :, :],
                        self.distance_metric,
                    )
                    values = np.dot(distances, weights)
                    candidate_index = int(np.argmin(values))
                    value = float(values[candidate_index])

                    if value < best_value - 0.01:
                        best_value = value
                        best_position = (
                            float(candidates[candidate_index, 0]),
                            float(candidates[candidate_index, 1]),
                        )
                        break

                if best_position is not None:
                    break
        finally:
            self.placed_indices = saved_placed

        if best_position is None:
            return False

        self.positions[idx] = best_position
        self.modules[idx].po_x, self.modules[idx].po_y = best_position
        return True

    def _find_any_free_position(
        self, idx: int
    ) -> tuple[float, float] | None:
        """Поиск свободной позиции по всей рабочей области платы."""
        bbox = self._local_bbox(idx)
        if bbox is None:
            return None

        local_min_x, local_min_y, local_max_x, local_max_y = bbox
        board_min_x, board_min_y, board_max_x, board_max_y = (
            self._get_board_bounds()
        )

        x_min = board_min_x + self.board_margin - local_min_x
        y_min = board_min_y + self.board_margin - local_min_y
        x_max = board_max_x - self.board_margin - local_max_x
        y_max = board_max_y - self.board_margin - local_max_y

        if x_max < x_min or y_max < y_min:
            return None

        fallback_step = max(0.1, min(self.grid_step / 4.0, 2.5))
        candidates = self._generate_grid_points(
            x_min,
            x_max,
            y_min,
            y_max,
            fallback_step,
            self.max_grid_positions,
        )
        valid = self._filter_candidates(idx, candidates)
        if not len(valid):
            return None

        return self._find_best_position(idx, valid)

    def _iterative_improve(self, max_passes: int = 5) -> int:
        """Локальная оптимизация swap/relocate до max_passes проходов."""
        if self.n < 2:
            return 0

        improvements = 0
        for _ in range(max_passes):
            changed = False
            placed = list(self.placed_indices)

            for i in range(len(placed)):
                for j in range(i + 1, len(placed)):
                    if self._try_swap(placed[i], placed[j]):
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
    # Основной цикл авторазмещения
    # ------------------------------------------------------------------
    def run(self) -> PlacementResult:
        started = time.perf_counter()

        if self.n == 0:
            return PlacementResult(
                execution_time_sec=0.0,
                component_count=0,
                placed_count=0,
                connection_count=0,
                total_weighted_connection_length_mm=0.0,
                placement_order=[],
            )

        self._build_connectivity_matrix()
        initial_length = self._calculate_total_weighted_connection_length()
        self._place_anchor()

        if self.n == 1:
            elapsed = time.perf_counter() - started
            return PlacementResult(
                execution_time_sec=elapsed,
                component_count=1,
                placed_count=1,
                connection_count=0,
                total_weighted_connection_length_mm=0.0,
                placement_order=list(self.placement_order),
                initial_weighted_connection_length_mm=initial_length,
                after_placement_weighted_connection_length_mm=0.0,
            )

        pending: list[int] = []

        # Основной последовательный этап: выбор компонента по J,
        # размещение — минимизацией F на допустимом множестве R_k.
        while self.unplaced_indices:
            j_values = self._calculate_J_all()
            if not len(j_values):
                break

            selected_local = int(np.argmax(j_values))
            chosen_idx = self.unplaced_indices[selected_local]
            best_position = None

            for grid_div in (1, 2, 4):
                for attempt in range(self.max_search_expansions):
                    candidates = self._generate_candidates(
                        chosen_idx,
                        neighbor_offset=self.neighbor_offset * (1 + attempt),
                        check_overlap=True,
                        grid_step=self.grid_step / grid_div,
                    )
                    if len(candidates):
                        best_position = self._find_best_position(
                            chosen_idx, candidates
                        )
                        if best_position is not None:
                            break
                if best_position is not None:
                    break

            if best_position is None:
                pending.append(chosen_idx)
                self.unplaced_indices.remove(chosen_idx)
            else:
                self._commit_placement(chosen_idx, *best_position)

        # Повторная попытка для отложенных компонентов с более мелкой сеткой.
        if pending:
            remaining: list[int] = []
            for idx in pending:
                best_position = None
                for grid_div in (4, 8):
                    for attempt in range(self.max_search_expansions):
                        candidates = self._generate_candidates(
                            idx,
                            neighbor_offset=self.neighbor_offset * (2 + attempt),
                            check_overlap=True,
                            grid_step=self.grid_step / grid_div,
                        )
                        if len(candidates):
                            best_position = self._find_best_position(idx, candidates)
                            if best_position is not None:
                                break
                    if best_position is not None:
                        break

                if best_position is None:
                    remaining.append(idx)
                else:
                    self._commit_placement(idx, *best_position)
            pending = remaining

        # Последняя попытка: вся рабочая область платы.
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

        # Замер после последовательного этапа, до локальной оптимизации.
        after_placement_length = (
            self._calculate_total_weighted_connection_length()
        )

        # ЛО выполняется отдельной фазой и использует ту же выбранную метрику d.
        self._iterative_improve(max_passes=5)
        after_local_optimization_length = (
            self._calculate_total_weighted_connection_length()
        )

        elapsed = time.perf_counter() - started
        return PlacementResult(
            execution_time_sec=elapsed,
            component_count=self.n,
            placed_count=len(self.placed_indices),
            connection_count=self._calculate_connection_count(),
            total_weighted_connection_length_mm=after_local_optimization_length,
            placement_order=list(self.placement_order),
            failed_to_place=list(self.failed_to_place),
            initial_weighted_connection_length_mm=initial_length,
            after_placement_weighted_connection_length_mm=after_placement_length,
        )


def auto_place_board(
    board: Board,
    grid_step_mm: float = 1.27,
    anchor: AnchorPosition = "center",
    criteria: SelectionCriteria = "absolute",
    component_priority: ComponentPriority = "max_connections",
    max_workers: int | None = None,
    neighbor_offset_mm: float = 20.0,
    padding_mm: float | None = None,
    min_center_dist_mm: float = 0.0,
    chain_lambda: int = 0,
    distance_metric: DistanceMetric = "euclidean",
    board_margin_mm: float = 5.0,
    min_clearance_mm: float = 1.0,
    max_grid_positions: int = 100_000,
) -> PlacementResult:
    """Публичная обёртка над AutoPlacer.run()."""
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
        distance_metric=distance_metric,
        board_margin_mm=board_margin_mm,
        min_clearance_mm=min_clearance_mm,
        max_grid_positions=max_grid_positions,
    ).run()
