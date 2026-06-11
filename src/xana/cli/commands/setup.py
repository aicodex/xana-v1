"""xana setup — materialize project .xana/ + initialize ~/.xana/config/system.json.

Idempotent: re-running never overwrites existing files. Reports what was
created vs. skipped on stdout so the user can see machine state at a glance.
"""
from __future__ import annotations

import argparse
from importlib import resources
from pathlib import Path
from typing import TextIO

from xana.config.loader import ensure_user_system_json, user_config_dir


_SKELETON_PACKAGE = "xana._templates.xana_skeleton"
_PROJECT_DIR_NAME = ".xana"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="project root to set up (default: cwd)",
    )


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    project_root = (args.project or Path.cwd()).resolve()
    if not project_root.exists():
        print(f"[xana setup] project root does not exist: {project_root}", file=out)
        return 2

    target_dir = project_root / _PROJECT_DIR_NAME
    created, skipped = _materialize_skeleton(target_dir)
    print(f"[xana setup] project skeleton at {target_dir}", file=out)
    for path in created:
        print(f"  created  {path.relative_to(project_root)}", file=out)
    for path in skipped:
        print(f"  skipped  {path.relative_to(project_root)} (exists)", file=out)

    user_path = ensure_user_system_json()
    user_existed = user_path.exists() and user_path.stat().st_size > 0
    print(file=out)
    print(f"[xana setup] user config dir   {user_config_dir()}", file=out)
    print(
        f"  {'ready  ' if user_existed else 'created'} {user_path.name}",
        file=out,
    )
    return 0


def _materialize_skeleton(target_dir: Path) -> tuple[list[Path], list[Path]]:
    """Walk the package skeleton resources and copy each file under target_dir.

    Returns (created_paths, skipped_paths). Empty file content for .gitkeep
    placeholders is preserved as-is. Uses manual relative-path tracking
    because importlib.resources Traversables (MultiplexedPath) don't support
    pathlib's relative_to.
    """
    created: list[Path] = []
    skipped: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    skeleton_root = resources.files(_SKELETON_PACKAGE)
    for rel_parts, resource in _walk_files(skeleton_root, ()):
        dest = target_dir.joinpath(*rel_parts)
        if dest.exists():
            skipped.append(dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resource.read_bytes())
        created.append(dest)
    return created, skipped


def _walk_files(node, prefix: tuple[str, ...]) -> list[tuple[tuple[str, ...], object]]:
    """Depth-first walk over importlib.resources Traversable, yielding
    (relative_path_parts, file_traversable) tuples."""
    out: list[tuple[tuple[str, ...], object]] = []
    for entry in node.iterdir():
        name = entry.name
        if entry.is_file():
            out.append((prefix + (name,), entry))
        elif entry.is_dir():
            out.extend(_walk_files(entry, prefix + (name,)))
    return out
