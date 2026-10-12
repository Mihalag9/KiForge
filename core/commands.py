"""Undo/Redo через паттерн Command."""
from __future__ import annotations
from .footprint import Footprint, Pad, FpLine, FpCircle, FpArc, FpText


class Command:
    label = "команда"
    def do(self): raise NotImplementedError
    def undo(self): raise NotImplementedError


class CommandStack:
    def __init__(self):
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self.on_change = None

    def push(self, cmd: Command):
        cmd.do()
        self._undo.append(cmd)
        self._redo.clear()
        self._notify()

    def undo(self) -> bool:
        if not self._undo: return False
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)
        self._notify()
        return True

    def redo(self) -> bool:
        if not self._redo: return False
        cmd = self._redo.pop()
        cmd.do()
        self._undo.append(cmd)
        self._notify()
        return True

    def clear(self):
        self._undo.clear(); self._redo.clear(); self._notify()

    def can_undo(self): return bool(self._undo)
    def can_redo(self): return bool(self._redo)

    def _notify(self):
        if self.on_change is not None:
            self.on_change()


class EditObjectCommand(Command):
    label = "Изменить объект"
    def __init__(self, obj, **changes):
        self.obj = obj
        self.changes = changes
        self.old = {k: getattr(obj, k) for k in changes}
    def do(self):
        for k, v in self.changes.items():
            setattr(self.obj, k, v)
    def undo(self):
        for k, v in self.old.items():
            setattr(self.obj, k, v)


class EditPadCommand(EditObjectCommand):    label = "Изменить пад"
class EditLineCommand(EditObjectCommand):   label = "Изменить линию"
class EditCircleCommand(EditObjectCommand): label = "Изменить окружность"
class EditArcCommand(EditObjectCommand):    label = "Изменить дугу"
class EditTextCommand(EditObjectCommand):   label = "Изменить текст"


class _ListAdd(Command):
    attr = ""
    def __init__(self, fp: Footprint, obj):
        self.fp = fp; self.obj = obj
    def do(self):
        getattr(self.fp, self.attr).append(self.obj)
    def undo(self):
        lst = getattr(self.fp, self.attr)
        if self.obj in lst: lst.remove(self.obj)


class _ListRemove(Command):
    attr = ""
    def __init__(self, fp: Footprint, obj):
        self.fp = fp; self.obj = obj; self.index = None
    def do(self):
        lst = getattr(self.fp, self.attr)
        if self.obj in lst:
            self.index = lst.index(self.obj)
            lst.remove(self.obj)
    def undo(self):
        if self.index is not None:
            getattr(self.fp, self.attr).insert(self.index, self.obj)


class AddPadCommand(_ListAdd):     label = "Добавить пад";      attr = "pads"
class RemovePadCommand(_ListRemove): label = "Удалить пад";     attr = "pads"
class AddLineCommand(_ListAdd):    label = "Добавить линию";    attr = "lines"
class RemoveLineCommand(_ListRemove): label = "Удалить линию";  attr = "lines"
class AddCircleCommand(_ListAdd):  label = "Добавить окружность"; attr = "circles"
class RemoveCircleCommand(_ListRemove): label = "Удалить окружность"; attr = "circles"
class AddArcCommand(_ListAdd):     label = "Добавить дугу";     attr = "arcs"
class RemoveArcCommand(_ListRemove): label = "Удалить дугу";    attr = "arcs"
class AddTextCommand(_ListAdd):    label = "Добавить текст";    attr = "texts"
class RemoveTextCommand(_ListRemove): label = "Удалить текст";  attr = "texts"


class MoveManyCommand(Command):
    label = "Переместить группу"
    def __init__(self, moves):
        self._moves = list(moves)
        self._old = [{k: getattr(o, k) for k in ch} for o, ch in self._moves]
    def do(self):
        for (obj, changes), _ in zip(self._moves, self._old):
            for k, v in changes.items():
                setattr(obj, k, v)
    def undo(self):
        for (obj, _), old in zip(self._moves, self._old):
            for k, v in old.items():
                setattr(obj, k, v)


class SetOriginCommand(Command):
    label = "Точка привязки"
    def __init__(self, fp: Footprint, dx: float, dy: float):
        self.fp = fp; self.dx = dx; self.dy = dy

    def _shift(self, sign: float):
        for p in self.fp.pads:
            p.x += sign * self.dx; p.y += sign * self.dy
        for ln in self.fp.lines:
            ln.x1 += sign * self.dx; ln.y1 += sign * self.dy
            ln.x2 += sign * self.dx; ln.y2 += sign * self.dy
        for c in self.fp.circles:
            c.cx += sign * self.dx; c.cy += sign * self.dy
            c.ex += sign * self.dx; c.ey += sign * self.dy
        for a in self.fp.arcs:
            a.cx += sign * self.dx; a.cy += sign * self.dy
            a.sx += sign * self.dx; a.sy += sign * self.dy
        for t in self.fp.texts:
            t.x += sign * self.dx; t.y += sign * self.dy

    def do(self):   self._shift(-1.0)
    def undo(self): self._shift(+1.0)