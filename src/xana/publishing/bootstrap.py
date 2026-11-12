"""Standalone bootstrap copied beside the two artifacts; stdlib only, no xana required."""
from pathlib import Path
import json
import os
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3,12):
        raise SystemExit("独立应用安装要求 Python 3.12 或更新版本")
    bundle = Path(__file__).resolve().parent
    packages = sorted(p.parent for p in bundle.glob('*/xana-agent.json'))
    if len(packages) != 1:
        raise SystemExit('发布包必须包含一个能力包')
    package = packages[0]
    manifest = json.loads((package/'xana-agent.json').read_text())
    home = Path(os.environ.get('XANA_HOME') or Path.home()/'.xana').expanduser()
    name=manifest['name']
    if not name or Path(name).name!=name or name in ('.','..'):
        raise SystemExit('无效应用名')
    envdir = home/'apps'/name/'venv'
    if (envdir.parent/'package').exists():
        raise SystemExit('应用已安装，未覆盖已有配置和数据')
    if not envdir.exists():
        venv.EnvBuilder(with_pip=True).create(envdir)
    python = envdir/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    wheels = list(bundle.glob('xana-*.whl'))
    if len(wheels)!=1:
        raise SystemExit('发布包缺少唯一的 xana 运行核 wheel')
    import hashlib
    checks=json.loads((bundle/'checksums.json').read_text())
    if hashlib.sha256(wheels[0].read_bytes()).hexdigest()!=checks.get(wheels[0].name):
        raise SystemExit('运行核校验失败')
    subprocess.run([str(python),'-m','pip','install',str(wheels[0])],check=True)
    subprocess.run([str(python),'-m','xana.cli.xana','agents','add',str(package),'--agent',manifest['name']],check=True)
    subprocess.run([str(python),'-m','xana.cli.xana','agents','setup',manifest['name']],check=True)


if __name__=='__main__':
    main()
