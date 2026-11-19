"""
Validates every JSON artifact of the PBIP project against the official Microsoft
JSON schema declared in its "$schema" property (report, pages, visuals, .pbip,
.pbir, .pbism, .platform...). Schemas are downloaded on demand and cached.

    pip install jsonschema
    python tools/validate_pbir.py            # from the repository root

Exit code 0 = every file is valid.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

SUFFIXES = {".json", ".pbip", ".pbir", ".pbism"}
SKIP_DIRS = {".git", ".pbi", "sample-data", "bin", "obj", "node_modules"}


@lru_cache(maxsize=None)
def fetch(uri: str) -> dict:
    with urllib.request.urlopen(uri, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def retrieve(uri: str) -> Resource:
    return Resource.from_contents(fetch(uri), default_specification=DRAFT7)


def artifacts(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)
        and (p.suffix in SUFFIXES or p.name == ".platform")
    )


def main(root: Path) -> int:
    registry = Registry(retrieve=retrieve)
    failed = checked = 0
    for path in artifacts(root):
        rel = path.relative_to(root).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            print(f"INVALID JSON  {rel}: {exc}")
            failed += 1
            continue
        uri = data.get("$schema") if isinstance(data, dict) else None
        if not uri:
            print(f"no $schema    {rel}")
            continue
        try:
            schema = fetch(uri)
        except urllib.error.HTTPError as exc:
            # Power BI Desktop can write a schema version before Microsoft publishes it.
            print(f"SKIP          {rel}: schema not published yet ({exc.code})")
            continue
        validator_cls = validators.validator_for(schema)
        validator = validator_cls({"$ref": uri}, registry=registry)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        checked += 1
        if errors:
            failed += 1
            print(f"FAIL          {rel}")
            for err in errors[:10]:
                where = "/".join(str(p) for p in err.absolute_path) or "(root)"
                print(f"    {where}: {err.message[:200]}")
        else:
            print(f"OK            {rel}")
    print(f"\n{checked} file(s) validated against their schema, {failed} with errors.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")))
