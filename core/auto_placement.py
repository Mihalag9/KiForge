"""
Модуль авторазмещения компонентов на плате (KiForge).

Реализует последовательный алгоритм размещения по связности (Раздел 3.3)
с учётом поворота и зеркалирования компонентов.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Literal

import numpy as np

from .board import Board
from .footprint import Footprint

SelectionCriteria = Literal["absolute", "relative", "max_connection"]
AnchorPosition = Literal["center", "top_left"]
ComponentPriority = Literal["max_connections", "max_size"]


@dataclass
class PlacementResult:
    execution_time_sec: float
    component_count: int
    total_wire_length_mm: float
    placement_order: List[str]


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
        padding_mm: float = 3.0
    ):
        self.board = board
        self.grid_step = grid_step_mm
        self.anchor = anchor
        self.criteria = criteria
        self.component_priority = component_priority
        self.max_workers = max_workers
        self.neighbor_offset = neighbor_offset_mm
        self.padding = padding_mm
        
        self.modules = board.modules
        self.n = len(self.modules)
        
        self.C: np.ndarray = np.zeros((self.n, self.n), dtype=np.float32)
        self.positions: List[Tuple[float, float]] = [
            (float(fp.po_x), float(fp.po_y)) for fp in self.modules
        ]
        
        self.placed_indices: List[int] = []
        self.unplaced_indices: List[int] = list(range(self.n))
        self.placement_order: List[str] = []

    def _build_connectivity_matrix(self) -> None:
        nets_per_module: List[set] = []
        for fp in self.modules:
            nets = set(p.net for p in fp.pads if p.net > 0)
            nets_per_module.append(nets)
        
        for i in range(self.n):
            for j in range(i + 1, self.n):
                common_nets = len(nets_per_module[i].intersection(nets_per_module[j]))
                self.C[i, j] = common_nets
                self.C[j, i] = common_nets

    def _get_board_bounds(self) -> Tuple[float, float, float, float]:
        edge_drawings = [d for d in self.board.drawings if d.layer == "Edge.Cuts"]
        if not edge_drawings:
            return 0.0, 0.0, 100.0, 100.0
        
        xs = [d.x1 for d in edge_drawings] + [d.x2 for d in edge_drawings]
        ys = [d.y1 for d in edge_drawings] + [d.y2 for d in edge_drawings]
        return min(xs), min(ys), max(xs), max(ys)

    def _get_component_bbox(self, idx: int, x: float, y: float) -> Tuple[float, float, float, float]:
        """
        Возвращает bounding box компонента в абсолютных координатах.
        Учитывает поворот и зеркалирование модуля.
        """
        fp = self.modules[idx]
        
        # Параметры трансформации
        angle = fp.po_rotation / 10.0  # градусы
        mirrored = fp.po_layer == 0  # B.Cu
        
        # Локальные координаты падов
        local_points = [(p.x, p.y) for p in fp.pads]
        
        if not local_points:
            return x - 5.0, y - 5.0, x + 5.0, y + 5.0
        
        # Применяем трансформацию к локальным координатам
        abs_points = []
        for lx, ly in local_points:
            # Зеркалирование
            if mirrored:
                ly = -ly
            # Поворот
            if angle:
                rad = math.radians(angle)
                c, s = math.cos(rad), math.sin(rad)
                lx, ly = lx * c + ly * s, -lx * s + ly * c
            # Смещаем по новой позиции
            abs_points.append((x + lx, y + ly))
        
        min_x = min(p[0] for p in abs_points) - self.padding
        max_x = max(p[0] for p in abs_points) + self.padding
        min_y = min(p[1] for p in abs_points) - self.padding
        max_y = max(p[1] for p in abs_points) + self.padding
        
        return min_x, min_y, max_x, max_y

    def _check_overlap(self, idx: int, x: float, y: float) -> bool:
        """
        Проверяет пересечение bounding box компонента idx в позиции (x, y)
        со всеми уже размещенными компонентами.
        """
        new_bbox = self._get_component_bbox(idx, x, y)
        
        for placed_idx in self.placed_indices:
            px, py = self.positions[placed_idx]
            placed_bbox = self._get_component_bbox(placed_idx, px, py)
            
            # Проверка пересечения прямоугольников (AABB)
            if new_bbox[2] <= placed_bbox[0] or new_bbox[0] >= placed_bbox[2]:
                continue
            if new_bbox[3] <= placed_bbox[1] or new_bbox[1] >= placed_bbox[3]:
                continue
            
            return True
            
        return False

    def _place_anchor(self) -> None:
        if self.n == 0:
            return
        
        if self.component_priority == "max_connections":
            row_sums = self.C.sum(axis=1)
            anchor_idx = int(np.argmax(row_sums))
        else:
            sizes = [self._get_component_size(i) for i in range(self.n)]
            anchor_idx = sizes.index(max(sizes))
        
        min_x, min_y, max_x, max_y = self._get_board_bounds()
        if self.anchor == "center":
            x = (min_x + max_x) / 2.0
            y = (min_y + max_y) / 2.0
        else:
            x = min_x + 10.0
            y = min_y + 10.0
        
        self._commit_placement(anchor_idx, x, y)

    def _get_component_size(self, idx: int) -> float:
        fp = self.modules[idx]
        if not fp.pads:
            return 0.0
        
        # Локальные координаты падов
        local_points = [(p.x, p.y) for p in fp.pads]
        
        # Применяем трансформацию
        angle = fp.po_rotation / 10.0
        mirrored = fp.po_layer == 0
        
        transformed = []
        for lx, ly in local_points:
            if mirrored:
                ly = -ly
            if angle:
                rad = math.radians(angle)
                c, s = math.cos(rad), math.sin(rad)
                lx, ly = lx * c + ly * s, -lx * s + ly * c
            transformed.append((lx, ly))
        
        width = max(p[0] for p in transformed) - min(p[0] for p in transformed)
        height = max(p[1] for p in transformed) - min(p[1] for p in transformed)
        return width * height

    def _generate_candidates(self) -> np.ndarray:
        """
        Генерирует сетку кандидатов и фильтрует позиции с пересечением.
        """
        if not self.placed_indices:
            return np.array([])
        
        placed_pos = np.array([self.positions[i] for i in self.placed_indices])
        
        min_x, min_y = placed_pos.min(axis=0) - self.neighbor_offset
        max_x, max_y = placed_pos.max(axis=0) + self.neighbor_offset
        
        b_min_x, b_min_y, b_max_x, b_max_y = self._get_board_bounds()
        min_x, min_y = max(min_x, b_min_x), max(min_y, b_min_y)
        max_x, max_y = min(max_x, b_max_x), min(max_y, b_max_y)
        
        x_coords = np.arange(min_x, max_x + self.grid_step, self.grid_step)
        y_coords = np.arange(min_y, max_y + self.grid_step, self.grid_step)
        xx, yy = np.meshgrid(x_coords, y_coords)
        candidates = np.column_stack((xx.ravel(), yy.ravel()))
        
        # Быстрая фильтрация по минимальному расстоянию между центрами
        min_center_dist = 15.0
        if len(placed_pos) > 0:
            dists = np.linalg.norm(candidates[:, np.newaxis] - placed_pos[np.newaxis, :], axis=2)
            mask = (dists > min_center_dist).all(axis=1)
            candidates = candidates[mask]
        
        # Точная фильтрация по bounding box
        valid_candidates = []
        
        for x, y in candidates:
            can_place = True
            # Проверяем для самого большого неразмещенного компонента
            if self.unplaced_indices:
                # Находим компонент с максимальным размером
                sizes = [(i, self._get_component_size(i)) for i in self.unplaced_indices]
                largest_idx = max(sizes, key=lambda x: x[1])[0]
                
                if self._check_overlap(largest_idx, float(x), float(y)):
                    can_place = False
            
            if can_place:
                valid_candidates.append((x, y))
                
        return np.array(valid_candidates) if valid_candidates else np.array([])

    def _calculate_J(self, idx: int) -> float:
        placed_mask = np.zeros(self.n, dtype=bool)
        placed_mask[self.placed_indices] = True
        unplaced_mask = ~placed_mask
        
        c_i = self.C[idx]
        sum_placed = c_i[placed_mask].sum()
        
        if self.criteria == "absolute":
            sum_unplaced = c_i[unplaced_mask].sum()
            return float(sum_placed - sum_unplaced)
        elif self.criteria == "relative":
            total = c_i.sum()
            return float(sum_placed / total) if total > 0 else 0.0
        elif self.criteria == "max_connection":
            if len(self.placed_indices) == 0:
                return 0.0
            return float(c_i[self.placed_indices].max())
        return 0.0

    def _calculate_F_chunk(self, args: Tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray:
        chunk_candidates, placed_pos, weights = args
        diff = chunk_candidates[:, np.newaxis, :] - placed_pos[np.newaxis, :, :]
        dists = np.linalg.norm(diff, axis=2)
        F_values = np.dot(dists, weights)
        return F_values

    def _find_best_position(self, chosen_idx: int, candidates: np.ndarray) -> Tuple[float, float]:
        if len(candidates) == 0:
            last_x, last_y = self.positions[self.placed_indices[-1]]
            return last_x + 25.0, last_y + 25.0
        
        placed_pos = np.array([self.positions[i] for i in self.placed_indices])
        weights = self.C[chosen_idx, self.placed_indices]
        
        num_workers = self.max_workers or 4
        chunks = np.array_split(candidates, num_workers)
        tasks = [(chunk, placed_pos, weights) for chunk in chunks if len(chunk) > 0]
        
        all_F: List[np.ndarray] = []
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            results = executor.map(self._calculate_F_chunk, tasks)
            for res in results:
                all_F.append(res)
        
        if not all_F:
            return tuple(candidates[0])
        
        all_F = np.concatenate(all_F)
        min_F = all_F.min()
        final_idx = int(np.where(np.isclose(all_F, min_F))[0][0])
        
        return tuple(candidates[final_idx])

    def _commit_placement(self, idx: int, x: float, y: float) -> None:
        self.modules[idx].po_x = x
        self.modules[idx].po_y = y
        self.positions[idx] = (x, y)
        
        self.placed_indices.append(idx)
        self.unplaced_indices.remove(idx)
        self.placement_order.append(self.modules[idx].name)

    def _calculate_total_wire_length(self) -> float:
        pos_array = np.array(self.positions)
        total_length = 0.0
        for i in range(self.n):
            for j in range(i + 1, self.n):
                if self.C[i, j] > 0:
                    dist = math.dist(pos_array[i], pos_array[j])
                    total_length += self.C[i, j] * dist
        return total_length

    def run(self) -> PlacementResult:
        start_time = time.perf_counter()
        
        if self.n <= 1:
            return PlacementResult(0.0, self.n, 0.0, [m.name for m in self.modules])
        
        self._build_connectivity_matrix()
        self._place_anchor()
        
        while self.unplaced_indices:
            candidates = self._generate_candidates()
            
            best_j = -math.inf
            chosen_idx = self.unplaced_indices[0]
            
            for idx in self.unplaced_indices:
                j_val = self._calculate_J(idx)
                if j_val > best_j:
                    best_j = j_val
                    chosen_idx = idx
                elif j_val == best_j and idx < chosen_idx:
                    chosen_idx = idx
            
            best_pos = self._find_best_position(chosen_idx, candidates)
            self._commit_placement(chosen_idx, best_pos[0], best_pos[1])
        
        end_time = time.perf_counter()
        wire_length = self._calculate_total_wire_length()
        
        return PlacementResult(
            execution_time_sec=end_time - start_time,
            component_count=self.n,
            total_wire_length_mm=wire_length,
            placement_order=self.placement_order
        )


def auto_place_board(
    board: Board,
    grid_step_mm: float = 10.0,
    anchor: AnchorPosition = "center",
    criteria: SelectionCriteria = "absolute",
    component_priority: ComponentPriority = "max_connections",
    max_workers: int | None = None,
    neighbor_offset_mm: float = 20.0,
    padding_mm: float = 3.0
) -> PlacementResult:
    placer = AutoPlacer(
        board=board,
        grid_step_mm=grid_step_mm,
        anchor=anchor,
        criteria=criteria,
        component_priority=component_priority,
        max_workers=max_workers,
        neighbor_offset_mm=neighbor_offset_mm,
        padding_mm=padding_mm
    )
    return placer.run()