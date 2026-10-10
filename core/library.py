from __future__ import annotations
from dataclasses import dataclass, field
from .footprint import Footprint


@dataclass
class Library:
    file_name: str = ""                 # имя файла без расширения
    header_date: str = ""               # дата из заголовка (при сохранении обновляется)
    modules: list[Footprint] = field(default_factory=list)

    # строки между заголовком и $INDEX (обычно "# encoding utf-8")
    extra_header: list[str] = field(default_factory=list)
    # строки между последним $EndMODULE и $EndLIBRARY
    extra_before_end: list[str] = field(default_factory=list)