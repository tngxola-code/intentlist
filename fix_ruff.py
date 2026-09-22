from pathlib import Path

# src/intentlist/db.py
p = Path("src/intentlist/db.py")
s = p.read_text()
s = s.replace("from typing import Iterator", "from collections.abc import Iterator")
p.write_text(s)

# src/intentlist/models.py
p = Path("src/intentlist/models.py")
s = p.read_text()

# datetime.UTC
s = s.replace(
    "from datetime import date, datetime, timezone",
    "from datetime import date, datetime, UTC",
)
s = s.replace("datetime.now(timezone.utc)", "datetime.now(UTC)")

# keep timezone if it is still used elsewhere
if "timezone" in s:
    s = s.replace(
        "from datetime import date, datetime, UTC",
        "from datetime import date, datetime, timezone, UTC",
    )

# ClassVar for RUF012
if "ClassVar" not in s:
    s = s.replace(
        "from datetime import date, datetime, UTC",
        "from datetime import date, datetime, UTC\nfrom typing import ClassVar",
    )

s = s.replace(
    "type_annotation_map = {dict: JSON, list: JSON}",
    "type_annotation_map: ClassVar[dict] = {dict: JSON, list: JSON}",
)
p.write_text(s)

# src/intentlist/normalize.py
p = Path("src/intentlist/normalize.py")
s = p.read_text()
s = s.replace(", re.I)", ", re.IGNORECASE)")
p.write_text(s)

print("Ruff fixes applied.")
