"""Build a pure-Python runtime wheel from the current engine, using its package metadata."""
from __future__ import annotations

import base64
import csv
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import zipfile

import xana
from xana.config.paths import xana_home


def runtime_metadata() -> tuple[str, list[str]]:
    import tomllib
    # A synced installation records its authoritative source; pip installations use dist-info.
    source = xana_home() / '.sync_source'
    if source.exists():
        package = Path(source.read_text().strip())
        root = package.parent.parent
        project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
        dependencies = list(project['dependencies'])
        for entries in project.get('optional-dependencies', {}).values():
            dependencies.extend(entries)
        return project['version'], sorted(set(dependencies))
    dist = metadata.distribution('xana')
    # Include optional channel support in a distributable runtime as well.
    dependencies = [r.split(';')[0].strip() for r in dist.requires or ()]
    return dist.version, sorted(set(dependencies))


def build_runtime(destination: Path) -> tuple[Path, str]:
    root = Path(xana.__file__).parent
    files = {str(p.relative_to(root.parent)): p.read_bytes() for p in sorted(root.rglob('*'))
             if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')}
    base, dependencies = runtime_metadata()
    digest = hashlib.sha256()
    for name, data in files.items():
        digest.update(name.encode()); digest.update(data)
    digest.update(json.dumps(dependencies).encode())
    version = base.split('+')[0] + '+bundle.' + digest.hexdigest()[:16]
    info = f'xana-{version}.dist-info'
    headers = ['Metadata-Version: 2.1', 'Name: xana', f'Version: {version}',
               'Summary: Shared xana agent runtime', 'Requires-Python: >=3.12']
    headers.extend('Requires-Dist: ' + dependency for dependency in dependencies)
    files[info+'/METADATA'] = ('\n'.join(headers)+'\n').encode()
    files[info+'/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: xana-agent-publish\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    files[info+'/entry_points.txt'] = b'[console_scripts]\nxana = xana.cli.xana:main\nxanac = xana.cli.xanac:main\n'
    record = io.StringIO()
    writer = csv.writer(record, lineterminator='\n')
    for name, data in files.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        writer.writerow((name, 'sha256='+encoded, len(data)))
    writer.writerow((info+'/RECORD','',''))
    files[info+'/RECORD'] = record.getvalue().encode()
    path = destination / f'xana-{version}-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            entry.compress_type=zipfile.ZIP_DEFLATED
            entry.create_system=3
            entry.external_attr=0o100644<<16
            archive.writestr(entry, data)
    return path, version


def file_sha256(path: Path) -> str:
    with path.open('rb') as file:
        return hashlib.file_digest(file,'sha256').hexdigest()
