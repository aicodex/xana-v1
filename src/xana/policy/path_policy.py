"""Path boundary policies: protected-directory write gate & optional workspace confinement."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from xana.policy.contracts import PathOperation, PermissionResult
from xana.policy.interfaces import PathBoundaryPolicy
from xana.protocol.diag import DiagLogger
from xana.protocol.primitives import ComponentVersion
from xana.protocol.versioning import canonical_json, sha256_text

_DIAG = DiagLogger("policy")


def resolve_target_path(raw_path: str | Path, cwd: str | Path) -> Path:
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve()


class ProtectedDirsPathPolicy(PathBoundaryPolicy):
    """Denies writes inside protected directories (memory/, agents/, tools/, evals/) and optionally confines to roots."""

    POLICY_VERSION = "1.0.0"

    def __init__(
        self,
        *,
        protected_dirs: Iterable[Path] = (),
        deny_reason: str = "目标路径属于受保护目录，请使用专用工具修改",
        allowed_write_roots: Iterable[Path] = (),
    ) -> None:
        self._protected = tuple(Path(d).expanduser().resolve() for d in protected_dirs)
        self._deny_reason = deny_reason
        self._allowed_roots = tuple(Path(d).expanduser().resolve() for d in allowed_write_roots)

    def component_version(self) -> ComponentVersion:
        cfg_hash = sha256_text(canonical_json({
            "protected": sorted(str(p) for p in self._protected),
            "allowed_roots": sorted(str(p) for p in self._allowed_roots),
        }))[:16]
        return ComponentVersion(
            name="protected_dirs_path_policy",
            kind="policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
            config_hash=cfg_hash,
        )

    def check_path(self, path: Path, *, operation: PathOperation, cwd: Path) -> PermissionResult:
        target = resolve_target_path(path, cwd)
        if operation == "write":
            for protected in self._protected:
                try:
                    target.relative_to(protected)
                except ValueError:
                    continue
                _DIAG.anomaly("path.protected_denied", target=str(target), protected=str(protected))
                return PermissionResult(decision="deny", reason=self._deny_reason)
            if self._allowed_roots:
                in_root = False
                for root in self._allowed_roots:
                    try:
                        target.relative_to(root)
                        in_root = True
                        break
                    except ValueError:
                        continue
                if not in_root:
                    _DIAG.anomaly("path.outside_workspace", target=str(target))
                    return PermissionResult(decision="deny", reason=f"路径 {target} 超出允许写入目录")
        return PermissionResult(decision="allow")
