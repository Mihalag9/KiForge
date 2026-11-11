"""
Модуль авторазмещения компонентов на плате (KiForge).

Реализует последовательный алгоритм размещения по связности (Раздел 3.3)
с учётом поворота и зеркалирования компонентов.

Критерии выбора очередного модуля (формулы из учебных материалов):
  - "absolute"       — J = Σ c_ij(placed) − Σ c_ij(unplaced)   (3.3.1 / 3.3.5 Важенин)
  - "relative"       — J = Σ placed / Σ total                    (3.3.6)
  - "max_connection" — J = max c_ij(placed)                      (3.3.1 с фото стр.142)
  - "sum_placed"     — J = Σ c_ij(placed)                        (3.3.3 с фото стр.143)

Критерий выбора позиции:
  - F = Σ c_ij · d(p_i, p_j) → min                               (3.3.2)

Порядок шагов (по учебнику, пункты 1–8):
  1. Размещение директивного (anchor) модуля.
  2. Формирование позиций, соседних с занятыми.
  3. Расчёт J для всех неразмещённых модулей.
  4. Выбор модуля с макс. J.
  5. Расчёт F для соседних позиций (для выбранного модуля).
  6. Выбор позиции с мин. F.
  7. Размещение.
  8. Если не все размещены — к шагу 2.

Многопоточность: расчёт F по чанкам кандидатов через ThreadPoolExecutor
(numpy освобождает GIL). Для малых наборов кандидатов потоков нет —
overhead превышает выгоду.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Literal

import numpy as np

from .board import Board
from .footprint import Footprint

SelectionCriteria = Literal[
    "absolute",        # Важенин: Σ placed − Σ unplaced   (3.3.1 / 3.3.5)
    "relative",        # Σ placed / Σ total                (3.3.6)
    "max_connection",  # max c_ij(placed)                  (3.3.1 с фото)
    "sum_placed",      # Σ c_ij(placed)                    (3.3.3 с фото)
]
AnchorPosition = Literal["center", "top_left"]
ComponentPriority = Literal["max_connections", "max_size"]


@dataclass
class PlacementResult:
    execution_time_sec: float
    component_count: int
    placed_count: int
    total_wire_length_mm: float
    placement_order: List[str]
    failed_to_place: List[str] = field(default_factory=list)


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
        padding_mm: float = 3.0,
        min_center_dist_mm: float = 0.0,
        max_search_expansions: int = 3,
    ):
        self.board = board
        self.grid_step = grid_step_mm
        self.anchor = anchor
        self.criteria = criteria
        self.component_priority = component_priority
        self.max_workers = max_workers
        self.neighbor_offset = neighbor_offset_mm
        self.padding = padding_mm
        self.min_center_dist = min_center_dist_mm
        self.max_search_expansions = max_search_expansions

        self.modules = board.modules
        self.n = len(self.modules)

        self.C: np.ndarray = np.zeros((self.n, self.n), dtype=np.float32)
        self.positions: List[Tuple[float, float]] = [
            (float(fp.po_x), float(fp.po_y)) for fp in self.modules
        ]

        self.placed_indices: List[int] = []
        self.unplaced_indices: List[int] = list(range(self.n))
        self.placement_order: List[str] = []
        self.failed_to_place: List[str] = []

        # Кэш размеров — не пересчитываем в цикле
        self._sizes: List[float] = [0.0] * self.n

    # ------------------------------------------------------------------
    # Матрица связности
    # ------------------------------------------------------------------
    def _build_connectivity_matrix(self) -> None:
        """C[i,j] = число общих цепей между модулями i и j."""
        nets_per_module: List[set] = []
        for fp in self.modules:
            nets = set(p.net for p in fp.pads if p.net > 0)
            nets_per_module.append(nets)

        for i in range(self.n):
            for j in range(i + 1, self.n):
                common = len(nets_per_module[i] & nets_per_module[j])
                self.C[i, j] = common
                self.C[j, i] = common

    # ------------------------------------------------------------------
    # Границы платы
    # ------------------------------------------------------------------
    def _get_board_bounds(self) -> Tuple[float, float, float, float]:
        """BBox платы по Edge.Cuts. Учитывает линии, окружности и дуги.

        Для окружности (shape==1): (x1,y1) — центр, (x2,y2) — точка на
        окружности, радиус = расстояние между ними.
        Для дуги (shape==2): (x1,y1) — центр, (x2,y2) — стартовая точка.
        BBox берётся по полной окружности (консервативно для дуги).
        """
        edge = [d for d in self.board.drawings if d.layer == "Edge.Cuts"]
        if not edge:
            # Фолбэк: по позициям модулей, иначе 100×100 мм
            if self.modules:
                xs = [float(fp.po_x) for fp in self.modules]
                ys = [float(fp.po_y) for fp in self.modules]
                return min(xs), min(ys), max(xs), max(ys)
            return 0.0, 0.0, 100.0, 100.0

        xs: List[float] = []
        ys: List[float] = []
        for d in edge:
            if d.shape == 1:
                # Окружность
                r = math.hypot(d.x2 - d.x1, d.y2 - d.y1)
                xs.extend([d.x1 - r, d.x1 + r])
                ys.extend([d.y1 - r, d.y1 + r])
            elif d.shape == 2:
                # Дуга — bbox по полной окружности (консервативно)
                r = math.hypot(d.x2 - d.x1, d.y2 - d.y1)
                xs.extend([d.x1 - r, d.x1 + r])
                ys.extend([d.y1 - r, d.y1 + r])
            else:
                # Линия (shape == 0)
                xs.extend([d.x1, d.x2])
                ys.extend([d.y1, d.y2])
        return min(xs), min(ys), max(xs), max(ys)

    # ------------------------------------------------------------------
    # Трансформация и bbox компонента
    # ------------------------------------------------------------------
    @staticmethod
    def _transform_point(
        lx: float, ly: float, angle_deg: float, mirrored: bool
    ) -> Tuple[float, float]:
        """Локальную точку → в координаты после зеркалирования и поворота.

        Зеркалирование (модуль на B.Cu): ly = -ly (относительно оси X).
        Поворот на angle_deg (против часовой, как в KiCad).
        """
        if mirrored:
            ly = -ly
        if angle_deg:
            rad = math.radians(angle_deg)
            c, s = math.cos(rad), math.sin(rad)
            return lx * c + ly * s, -lx * s + ly * c
        return lx, ly

    def _module_angle_mirrored(self, idx: int) -> Tuple[float, bool]:
        """(угол в градусах, зеркалён ли) для модуля idx.

        В нумерации платы (BOARD_LAYER_NUM_TO_NAME в io_kicad/layers.py):
          0  = B.Cu (Back)  → зеркалится
          15 = F.Cu (Front) → не зеркалится
        po_rotation хранится в 0.1° (как в файле), делим на 10.
        """
        fp = self.modules[idx]
        angle = fp.po_rotation / 10.0
        mirrored = fp.po_layer == 0
        return angle, mirrored

    def _local_points(self, idx: int) -> List[Tuple[float, float]]:
        """Все локальные точки модуля: пады + silk-контур (линии,
        окружности, дуги). Нужно для корректного bbox компонента."""
        fp = self.modules[idx]
        pts: List[Tuple[float, float]] = []
        for p in fp.pads:
            pts.append((p.x, p.y))
        for ln in fp.lines:
            pts.append((ln.x1, ln.y1))
            pts.append((ln.x2, ln.y2))
        for c in fp.circles:
            r = c.radius
            pts.append((c.cx + r, c.cy + r))
            pts.append((c.cx - r, c.cy - r))
        for a in fp.arcs:
            r = math.hypot(a.sx - a.cx, a.sy - a.cy)
            pts.append((a.cx + r, a.cy + r))
            pts.append((a.cx - r, a.cy - r))
        return pts

    def _get_component_bbox(
        self, idx: int, x: float, y: float
    ) -> Tuple[float, float, float, float]:
        """BBox компонента idx при позиции (x, y) в абсолютных координатах.

        Учитывает пады, silk-контур (линии, окружности, дуги), поворот и
        зеркалирование модуля. Добавляет padding со всех сторон.
        """
        angle, mirrored = self._module_angle_mirrored(idx)
        local = self._local_points(idx)
        if not local:
            return x - 5.0, y - 5.0, x + 5.0, y + 5.0

        abs_pts = [
            (x + tx, y + ty)
            for tx, ty in (
                self._transform_point(lx, ly, angle, mirrored) for lx, ly in local
            )
        ]
        xs = [p[0] for p in abs_pts]
        ys = [p[1] for p in abs_pts]
        return (
            min(xs) - self.padding,
            min(ys) - self.padding,
            max(xs) + self.padding,
            max(ys) + self.padding,
        )

    def _get_component_size(self, idx: int) -> float:
        """Площадь bbox компонента в его текущей ориентации (без padding).
        Результат кэшируется — ориентация не меняется во время размещения.
        """
        if self._sizes[idx] > 0.0:
            return self._sizes[idx]
        local = self._local_points(idx)
        if not local:
            self._sizes[idx] = 0.0
            return 0.0
        angle, mirrored = self._module_angle_mirrored(idx)
        tr = [self._transform_point(lx, ly, angle, mirrored) for lx, ly in local]
        xs = [p[0] for p in tr]
        ys = [p[1] for p in tr]
        size = (max(xs) - min(xs)) * (max(ys) - min(ys))
        self._sizes[idx] = size
        return size

    def _check_overlap(self, idx: int, x: float, y: float) -> bool:
        """Пересекает ли bbox компонента idx в (x, y) любой размещённый."""
        new_bbox = self._get_component_bbox(idx, x, y)
        for pi in self.placed_indices:
            px, py = self.positions[pi]
            pb = self._get_component_bbox(pi, px, py)
            # AABB-пересечение
            if new_bbox[2] <= pb[0] or new_bbox[0] >= pb[2]:
                continue
            if new_bbox[3] <= pb[1] or new_bbox[1] >= pb[3]:
                continue
            return True
        return False

    # ------------------------------------------------------------------
    # Шаг 1: anchor
    # ------------------------------------------------------------------
    def _place_anchor(self) -> None:
        """Размещение директивного модуля (anchor).

        Anchor выбирается:
          - max_connections — модуль с наибольшей суммой связей
          - max_size        — модуль с наибольшей площадью bbox

        Позиция:
          - center   — центр платы
          - top_left — верхний левый угол, с отступом так, чтобы bbox
                       модуля полностью помещался внутри Edge.Cuts
        """
        if self.n == 0:
            return

        if self.component_priority == "max_connections":
            row_sums = self.C.sum(axis=1)
            anchor_idx = int(np.argmax(row_sums))
        else:
            sizes = [self._get_component_size(i) for i in range(self.n)]
            anchor_idx = sizes.index(max(sizes))

        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        board_cx = (b_min_x + b_max_x) / 2.0
        board_cy = (b_min_y + b_max_y) / 2.0

        lbbox = self._local_bbox(anchor_idx)
        if lbbox is None:
            # Нет pads и silk — ставим origin в центр / угол
            if self.anchor == "center":
                self._commit_placement(anchor_idx, board_cx, board_cy)
            else:
                self._commit_placement(anchor_idx, b_min_x + self.padding, b_min_y + self.padding)
            return

        l_min_x, l_min_y, l_max_x, l_max_y = lbbox
        if self.anchor == "center":
            # Центрируем BBOX модуля (не origin) в центре платы.
            bbox_cx = (l_min_x + l_max_x) / 2.0
            bbox_cy = (l_min_y + l_max_y) / 2.0
            x = board_cx - bbox_cx
            y = board_cy - bbox_cy
        else:
            # top_left: левый верх BBOX модуля = левый верх платы + padding.
            x = b_min_x - l_min_x + self.padding
            y = b_min_y - l_min_y + self.padding

        self._commit_placement(anchor_idx, x, y)

    # ------------------------------------------------------------------
    # Шаги 3–4: выбор модуля (критерий J, векторизован)
    # ------------------------------------------------------------------
    def _calculate_J_all(self) -> np.ndarray:
        """J для всех неразмещённых модулей сразу (векторизованно).

        Возвращает массив длиной len(unplaced_indices). При равных J
        argmax возвращает первый — что соответствует правилу учебника
        «с меньшим порядковым номером».
        """
        if not self.unplaced_indices:
            return np.array([])

        placed_mask = np.zeros(self.n, dtype=bool)
        placed_mask[self.placed_indices] = True
        unplaced_mask = ~placed_mask

        up = np.array(self.unplaced_indices, dtype=np.intp)
        c_sub = self.C[up]                         # (k, n)
        sum_placed = c_sub[:, placed_mask].sum(axis=1)

        if self.criteria == "absolute":
            sum_unplaced = c_sub[:, unplaced_mask].sum(axis=1)
            return sum_placed - sum_unplaced
        elif self.criteria == "relative":
            total = c_sub.sum(axis=1)
            out = np.zeros_like(sum_placed, dtype=np.float64)
            nz = total > 0
            out[nz] = sum_placed[nz] / total[nz]
            return out
        elif self.criteria == "max_connection":
            if not self.placed_indices:
                return np.zeros(len(up), dtype=np.float64)
            return c_sub[:, placed_mask].max(axis=1)
        elif self.criteria == "sum_placed":
            return sum_placed
        return np.zeros(len(up), dtype=np.float64)

    # ------------------------------------------------------------------
    # Шаги 2, 5: кандидаты для выбранного модуля
    # ------------------------------------------------------------------
    def _local_bbox(self, idx: int) -> Tuple[float, float, float, float] | None:
        """Локальный bbox модуля (min_x, min_y, max_x, max_y) после
        поворота и зеркалирования, без padding. Координаты — относительно
        origin модуля (точки привязки po_x/po_y).

        Важно: origin модуля НЕ совпадает с центром bbox — у DIP-корпусов,
        например, pads асимметричны относительно origin. Поэтому для
        отсечения сетки нужно использовать именно min/max локальных точек,
        а не «половину размера».
        """
        local = self._local_points(idx)
        if not local:
            return None
        angle, mirrored = self._module_angle_mirrored(idx)
        tr = [self._transform_point(lx, ly, angle, mirrored) for lx, ly in local]
        xs = [p[0] for p in tr]
        ys = [p[1] for p in tr]
        return min(xs), min(ys), max(xs), max(ys)

    def _generate_candidates(
        self,
        chosen_idx: int,
        neighbor_offset: float | None = None,
        check_overlap: bool = True,
    ) -> np.ndarray:
        """Сетка позиций вокруг размещённых, отфильтрованная так, чтобы
        компонент chosen_idx в этой позиции не выходил за границы платы.

        При check_overlap=True (по умолчанию) также отсеваются позиции,
        где bbox модуля пересекает любой размещённый модуль.

        neighbor_offset задаёт радиус расширения области поиска вокруг
        кластера размещённых модулей (по умолчанию self.neighbor_offset).
        """
        if not self.placed_indices:
            return np.array([])

        off = neighbor_offset if neighbor_offset is not None else self.neighbor_offset
        placed_pos = np.array([self.positions[i] for i in self.placed_indices])

        min_x, min_y = placed_pos.min(axis=0) - off
        max_x, max_y = placed_pos.max(axis=0) + off

        # Отсекаем сетку так, чтобы ВЕСЬ bbox модуля (с padding) вошёл в плату.
        lbbox = self._local_bbox(chosen_idx)
        if lbbox is None:
            return np.array([])
        l_min_x, l_min_y, l_max_x, l_max_y = lbbox
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        min_x = max(min_x, b_min_x - l_min_x + self.padding)
        min_y = max(min_y, b_min_y - l_min_y + self.padding)
        max_x = min(max_x, b_max_x - l_max_x - self.padding)
        max_y = min(max_y, b_max_y - l_max_y - self.padding)

        # Генерируем сетку. +1e-9 чтобы включить max, если он на сетке,
        # но НЕ создавать лишнюю точку за max (как было с +grid_step).
        eps = 1e-9
        x_coords = np.arange(min_x, max_x + eps, self.grid_step)
        y_coords = np.arange(min_y, max_y + eps, self.grid_step)
        if len(x_coords) == 0 or len(y_coords) == 0:
            return np.array([])
        xx, yy = np.meshgrid(x_coords, y_coords)
        candidates = np.column_stack((xx.ravel(), yy.ravel()))

        # Быстрый отсев по минимальному расстоянию между центрами
        if check_overlap and len(placed_pos) > 0 and self.min_center_dist > 0:
            dists = np.linalg.norm(
                candidates[:, np.newaxis, :] - placed_pos[np.newaxis, :], axis=2
            )
            mask = (dists > self.min_center_dist).all(axis=1)
            candidates = candidates[mask]

        if len(candidates) == 0:
            return candidates

        # Точная фильтрация по bbox выбранного компонента
        if check_overlap:
            valid: List[Tuple[float, float]] = []
            for cx, cy in candidates:
                if not self._check_overlap(chosen_idx, float(cx), float(cy)):
                    valid.append((float(cx), float(cy)))
            return np.array(valid) if valid else np.array([])

        return candidates

    # ------------------------------------------------------------------
    # Шаг 6: выбор лучшей позиции (критерий F, многопоточно)
    # ------------------------------------------------------------------
    def _calculate_F_chunk(
        self, args: Tuple[np.ndarray, np.ndarray, np.ndarray]
    ) -> np.ndarray:
        """F = Σ c_ij · dist(candidate, placed_j) для чанка кандидатов."""
        chunk, placed_pos, weights = args
        diff = chunk[:, np.newaxis, :] - placed_pos[np.newaxis, :, :]
        dists = np.linalg.norm(diff, axis=2)
        return np.dot(dists, weights)

    def _find_best_position(
        self, chosen_idx: int, candidates: np.ndarray
    ) -> Tuple[float, float] | None:
        """Минимум F по кандидатам. None — если кандидатов нет."""
        if len(candidates) == 0:
            return None

        placed_pos = np.array([self.positions[i] for i in self.placed_indices])
        weights = self.C[chosen_idx, self.placed_indices]

        # Для малого набора кандидатов threading нерентабелен
        if len(candidates) < 64 or self.max_workers == 1:
            all_F = self._calculate_F_chunk((candidates, placed_pos, weights))
        else:
            num_workers = self.max_workers or 4
            num_workers = min(num_workers, len(candidates))
            chunks = [c for c in np.array_split(candidates, num_workers) if len(c) > 0]
            tasks = [(c, placed_pos, weights) for c in chunks]
            parts: List[np.ndarray] = []
            with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                for r in ex.map(self._calculate_F_chunk, tasks):
                    parts.append(r)
            all_F = np.concatenate(parts) if parts else np.array([])

        if len(all_F) == 0:
            return None
        min_F = all_F.min()
        idx = int(np.where(np.isclose(all_F, min_F))[0][0])
        return float(candidates[idx, 0]), float(candidates[idx, 1])

    # ------------------------------------------------------------------
    # Шаг 7: коммит и метрики
    # ------------------------------------------------------------------
    def _commit_placement(self, idx: int, x: float, y: float) -> None:
        self.modules[idx].po_x = x
        self.modules[idx].po_y = y
        self.positions[idx] = (x, y)
        self.placed_indices.append(idx)
        if idx in self.unplaced_indices:
            self.unplaced_indices.remove(idx)
        self.placement_order.append(self.modules[idx].name)

    def _calculate_total_wire_length(self) -> float:
        """Σ C_ij · d(i, j) по всем парам с C_ij > 0 (верхний треугольник).

        Векторизованно, но только по связным парам — экономит память на
        больших платах, где C разрежена.
        """
        if self.n == 0:
            return 0.0
        pos = np.array(self.positions)
        rows, cols = np.where(np.triu(self.C, k=1) > 0)
        if len(rows) == 0:
            return 0.0
        dists = np.linalg.norm(pos[rows] - pos[cols], axis=1)
        weights = self.C[rows, cols]
        return float(np.dot(dists, weights))

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------
    def run(self) -> PlacementResult:
        start = time.perf_counter()

        if self.n == 0:
            return PlacementResult(0.0, 0, 0, 0.0, [], [])

        self._build_connectivity_matrix()

        if self.n == 1:
            # Один модуль — тоже размещаем как anchor
            self._place_anchor()
            end = time.perf_counter()
            return PlacementResult(
                execution_time_sec=end - start,
                component_count=1,
                placed_count=1,
                total_wire_length_mm=0.0,
                placement_order=[m.name for m in self.modules],
            )

        # Шаг 1: anchor
        self._place_anchor()

        # Шаги 2–8
        while self.unplaced_indices:
            # Шаги 3–4: выбираем модуль с макс. J (ПЕРВЫМ — до кандидатов)
            j_values = self._calculate_J_all()
            if len(j_values) == 0:
                break
            best_local = int(np.argmax(j_values))
            chosen_idx = self.unplaced_indices[best_local]

            # Шаги 2, 5, 6: кандидаты для ВЫБРАННОГО модуля → минимум F.
            # Фаза 1: ищем позицию БЕЗ пересечений, расширяя радиус поиска.
            best_pos: Tuple[float, float] | None = None
            for attempt in range(self.max_search_expansions):
                off = self.neighbor_offset * (1 + attempt)
                candidates = self._generate_candidates(
                    chosen_idx, neighbor_offset=off, check_overlap=True
                )
                if len(candidates) > 0:
                    best_pos = self._find_best_position(chosen_idx, candidates)
                    if best_pos is not None:
                        break

            # Фаза 2: best-effort — позиции без проверки пересечений.
            # Модуль всё равно нужно разместить (не оставлять в исходной
            # позиции, которая может конфликтовать). Выбираем минимум F
            # по всем позициям в пределах платы.
            if best_pos is None:
                off = self.neighbor_offset * self.max_search_expansions
                candidates = self._generate_candidates(
                    chosen_idx, neighbor_offset=off, check_overlap=False
                )
                if len(candidates) > 0:
                    best_pos = self._find_best_position(chosen_idx, candidates)

            if best_pos is None:
                # Модуль не помещается в плату вообще — фиксируем неудачу.
                self.failed_to_place.append(self.modules[chosen_idx].name)
                self.unplaced_indices.remove(chosen_idx)
                continue

            # Шаг 7
            self._commit_placement(chosen_idx, best_pos[0], best_pos[1])

        end = time.perf_counter()
        wire = self._calculate_total_wire_length()
        return PlacementResult(
            execution_time_sec=end - start,
            component_count=self.n,
            placed_count=len(self.placed_indices),
            total_wire_length_mm=wire,
            placement_order=self.placement_order,
            failed_to_place=self.failed_to_place,
        )


def auto_place_board(
    board: Board,
    grid_step_mm: float = 10.0,
    anchor: AnchorPosition = "center",
    criteria: SelectionCriteria = "absolute",
    component_priority: ComponentPriority = "max_connections",
    max_workers: int | None = None,
    neighbor_offset_mm: float = 20.0,
    padding_mm: float = 3.0,
    min_center_dist_mm: float = 15.0,
) -> PlacementResult:
    """Удобная обёртка над AutoPlacer.run()."""
    placer = AutoPlacer(
        board=board,
        grid_step_mm=grid_step_mm,
        anchor=anchor,
        criteria=criteria,
        component_priority=component_priority,
        max_workers=max_workers,
        neighbor_offset_mm=neighbor_offset_mm,
        padding_mm=padding_mm,
        min_center_dist_mm=min_center_dist_mm,
    )
    return placer.run()
