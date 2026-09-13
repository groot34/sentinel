"""Built-in source code and git diff inspection tools for Sentinel 2.0.

Provides deterministic parsing of unified git diff patches and regex/symbol
searching across incident service source files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from agents.code_tools import (
    SKIP_DIR_NAMES,
    SKIP_FILE_NAMES,
    DiffHunk as _DiffHunk,
    parse_git_diff as _parse_git_diff,
)
from core.tools.base import BaseTool
from core.tools.context import ToolExecutionContext
from core.tools.models import (
    DiffFileObservation,
    DiffHunkObservation,
    SourceMatchObservation,
)
from core.tools.result import ToolResult


# ============================================================================
# 1. GitDiffParseTool
# ============================================================================

class GitDiffParseInput(BaseModel):
    """Input parameters for parsing a git diff patch."""

    relative_path: str = Field(default="git_diff.patch", description="Relative path to git diff/patch file")


class GitDiffParseTool(BaseTool[GitDiffParseInput]):
    """Deterministic tool to parse a unified git diff into structured file changes and hunks."""

    name = "parse_git_diff"
    description = "Parse a unified git diff patch file into structured changed files, hunks, and line edits."
    input_schema = GitDiffParseInput

    def run(self, params: GitDiffParseInput, context: ToolExecutionContext) -> ToolResult:
        diff_path = context.resolve_path(params.relative_path)
        if not diff_path.is_file():
            return ToolResult.fail(
                self.name,
                f"Git diff file not found at '{params.relative_path}'",
            )

        try:
            text = diff_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            return ToolResult.fail(
                self.name,
                f"Failed to read diff file at '{params.relative_path}': {exc}",
            )

        git_diff = _parse_git_diff(text)

        # Group hunks by target file
        file_hunks: Dict[str, List[_DiffHunk]] = {}
        for h in git_diff.hunks:
            key = h.new_path if h.new_path and h.new_path != "/dev/null" else h.old_path
            file_hunks.setdefault(key, []).append(h)

        observations: List[DiffFileObservation] = []
        for file_path, hunks in file_hunks.items():
            total_added = sum(len(h.added) for h in hunks)
            total_removed = sum(len(h.removed) for h in hunks)

            # Determine change type from first hunk
            first = hunks[0]
            if first.old_path == "/dev/null":
                change_type = "added"
            elif first.new_path == "/dev/null":
                change_type = "deleted"
            else:
                change_type = "modified"

            hunk_obs = [
                DiffHunkObservation(
                    old_start=h.old_start,
                    old_lines=len(h.removed),
                    new_start=h.new_start,
                    new_lines=len(h.added),
                    header=h.header,
                    lines=h.body_lines,
                )
                for h in hunks
            ]

            observations.append(
                DiffFileObservation(
                    file_path=file_path,
                    change_type=change_type,
                    added_lines=total_added,
                    removed_lines=total_removed,
                    hunks=hunk_obs,
                )
            )

        # Legitimate empty diff -> SUCCESS with data=[]
        return ToolResult.ok(self.name, data=observations)


# ============================================================================
# 2. SourceFileSearchTool
# ============================================================================

class SourceFileSearchInput(BaseModel):
    """Input parameters for searching within source files."""

    pattern: str = Field(..., min_length=1, description="Regex or string pattern to search for")
    relative_dir: str = Field(default="service", description="Relative directory within incident to search")
    ignore_case: bool = Field(default=False, description="Whether pattern search is case-insensitive")


class SourceFileSearchTool(BaseTool[SourceFileSearchInput]):
    """Search service source code files line by line for string or regex patterns."""

    name = "search_source_files"
    description = "Search service source code files for pattern or symbol occurrences."
    input_schema = SourceFileSearchInput

    def run(self, params: SourceFileSearchInput, context: ToolExecutionContext) -> ToolResult:
        search_dir = context.resolve_path(params.relative_dir)
        if not search_dir.is_dir():
            return ToolResult.fail(
                self.name,
                f"Source directory not found at '{params.relative_dir}'",
            )

        flags = re.IGNORECASE if params.ignore_case else 0
        try:
            compiled = re.compile(params.pattern, flags)
        except re.error:
            compiled = re.compile(re.escape(params.pattern), flags)

        observations: List[SourceMatchObservation] = []
        for file_path in sorted(search_dir.rglob("*.py")):
            if any(part in SKIP_DIR_NAMES for part in file_path.parts):
                continue
            if file_path.name in SKIP_FILE_NAMES:
                continue

            # Verify canonical safety for each discovered file through context
            try:
                rel_to_incident = file_path.relative_to(context.incident_root).as_posix()
                _ = context.resolve_path(rel_to_incident)
            except Exception:
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for line_no, line in enumerate(content.splitlines(), start=1):
                if compiled.search(line):
                    observations.append(
                        SourceMatchObservation(
                            file_path=rel_to_incident,
                            line_number=line_no,
                            line_text=line,
                            pattern=params.pattern,
                        )
                    )

        # Empty match must be SUCCESS with data=[]
        return ToolResult.ok(self.name, data=observations)
