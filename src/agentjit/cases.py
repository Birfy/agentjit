"""Loading corpus cases. A case is an implementation, a spec, and the verdict it
should receive."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .types import Example, Spec


@dataclass
class Case:
    name: str
    why: str                      # which gate this case is meant to exercise
    source: str
    spec: Spec
    examples: list[Example]
    expect_level: str
    expect_failing: list[str]     # the gates expected to fail
    path: Path

    @classmethod
    def load(cls, d: Path) -> "Case":
        meta = json.loads((d / "case.json").read_text())
        return cls(
            name=meta.get("name", d.name),
            why=meta.get("why", ""),
            source=(d / meta.get("impl", "impl.py")).read_text(),
            spec=Spec(**meta["spec"]),
            examples=[Example(**e) for e in meta["examples"]],
            expect_level=meta["expect"]["level"],
            expect_failing=meta["expect"].get("failing_gates", []),
            path=d,
        )


def load_all(root: Path) -> list[Case]:
    return [Case.load(d) for d in sorted(root.iterdir()) if (d / "case.json").exists()]
