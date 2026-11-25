"""Terminal-Bench adapter using Harbor's official BaseAgent contract."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import tempfile
import time
import zipfile

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from xana.benchmarks.workspace import prepare_home
from xana.config.system_model import SystemConfig
from xana.llm_driver.auth.kimi_oauth import KimiTokenStore


ROOT = "/opt/xana-benchmark"
PYTHON = f"{ROOT}/venv/bin/python"


class XanaAgent(BaseAgent):
    def __init__(self, *args, xana_home: str, wheel: str,
                 timeout: int = 1800, max_turns: int = 200, **kwargs) -> None:
        super().__init__(*args, skip_model_parsing=True, **kwargs)
        if not self.model_name:
            raise ValueError("Select a model name from the benchmark Xana config")
        if self.mcp_servers or self.skills_dir or self.load_trajectory:
            raise ValueError("This adapter supports fresh Terminal-Bench terminal trials")
        if timeout <= 0 or max_turns <= 0:
            raise ValueError("timeout and max_turns must be positive")
        self.source_home = Path(xana_home).expanduser().resolve()
        self.wheel = Path(wheel).expanduser().resolve()
        if not self.wheel.is_file() or self.wheel.suffix != ".whl":
            raise ValueError("wheel must be a locally built Xana wheel")
        with zipfile.ZipFile(self.wheel) as archive:
            metadata_paths = [p for p in archive.namelist() if p.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise ValueError("Expected one Xana distribution in the wheel")
            metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        if metadata["Name"] != "xana" or not metadata["Version"]:
            raise ValueError("The wheel must contain the xana distribution")
        self.wheel_version = metadata["Version"]
        self.wheel_sha256 = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.timeout = int(timeout)
        self.max_turns = int(max_turns)
        self.kimi_store: KimiTokenStore | None = None

    @staticmethod
    def name() -> str:
        return "xana"

    def version(self) -> str:
        return self.wheel_version

    async def setup(self, environment: BaseEnvironment) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("Install uv on the Harbor host before running Xana")
        # The uploaded uv binary requires a matching Linux host/container architecture.
        result = await environment.exec(f"mkdir -p {ROOT}", timeout_sec=30)
        if result.return_code:
            raise RuntimeError(f"Cannot prepare Xana container: {result.stderr}")
        await environment.upload_file(Path(uv).resolve(), f"{ROOT}/uv")
        await environment.upload_file(self.wheel, f"{ROOT}/{self.wheel.name}")
        result = await environment.exec(
            f"chmod +x {ROOT}/uv && {ROOT}/uv venv --python 3.12 {ROOT}/venv && "
            f"{ROOT}/uv pip install --python {PYTHON} {shlex.quote(ROOT + '/' + self.wheel.name)}",
            timeout_sec=600,
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "setup.log").write_text((result.stdout or "") + (result.stderr or ""))
        if result.return_code:
            raise RuntimeError("Xana installation failed; inspect setup.log")
        with tempfile.TemporaryDirectory(prefix="xana-harbor-") as tmp:
            home = Path(tmp) / "home"
            config = SystemConfig.model_validate_json((self.source_home / "config/system.json").read_text())
            entry = config.model(self.model_name)
            if entry is not None and entry.type.startswith("kimi"):
                self.kimi_store = KimiTokenStore(self.source_home / "credentials/kimi.json")
                await self.kimi_store.access_token()
            prepare_home(self.source_home, home, model=self.model_name,
                         tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"],
                         max_turns=self.max_turns, portable=True)
            await environment.upload_dir(home, f"{ROOT}/home")
        # Write traces into Harbor's collected log mount from the first row.
        # A timeout may cancel run() before a final copy could execute.
        history = shlex.quote(str(self.environment_logs_dir / "xana-history"))
        runs = shlex.quote(str(self.environment_logs_dir / "xana-runs"))
        result = await environment.exec(
            f"mkdir -p {history} {runs} && "
            f"ln -s {history} {ROOT}/home/history && ln -s {runs} {ROOT}/home/runs",
            timeout_sec=30,
        )
        if result.return_code:
            raise RuntimeError("Cannot attach Xana trial logs to Harbor artifacts")

    async def _supply_access_tokens(self, environment: BaseEnvironment,
                                    store: KimiTokenStore) -> None:
        """Refresh at the host authority, atomically deliver access-only snapshots.

        Refresh before the container's own 60-second expiry margin, so the normal
        token store can reread the snapshot without possessing a refresh token.
        """
        previous = ""
        while True:
            token = store.load(from_disk=True)
            await store.access_token(force_refresh=token is not None and token.expires_at - time.time() < 180)
            token = store.load(from_disk=True)
            if token is None:
                raise RuntimeError("Benchmark Kimi credentials disappeared")
            if token.access_token != previous:
                snapshot = {**asdict(token), "refresh_token": ""}
                with tempfile.TemporaryDirectory(prefix="xana-access-") as tmp:
                    path = Path(tmp) / "kimi.json"
                    path.write_text(json.dumps(snapshot))
                    path.chmod(0o600)
                    await environment.upload_file(path, f"{ROOT}/home/credentials/kimi-next.json")
                    result = await environment.exec(
                        f"chmod 600 {ROOT}/home/credentials/kimi-next.json && "
                        f"mv {ROOT}/home/credentials/kimi-next.json {ROOT}/home/credentials/kimi.json",
                        timeout_sec=30,
                    )
                    if result.return_code:
                        raise RuntimeError("Could not update container access token")
                previous = token.access_token
            await asyncio.sleep(30)

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        remote_logs = str(self.environment_logs_dir)
        with tempfile.TemporaryDirectory(prefix="xana-instruction-") as tmp:
            task = Path(tmp) / "instruction.txt"
            task.write_text(instruction, encoding="utf-8")
            await environment.upload_file(task, f"{ROOT}/instruction.txt")
        result_path = remote_logs + "/xana-result.json"
        command = shlex.join([
            PYTHON, "-m", "xana.benchmarks.terminal", "--home", f"{ROOT}/home",
            "--instruction", f"{ROOT}/instruction.txt", "--output", result_path,
            "--timeout", str(self.timeout),
        ])
        execution = asyncio.create_task(environment.exec(command, timeout_sec=self.timeout + 10))
        renewal = (asyncio.create_task(self._supply_access_tokens(environment, self.kimi_store))
                   if self.kimi_store is not None else None)
        try:
            if renewal is not None:
                done, _ = await asyncio.wait({execution, renewal}, return_when=asyncio.FIRST_COMPLETED)
                if renewal in done:
                    await renewal  # Never hide an authentication/snapshot delivery failure.
            result = await execution
            (self.logs_dir / "execution.log").write_text((result.stdout or "") + (result.stderr or ""))
            if result.return_code:
                raise RuntimeError("Xana execution failed; inspect execution.log and worker.stderr.log")
            local_result = self.logs_dir / "xana-result.json"
            await environment.download_file(result_path, local_result)
            payload = json.loads(local_result.read_text())
            usage = payload["usage"]
            context.n_input_tokens = usage.get("total_input_tokens")
            context.n_output_tokens = usage.get("total_output_tokens")
            context.n_cache_tokens = usage.get("total_cache_read_tokens")
            context.metadata = {"xana_result": "xana-result.json", "model": self.model_name,
                                "wheel_sha256": self.wheel_sha256}
        finally:
            pending = [execution, *([renewal] if renewal is not None else [])]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # History already lives in Harbor's log mount. On timeout/cancellation
            # Harbor tears down the task environment, including its access token.
            if not asyncio.current_task().cancelling():
                await environment.exec(
                    f"rm -f {ROOT}/home/config/system.json {ROOT}/home/credentials/kimi.json",
                    timeout_sec=30,
                )
