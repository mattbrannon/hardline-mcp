"""Every write names its columns (CLAUDE.md, "Never reshape a table in place").

Revisions of this code run against one store for as long as their processes
live. A positional ``INSERT INTO t VALUES (...)`` fails in all of them the moment
any revision adds a column to ``t``, so no writer may rely on a table's shape.
"""

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "hardline_mcp"

# INSERT [OR x] INTO table VALUES - a table name straight into VALUES. Strings
# are adjacent literals in the source, so whitespace between them is optional.
POSITIONAL = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+\w+\s*VALUES", re.IGNORECASE)


def positional_inserts() -> list[str]:
    found = []
    for path in sorted(SOURCE.glob("*.py")):
        # Adjacent string literals are one statement: join them before matching.
        text = re.sub(r'"\s*\n\s*(?:f?")', "", path.read_text(encoding="utf-8"))
        found += [f"{path.name}: {m.group(0)}" for m in POSITIONAL.finditer(text)]
    return found


def test_no_insert_relies_on_a_tables_column_order():
    assert positional_inserts() == []
