"""Алгоритм авторазмещения корпусов на legacy-плате KiCad.

Поддерживает:
- критерии выбора следующего корпуса (3.3.1, 3.3.3, 3.3.4, 3.3.5, 3.3.6);
- вес цепи omega_s = P_s / P_avg в матрице r_ij;
- евклидову и манхэттенскую метрики расстояния d;
- независимые параметры A (отступ), B (зазор) и C (шаг сетки);
- ограничение числа кандидатных позиций;
- многопоточную обработку J, R_k и F;
- локальную оптимизацию посредством swap и relocate.

Метрика качества — суммарная длина воздушных связей: для каждой цепи
берутся абсолютные центры падов и вычисляется длина минимального
связывающего дерева (MST); сумма по всем цепям даёт длину связей
(ratsnest), а не длину медных дорожек. В отчёте (initial/after/total)
метрика всегда евклидова; целевая функция Fmin и локальная оптимизация
используют выбранную пользователем метрику d. Число связей равно
сумме (P_s - 1) по цепям (полю Links legacy-файла платы).
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

# Постоянные пулы потоков: создаются один раз и переиспользуются между
# этапами и запусками — создание пула на каждый вызов дороже вычислений.
_THREAD_POOL_CACHE: dict[int, ThreadPoolExecutor] = {}


@dataclass
class PlacementResult:
    """Итоги размещения и длины связей на разных этапах.

    Все длины — суммарная длина воздушных связей: сумма длин
    минимальных связывающих деревьев (MST) по падам каждой цепи,
    евклидова метрика. Это длина связей (ratsnest), а не длина
    медных дорожек.
    """

    execution_time_sec: float
    component_count: int
    placed_count: int
    connection_count: int               # воздушные связи (Links)
    total_connection_length_mm: float   # после локальной оптимизации
    placement_order: list[str]
    anchor_name: str = ""
    failed_to_place: list[str] = field(default_factory=list)
    connection_pairs_count: int = 0     # пар корпусов с общими цепями
    initial_connection_length_mm: float = 0.0
    after_placement_connection_length_mm: float = 0.0

    @property
    def after_local_optimization_connection_length_mm(self) -> float:
        """Длина связей после локальной оптимизации."""
        return self.total_connection_length_mm

    @property
    def placement_reduction_mm(self) -> float:
        """Сокращение длины связей от исходного состояния до размещения."""
        return (
            self.initial_connection_length_mm
            - self.after_placement_connection_length_mm
        )

    @property
    def placement_improvement_percent(self) -> float:
        initial = self.initial_connection_length_mm
        if abs(initial) < 1e-12:
            return 0.0
        return self.placement_reduction_mm / initial * 100.0

    @property
    def local_optimization_reduction_mm(self) -> float:
        """Сокращение длины связей за счёт локальной оптимизации."""
        return (
            self.after_placement_connection_length_mm
            - self.after_local_optimization_connection_length_mm
        )

    @property
    def local_optimization_improvement_percent(self) -> float:
        before = self.after_placement_connection_length_mm
        if abs(before) < 1e-12:
            return 0.0
        return self.local_optimization_reduction_mm / before * 100.0

    # ----------------------------------------------------------------
    # Устаревшие псевдонимы прежней версии API (значения те же).
    # ----------------------------------------------------------------
    @property
    def total_weighted_connection_length_mm(self) -> float:
        return self.total_connection_length_mm

    @property
    def initial_weighted_connection_length_mm(self) -> float:
        return self.initial_connection_length_mm

    @property
    def after_placement_weighted_connection_length_mm(self) -> float:
        return self.after_placement_connection_length_mm

    @property
    def after_local_optimization_weighted_connection_length_mm(self) -> float:
        return self.total_connection_length_mm

    @property
    def total_wire_length_mm(self) -> float:
        """Устаревший псевдоним; значение не является длиной дорожек."""
        return self.total_connection_length_mm


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
        relocate_window_mm: float | None = None,
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
        cpu_limit = max(1, os.cpu_count() or 1)
        if not 1 <= int(max_workers) <= cpu_limit:
            raise ValueError(
                "max_workers должен быть в диапазоне от 1 до "
                f"{cpu_limit} (число логических ядер машины)"
            )
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
        # Окно relocate в ЛО вокруг текущей позиции корпуса: число
        # кандидатных позиций падает в разы, качество практически
        # не меняется. None — поиск по всей области размещённых корпусов
        # (прежнее поведение).
        self.relocate_window_mm: float | None = (
            None if relocate_window_mm is None
            else float(relocate_window_mm)
        )

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

        # Геометрия цепей для метрики длины связей (заполняется в run()).
        self.net_pad_offsets: dict[
            int, list[tuple[int, tuple[float, float]]]
        ] = {}
        self.airwire_count: int = 0

    # ------------------------------------------------------------------
    # Геометрия цепей и метрика длины воздушных связей (MST по падам)
    # ------------------------------------------------------------------
    def _build_net_geometry(self) -> None:
        """Собрать смещения падов цепей относительно origins корпусов.

        net_pad_offsets[net] — список (индекс корпуса, (dx, dy)) для
        каждого пада цепи; смещение повёрнуто и зеркально вместе с
        корпусом. Абсолютная позиция пада = origin корпуса + смещение,
        поэтому при перемещении корпуса достаточно сдвинуть его пады
        на новую позицию origin.
        """
        self.net_pad_offsets = {}
        for module_idx, footprint in enumerate(self.modules):
            angle, mirrored = self._module_angle_mirrored(module_idx)
            for pad in footprint.pads:
                net = int(pad.net)
                if net <= 0:
                    continue
                dx, dy = self._transform_point(
                    float(pad.x), float(pad.y), angle, mirrored)
                self.net_pad_offsets.setdefault(net, []).append(
                    (module_idx, (dx, dy))
                )
        self.airwire_count = self._calculate_connection_count()

    def _net_pad_points(
        self, net: int, exclude_idx: int | None = None
    ) -> list[tuple[float, float]]:
        """Абсолютные точки падов цепи (все корпуса, кроме exclude_idx)."""
        points: list[tuple[float, float]] = []
        for module_idx, (dx, dy) in self.net_pad_offsets.get(net, ()):
            if module_idx == exclude_idx:
                continue
            ox, oy = self.positions[module_idx]
            points.append((ox + dx, oy + dy))
        return points

    def _module_net_offsets(self, idx: int, net: int) -> np.ndarray:
        """Смещения падов корпуса idx на цепи net (массив (k, 2))."""
        offsets = [
            off for module_idx, off in self.net_pad_offsets.get(net, ())
            if module_idx == idx
        ]
        return np.asarray(offsets, dtype=np.float64).reshape(-1, 2)

    @staticmethod
    def _mst_length(
        points, metric: DistanceMetric = "euclidean"
    ) -> float:
        """Длина минимального связывающего дерева точек (Прим, O(k^2))."""
        k = len(points)
        if k < 2:
            return 0.0
        pts = [(float(p[0]), float(p[1])) for p in points]
        if metric == "manhattan":
            def dist(a, b):
                return abs(a[0] - b[0]) + abs(a[1] - b[1])
        else:
            def dist(a, b):
                return math.hypot(a[0] - b[0], a[1] - b[1])

        in_tree = [False] * k
        in_tree[0] = True
        min_d = [dist(pts[0], pts[j]) for j in range(k)]
        total = 0.0
        for _ in range(k - 1):
            best, best_d = -1, math.inf
            for j in range(k):
                if not in_tree[j] and min_d[j] < best_d:
                    best, best_d = j, min_d[j]
            total += best_d
            in_tree[best] = True
            for j in range(k):
                if not in_tree[j]:
                    d = dist(pts[best], pts[j])
                    if d < min_d[j]:
                        min_d[j] = d
        return total

    @staticmethod
    def _mst_length_batch(
        points_batch: np.ndarray, metric: DistanceMetric = "euclidean"
    ) -> np.ndarray:
        """MST-длина для батча наборов точек (n, k, 2) -> (n,).

        Векторизованный алгоритм Прима: шаги дерева выполняются сразу
        для всех наборов батча. Для k = 2 и k = 3 используются точные
        быстрые формулы без итераций (MST двух точек — одно расстояние;
        MST трёх точек — сумма двух меньших из трёх рёбер).
        """
        batch = np.asarray(points_batch, dtype=np.float64)
        n_sets, k, _ = batch.shape
        if k < 2:
            return np.zeros(n_sets, dtype=np.float64)

        def pair(i: int, j: int) -> np.ndarray:
            delta = batch[:, i, :] - batch[:, j, :]
            if metric == "manhattan":
                return np.abs(delta).sum(axis=-1)
            return np.hypot(delta[:, 0], delta[:, 1])

        if k == 2:
            return pair(0, 1)
        if k == 3:
            d01, d02, d12 = pair(0, 1), pair(0, 2), pair(1, 2)
            return d01 + d02 + d12 - np.maximum(
                np.maximum(d01, d02), d12
            )

        delta = batch[:, :, None, :] - batch[:, None, :, :]
        if metric == "manhattan":
            dist = np.abs(delta).sum(axis=-1)
        else:
            dist = np.hypot(delta[..., 0], delta[..., 1])

        rows = np.arange(n_sets)
        visited = np.zeros((n_sets, k), dtype=bool)
        visited[:, 0] = True
        min_d = dist[:, 0, :].copy()
        total = np.zeros(n_sets, dtype=np.float64)
        for _ in range(k - 1):
            masked = np.where(visited, np.inf, min_d)
            nearest = np.argmin(masked, axis=1)
            total += masked[rows, nearest]
            visited[rows, nearest] = True
            min_d = np.minimum(min_d, dist[rows, nearest, :])
        return total

    def _nets_mst_sum(
        self, nets, metric: DistanceMetric | None = None
    ) -> float:
        """Суммарная длина MST указанных цепей при текущих позициях."""
        metric = metric or self.distance_metric
        total = 0.0
        for net in nets:
            points = self._net_pad_points(net)
            if len(points) >= 2:
                total += self._mst_length(points, metric)
        return total

    def _candidate_net_mst_values(
        self,
        idx: int,
        candidates: np.ndarray,
        metric: DistanceMetric | None = None,
    ) -> np.ndarray:
        """Значения F для кандидатных позиций корпуса idx.

        Для каждой цепи корпуса к падам остальных корпусов добавляются
        пады кандидата в проверяемой позиции; длина MST такой цепи
        суммируется по всем цепям корпуса. Меньшее значение — меньшая
        длина воздушных связей платы. Цепи без падов на других корпусах
        пропускаются: их MST не зависит от позиции кандидата.
        """
        candidates = np.asarray(candidates, dtype=np.float64)
        values = np.zeros(len(candidates), dtype=np.float64)
        if not len(candidates):
            return values
        metric = metric or self.distance_metric

        for net in self.nets_per_module[idx]:
            offsets = self._module_net_offsets(idx, net)
            fixed = np.asarray(
                self._net_pad_points(net, exclude_idx=idx),
                dtype=np.float64,
            ).reshape(-1, 2)
            m = len(fixed)
            k = m + len(offsets)
            if k < 2 or m == 0:
                continue

            # Ограничение памяти: матрица расстояний (chunk, k, k).
            chunk_size = max(1, int(1_000_000 // max(1, k * k)))
            for start in range(0, len(candidates), chunk_size):
                part = candidates[start:start + chunk_size]
                cand_pts = part[:, None, :] + offsets[None, :, :]
                pts = np.concatenate(
                    (
                        np.broadcast_to(fixed, (len(part), m, 2)),
                        cand_pts,
                    ),
                    axis=1,
                )
                values[start:start + chunk_size] += self._mst_length_batch(
                    pts, metric
                )
        return values

    def _calculate_total_connection_length(
        self, metric: DistanceMetric | None = None
    ) -> float:
        """Суммарная длина воздушных связей: сумма MST всех цепей по падам.

        Для каждой цепи берутся абсолютные центры падов всех корпусов и
        считается длина минимального связывающего дерева. Отчётная метрика
        всегда евклидова (metric=None); это длина связей (ratsnest),
        а не длина медных дорожек.
        """
        if self.n < 2:
            return 0.0
        metric = metric or "euclidean"
        total = 0.0
        for net in self.net_pad_offsets:
            points = self._net_pad_points(net)
            if len(points) >= 2:
                total += self._mst_length(points, metric)
        return total

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
            parts = list(self._shared_pool().map(self._calculate_J_chunk, tasks))

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
            parts = list(
                self._shared_pool().map(self._filter_candidate_chunk, tasks)
            )

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

    def _find_best_position(
        self, chosen_idx: int, candidates: np.ndarray
    ) -> tuple[float, float] | None:
        """Выбрать позицию с минимальной длиной MST цепей корпуса."""
        if len(candidates) == 0:
            return None

        values = self._evaluate_candidates_values(chosen_idx, candidates)

        if not len(values):
            return None

        best_value = values.min()
        best_index = int(np.flatnonzero(np.isclose(values, best_value))[0])
        return (
            float(candidates[best_index, 0]),
            float(candidates[best_index, 1]),
        )

    def _evaluate_candidates_values(
        self, idx: int, candidates: np.ndarray
    ) -> np.ndarray:
        """Значения F для всех кандидатов, при возможности параллельно.

        Пул потоков создаётся один раз на всё размещение и используется
        и в основном этапе, и в локальной оптимизации: создание нового
        пула на каждый вызов обходится дороже самих вычислений.
        """
        candidates = np.asarray(candidates, dtype=np.float64)
        if not len(candidates):
            return np.empty(0, dtype=np.float64)

        if len(candidates) < 256 or self.max_workers == 1:
            return self._candidate_net_mst_values(idx, candidates)

        pool = self._shared_pool()
        chunks = [
            part for part in np.array_split(
                candidates,
                min(self.max_workers, len(candidates)),
            )
            if len(part)
        ]
        tasks = [(idx, part, self.distance_metric) for part in chunks]
        parts = list(pool.map(self._candidate_net_mst_values_task, tasks))
        return (
            np.concatenate(parts)
            if parts else np.empty(0, dtype=np.float64)
        )

    def _shared_pool(self) -> ThreadPoolExecutor:
        """Постоянный пул потоков, общий для всех этапов размещения."""
        pool = _THREAD_POOL_CACHE.get(self.max_workers)
        if pool is None:
            pool = ThreadPoolExecutor(max_workers=self.max_workers)
            _THREAD_POOL_CACHE[self.max_workers] = pool
        return pool

    def _candidate_net_mst_values_task(self, args) -> np.ndarray:
        """Обёртка для параллельного вычисления F по блоку кандидатов."""
        idx, candidates, metric = args
        return self._candidate_net_mst_values(idx, candidates, metric)

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
        """Количество воздушных связей: сумма (P_s - 1) по цепям с P_s >= 2.

        Равняется числу звеньев ratsnest (полю Links в legacy-файле платы)
        и числу рёбер суммы MST по всем цепям.
        """
        return int(sum(
            len(pads) - 1
            for pads in self.net_pad_offsets.values()
            if len(pads) >= 2
        ))

    def _calculate_connection_pairs_count(self) -> int:
        """Количество пар корпусов с хотя бы одной общей цепью."""
        if self.n < 2:
            return 0
        return int(np.count_nonzero(np.triu(self.C > 0.0, k=1)))

    def _calculate_total_weighted_origin_distance(self) -> float:
        """Старая взвешенная оценка Σ r_ij * d_ij между origins корпусов.

        Оставлена для диагностики; в результатах размещения и в целевой
        функции не используется (не является длиной связей платы).
        """
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

    # ------------------------------------------------------------------
    # Локальная оптимизация
    # ------------------------------------------------------------------
    def _try_swap(self, i: int, j: int) -> bool:
        """Попытаться обменять позиции двух корпусов.

        Сравнивается суммарная длина MST цепей, затронутых обоими
        корпусами, до и после обмена.
        """
        if i in self.fixed_indices or j in self.fixed_indices:
            return False

        affected = set(self.nets_per_module[i]) | set(self.nets_per_module[j])
        if not affected:
            return False

        old_value = self._nets_mst_sum(affected)

        old_i = self.positions[i]
        old_j = self.positions[j]
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
            self._nets_mst_sum(affected) if valid else math.inf
        )

        if valid and new_value < old_value - 0.01:
            return True

        self.positions[i], self.positions[j] = old_i, old_j
        self.modules[i].po_x, self.modules[i].po_y = old_i
        self.modules[j].po_x, self.modules[j].po_y = old_j
        return False

    def _try_relocate(self, idx: int) -> bool:
        """Попытаться переместить корпус в позицию с меньшей длиной MST.

        Кандидаты оцениваются суммой длин MST цепей корпуса; позиции
        фильтруются на пересечения с остальными корпусами.
        """
        if idx in self.fixed_indices:
            return False
        if not self.nets_per_module[idx]:
            return False

        old_value = self._nets_mst_sum(self.nets_per_module[idx])
        best_value = old_value
        best_position: tuple[float, float] | None = None

        saved_placed = list(self.placed_indices)
        self.placed_indices = [i for i in saved_placed if i != idx]
        try:
            x0, y0 = self.positions[idx]
            for grid_div in (1, 2):
                for attempt in range(2):
                    candidates = self._generate_candidates(
                        idx,
                        neighbor_offset=self.neighbor_offset * (1 + attempt),
                        check_overlap=True,
                        grid_step=self.grid_step / grid_div,
                    )
                    if len(candidates) and self.relocate_window_mm is not None:
                        half = self.relocate_window_mm * (1 + attempt)
                        mask = (
                            (np.abs(candidates[:, 0] - x0) <= half)
                            & (np.abs(candidates[:, 1] - y0) <= half)
                        )
                        candidates = candidates[mask]
                    if not len(candidates):
                        continue

                    values = self._evaluate_candidates_values(idx, candidates)
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
                total_connection_length_mm=0.0,
                placement_order=[],
            )

        self._build_connectivity_matrix()
        self._build_net_geometry()
        initial_length = self._calculate_total_connection_length()
        self._place_anchor()

        anchor_name = ""
        if self.anchor_index is not None:
            anchor_name = self.modules[self.anchor_index].name

        if self.n == 1:
            elapsed = time.perf_counter() - started
            return PlacementResult(
                execution_time_sec=elapsed,
                component_count=1,
                placed_count=1,
                connection_count=0,
                total_connection_length_mm=0.0,
                placement_order=list(self.placement_order),
                anchor_name=anchor_name,
                initial_connection_length_mm=initial_length,
                after_placement_connection_length_mm=0.0,
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
        after_placement_length = self._calculate_total_connection_length()

        # ЛО выполняется отдельной фазой и использует ту же выбранную
        # метрику d внутри MST каждой цепи.
        self._iterative_improve(max_passes=5)
        after_local_optimization_length = (
            self._calculate_total_connection_length()
        )

        elapsed = time.perf_counter() - started
        return PlacementResult(
            execution_time_sec=elapsed,
            component_count=self.n,
            placed_count=len(self.placed_indices),
            connection_count=self.airwire_count,
            connection_pairs_count=self._calculate_connection_pairs_count(),
            total_connection_length_mm=after_local_optimization_length,
            placement_order=list(self.placement_order),
            anchor_name=anchor_name,
            failed_to_place=list(self.failed_to_place),
            initial_connection_length_mm=initial_length,
            after_placement_connection_length_mm=after_placement_length,
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
    relocate_window_mm: float | None = None,
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
        relocate_window_mm=relocate_window_mm,
    ).run()
