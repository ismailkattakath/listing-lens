"""Read the analyst's GQM tree (DATA notes/li-grafana-gqm.md) by its stated ID grammar.

- question heading: `^### (G\\d+\\.Q\\d+)( \\(retired\\))? — `
- a question that needs no panel has the exact text `no panel: <reason>` on its heading line
The file lives in DATA, never in git: it is the analyst's, and the repo holds no data.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

HEADING = re.compile(r"^### (G\d+\.Q\d+)( \(retired\))? — ", re.M)
QID = re.compile(r"^G\d+\.Q\d+$")
REL = Path("notes") / "li-grafana-gqm.md"


@dataclass(frozen=True)
class Tree:
    active: frozenset[str]  # not retired
    no_panel: frozenset[str]  # active, and exempt from "every question has a panel"
    retired: frozenset[str]


def parse(text: str) -> Tree:
    active, no_panel, retired = set(), set(), set()
    for m in HEADING.finditer(text):
        qid, is_retired = m[1], bool(m[2])
        line = text[m.start() : text.find("\n", m.start()) if "\n" in text[m.start() :] else len(text)]
        if is_retired:
            retired.add(qid)
            continue
        active.add(qid)
        if "no panel: " in line:
            no_panel.add(qid)
    return Tree(frozenset(active), frozenset(no_panel), frozenset(retired))


def path(data_root: str | None = None) -> Path | None:
    root = data_root if data_root is not None else os.environ.get("LLENS_DATA")
    return Path(root) / REL if root else None
