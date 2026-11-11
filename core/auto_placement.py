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
        padding_mm: float = 1.5,
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
        # Кэш локальных bbox: (min_x, min_y, max_x, max_y) после поворота/
        # зеркалирования, относительно origin. Не меняется во время
        # размещения → вычисляем один раз.
        self._local_bbox_cache: List[Tuple[float, float, float, float] | None] = [None] * self.n

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
        Использует кэшированный локальный bbox → O(1), без трансформации
        точек при каждом вызове.
        """
        lb = self._local_bbox(idx)
        if lb is None:
            return x - 5.0, y - 5.0, x + 5.0, y + 5.0
        l_min_x, l_min_y, l_max_x, l_max_y = lb
        return (
            x + l_min_x - self.padding,
            y + l_min_y - self.padding,
            x + l_max_x + self.padding,
            y + l_max_y + self.padding,
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
        """Пересекает ли bbox компонента idx в (x, y) любой размещённый.
        idx исключается из проверки (не проверяем against себя — важно
        для swap, когда модуль уже в placed_indices)."""
        new_bbox = self._get_component_bbox(idx, x, y)
        for pi in self.placed_indices:
            if pi == idx:
                continue
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
        origin модуля. Кэшируется — не пересчитывается при каждом вызове.
        """
        cached = self._local_bbox_cache[idx]
        if cached is not None:
            return cached
        local = self._local_points(idx)
        if not local:
            self._local_bbox_cache[idx] = None
            return None
        angle, mirrored = self._module_angle_mirrored(idx)
        tr = [self._transform_point(lx, ly, angle, mirrored) for lx, ly in local]
        xs = [p[0] for p in tr]
        ys = [p[1] for p in tr]
        result = (min(xs), min(ys), max(xs), max(ys))
        self._local_bbox_cache[idx] = result
        return result

    def _generate_candidates(
        self,
        chosen_idx: int,
        neighbor_offset: float | None = None,
        check_overlap: bool = True,
        grid_step: float | None = None,
    ) -> np.ndarray:
        """Сетка позиций вокруг размещённых, отфильтрованная так, чтобы
        компонент chosen_idx в этой позиции не выходил за границы платы.

        При check_overlap=True (по умолчанию) также отсеваются позиции,
        где bbox модуля пересекает любой размещённый модуль.

        neighbor_offset задаёт радиус расширения области поиска вокруг
        кластера размещённых модулей (по умолчанию self.neighbor_offset).
        grid_step — шаг сетки (по умолчанию self.grid_step).
        """
        if not self.placed_indices:
            return np.array([])

        off = neighbor_offset if neighbor_offset is not None else self.neighbor_offset
        step = grid_step if grid_step is not None else self.grid_step
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

        # Генерируем сетку. +eps чтобы включить max, если он на сетке.
        eps = 1e-9
        x_coords = np.arange(min_x, max_x + eps, step)
        y_coords = np.arange(min_y, max_y + eps, step)
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
    # Шаг 7+: итерационное улучшение (pairwise swap + relocate)
    # ------------------------------------------------------------------
    def _wire_contribution(self, idx: int) -> float:
        """Длина связей модуля idx ко всем остальным (по текущим позициям).
        Векторизованно через numpy."""
        if idx < 0 or idx >= self.n:
            return 0.0
        c_row = self.C[idx]
        mask = c_row > 0
        if not mask.any():
            return 0.0
        pos = np.array(self.positions)
        px, py = pos[idx]
        dists = np.linalg.norm(pos[mask] - np.array([px, py]), axis=1)
        return float(np.dot(c_row[mask], dists))

    def _pair_has_overlap(self, i: int, j: int) -> bool:
        """Проверяет, пересекаются ли модули i и j в их текущих позициях."""
        bi = self._get_component_bbox(i, *self.positions[i])
        bj = self._get_component_bbox(j, *self.positions[j])
        return not (bi[2] <= bj[0] or bi[0] >= bj[2] or
                    bi[3] <= bj[1] or bi[1] >= bj[3])

    def _any_overlap(self) -> bool:
        """Есть ли хотя бы одно пересечение среди всех размещённых."""
        for i in self.placed_indices:
            for j in self.placed_indices:
                if j <= i:
                    continue
                if self._pair_has_overlap(i, j):
                    return True
        return False

    def _in_bounds(self, idx: int, x: float, y: float) -> bool:
        """Входит ли bbox модуля idx в позиции (x, y) в границы платы."""
        bbox = self._get_component_bbox(idx, x, y)
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        eps = 0.01
        return (bbox[0] >= b_min_x - eps and bbox[1] >= b_min_y - eps and
                bbox[2] <= b_max_x + eps and bbox[3] <= b_max_y + eps)

    def _try_swap(self, i: int, j: int) -> bool:
        """Обмен позициями модулей i и j. Оставляем, если:
        - оба модуля остаются в границах платы
        - не появляется пересечение
        - суммарная длина связей уменьшается.
        """
        pi, pj = self.positions[i], self.positions[j]
        # Считаем вклад i+j до обмена
        old_ij = self._wire_contribution(i) + self._wire_contribution(j)
        # Обмен
        self.positions[i], self.positions[j] = pj, pi
        self.modules[i].po_x, self.modules[i].po_y = pj
        self.modules[j].po_x, self.modules[j].po_y = pi
        # Проверка границ (модули разного размера — позиция валидна для одного
        # может быть невалидна для другого)
        if not self._in_bounds(i, pj[0], pj[1]) or not self._in_bounds(j, pi[0], pi[1]):
            self.positions[i], self.positions[j] = pi, pj
            self.modules[i].po_x, self.modules[i].po_y = pi
            self.modules[j].po_x, self.modules[j].po_y = pj
            return False
        # Проверка пересечений (только i и j — остальные не двигались)
        if self._check_overlap(i, pj[0], pj[1]) or self._check_overlap(j, pi[0], pi[1]):
            self.positions[i], self.positions[j] = pi, pj
            self.modules[i].po_x, self.modules[i].po_y = pi
            self.modules[j].po_x, self.modules[j].po_y = pj
            return False
        new_ij = self._wire_contribution(i) + self._wire_contribution(j)
        if new_ij < old_ij - 0.01:
            return True
        # Откат — нет улучшения
        self.positions[i], self.positions[j] = pi, pj
        self.modules[i].po_x, self.modules[i].po_y = pi
        self.modules[j].po_x, self.modules[j].po_y = pj
        return False

    def _try_relocate(self, idx: int) -> bool:
        """Пытаемся переместить модуль idx на лучшую позицию рядом.
        Возвращает True, если модуль был перемещён с улучшением."""
        # Временно убираем idx из placed для генерации кандидатов
        saved_placed = list(self.placed_indices)
        self.placed_indices = [k for k in self.placed_indices if k != idx]
        # F к размещённым (без idx) — одинаковая метрика до и после
        placed_pos = np.array([self.positions[k] for k in self.placed_indices]) if self.placed_indices else None
        weights = np.array([float(self.C[idx, k]) for k in self.placed_indices]) if self.placed_indices else None
        if placed_pos is None or len(placed_pos) == 0:
            self.placed_indices = saved_placed
            return False
        old_pos = np.array(self.positions[idx])
        old_f = float(np.dot(np.linalg.norm(placed_pos - old_pos, axis=1), weights))
        best_pos = None
        best_f = old_f
        for grid_div in [1, 2]:
            step = self.grid_step / grid_div
            for attempt in range(2):
                off = self.neighbor_offset * (1 + attempt)
                candidates = self._generate_candidates(
                    idx, neighbor_offset=off, check_overlap=True, grid_step=step
                )
                if len(candidates) == 0:
                    continue
                diff = candidates[:, np.newaxis, :] - placed_pos[np.newaxis, :, :]
                dists = np.linalg.norm(diff, axis=2)
                f_vals = np.dot(dists, weights)
                min_i = int(np.argmin(f_vals))
                new_f = float(f_vals[min_i])
                if new_f < best_f - 0.01:
                    best_f = new_f
                    best_pos = (float(candidates[min_i, 0]), float(candidates[min_i, 1]))
                    break
            if best_pos is not None:
                break
        self.placed_indices = saved_placed
        if best_pos is not None:
            self.positions[idx] = best_pos
            self.modules[idx].po_x, self.modules[idx].po_y = best_pos
            return True
        return False

    def _find_any_free_position(self, idx: int) -> Tuple[float, float] | None:
        """Ищет ЛЮБУЮ позицию на плате, где модуль idx не пересекает
        размещённые и не выходит за границы. Мелкая сетка, вся плата.
        Возвращает None, если позиций совсем нет.
        """
        lbbox = self._local_bbox(idx)
        if lbbox is None:
            return None
        l_min_x, l_min_y, l_max_x, l_max_y = lbbox
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        # Границы для origin: чтобы bbox вошёл в плату
        ox_min = b_min_x - l_min_x + self.padding
        oy_min = b_min_y - l_min_y + self.padding
        ox_max = b_max_x - l_max_x - self.padding
        oy_max = b_max_y - l_max_y - self.padding
        if ox_max < ox_min or oy_max < oy_min:
            return None
        # Мелкая сетка по всей плате
        step = min(self.grid_step / 4, 2.5)
        eps = 1e-9
        for cy in np.arange(oy_min, oy_max + eps, step):
            for cx in np.arange(ox_min, ox_max + eps, step):
                if not self._check_overlap(idx, float(cx), float(cy)):
                    return float(cx), float(cy)
        return None

    def _iterative_improve(self, max_passes: int = 10) -> int:
        """Итерационное улучшение: pairwise swap + relocate.
        Возвращает число улучшений за все проходы."""
        if self.n < 2:
            return 0
        total_improvements = 0
        for _ in range(max_passes):
            improved = False
            # Pairwise swap
            placed = list(self.placed_indices)
            for a in range(len(placed)):
                for b in range(a + 1, len(placed)):
                    if self._try_swap(placed[a], placed[b]):
                        improved = True
                        total_improvements += 1
            # Relocate
            for idx in list(self.placed_indices):
                if self._try_relocate(idx):
                    improved = True
                    total_improvements += 1
            if not improved:
                break
        return total_improvements

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------
    def run(self) -> PlacementResult:
        start = time.perf_counter()

        if self.n == 0:
            return PlacementResult(0.0, 0, 0, 0.0, [], [])

        self._build_connectivity_matrix()

        if self.n == 1:
            self._place_anchor()
            end = time.perf_counter()
            return PlacementResult(
                execution_time_sec=end - start,
                component_count=1,
                placed_count=1,
                total_wire_length_mm=0.0,
                placement_order=[m.name for m in self.modules],
            )

        # === ФАЗА 1: последовательное размещение (Раздел 3.3) ===
        # Принцип: НИКОГДА не ставим модуль в позицию с пересечением.
        # Если на базовой сетке нет позиции — измельчаем сетку (×2, ×4).
        # Если и так нет — модуль переходит в «pending» и ждёт фазу 2.
        self._place_anchor()
        pending: List[int] = []

        # Фаза 1a: крупные модули первыми (пока плата свободна).
        # Крупные модули最难 разместить, когда плата заполнена. Ставим их
        # сразу после anchor, пока есть простор. Позиция всё равно
        # выбирается по F (минимум длины связей).
        large_count = max(0, self.n // 4)  # top 25% по размеру
        if large_count > 0:
            sizes = [(i, self._get_component_size(i)) for i in self.unplaced_indices]
            sizes.sort(key=lambda x: x[1], reverse=True)
            for idx, _ in sizes[:large_count]:
                if idx not in self.unplaced_indices:
                    continue
                best_pos = None
                for grid_div in [1, 2, 4]:
                    step = self.grid_step / grid_div
                    for attempt in range(self.max_search_expansions):
                        off = self.neighbor_offset * (1 + attempt)
                        candidates = self._generate_candidates(
                            idx, neighbor_offset=off,
                            check_overlap=True, grid_step=step,
                        )
                        if len(candidates) > 0:
                            best_pos = self._find_best_position(idx, candidates)
                            if best_pos is not None:
                                break
                    if best_pos is not None:
                        break
                if best_pos is not None:
                    self._commit_placement(idx, best_pos[0], best_pos[1])

        # Фаза 1b: остальные модули по J-критерию (Раздел 3.3, пункты 3-4)
        while self.unplaced_indices:
            j_values = self._calculate_J_all()
            if len(j_values) == 0:
                break
            best_local = int(np.argmax(j_values))
            chosen_idx = self.unplaced_indices[best_local]

            best_pos: Tuple[float, float] | None = None
            # Прогрессивное измельчение сетки: step, step/2, step/4
            for grid_div in [1, 2, 4]:
                step = self.grid_step / grid_div
                for attempt in range(self.max_search_expansions):
                    off = self.neighbor_offset * (1 + attempt)
                    candidates = self._generate_candidates(
                        chosen_idx, neighbor_offset=off,
                        check_overlap=True, grid_step=step,
                    )
                    if len(candidates) > 0:
                        best_pos = self._find_best_position(chosen_idx, candidates)
                        if best_pos is not None:
                            break
                if best_pos is not None:
                    break

            if best_pos is not None:
                self._commit_placement(chosen_idx, best_pos[0], best_pos[1])
            else:
                # НЕ размещаем с пересечением — в pending
                pending.append(chosen_idx)
                self.unplaced_indices.remove(chosen_idx)

        # === ФАЗА 2: итерационное улучшение + размещение pending ===
        if pending:
            # Лёгкая итерационная оптимизация для освобождения места
            self._iterative_improve(max_passes=1)
            # Пробуем pending с измельчённой сеткой
            still_pending = []
            for idx in pending:
                best_pos = None
                for grid_div in [4, 8]:
                    step = self.grid_step / grid_div
                    for attempt in range(self.max_search_expansions):
                        off = self.neighbor_offset * (2 + attempt)
                        candidates = self._generate_candidates(
                            idx, neighbor_offset=off,
                            check_overlap=True, grid_step=step,
                        )
                        if len(candidates) > 0:
                            best_pos = self._find_best_position(idx, candidates)
                            if best_pos is not None:
                                break
                    if best_pos is not None:
                        break
                if best_pos is not None:
                    self._commit_placement(idx, best_pos[0], best_pos[1])
                else:
                    still_pending.append(idx)
            pending = still_pending

        # Финальная итерационная оптимизация (1 проход — достаточно,
        # основной выигрыш даёт relocate, а не swap)
        self._iterative_improve(max_passes=1)

        # === ФАЗА 3: принудительное размещение остатков ===
        # Pending-модули, которым не нашлось позиции рядом со связями,
        # всё равно нужно разместить (не оставлять в исходной позиции,
        # которая может пересекать размещённые). Ищем ЛЮБУЮ свободную
        # позицию на всей плате с мелкой сеткой.
        if pending:
            still_pending = []
            for idx in pending:
                best_pos = self._find_any_free_position(idx)
                if best_pos is not None:
                    self._commit_placement(idx, best_pos[0], best_pos[1])
                else:
                    still_pending.append(idx)
            pending = still_pending

        # Truly failed — не помещаются в плату вообще
        for idx in pending:
            self.failed_to_place.append(self.modules[idx].name)

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
    padding_mm: float = 1.5,
    min_center_dist_mm: float = 0.0,
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
