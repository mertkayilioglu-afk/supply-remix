"""Configuration for the review dashboard (live at GET /dashboard, static in docs/dashboard/)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DashboardConfig:
    # Where the banner's "how this was built" link points. Absolute, so it works
    # both from the live server and from GitHub Pages.
    repo_url: str = "https://github.com/mertkayilioglu-afk/supply-remix"
    workflow_doc_path: str = "docs/AI_WORKFLOW.md"

    # Static snapshot: the top N ranked orders get a pre-computed plan ...
    static_top_n: int = 8
    # ... plus the highest-ranked order of every outcome not already covered
    # (full plan, partial plan, insufficient_volume, mixed, ...), so the static
    # page shows each kind of result the engine can give.
    static_include_each_outcome: bool = True

    @property
    def workflow_url(self) -> str:
        return f"{self.repo_url}/blob/main/{self.workflow_doc_path}"
