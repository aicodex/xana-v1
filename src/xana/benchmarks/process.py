"""Bounded, isolated transport to the normal Xana runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading

from xana.benchmarks.workspace import remove_credentials


class TrialProcess:
    def __init__(self, home: Path, workspace: Path, logs: Path, *, init: dict,
                 timeout: float = 600, python: str = sys.executable) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.home = home
        self.timeout = timeout
        logs.mkdir(parents=True, exist_ok=True)
        self.stderr = (logs / "worker.stderr.log").open("w")
        self.events: queue.Queue[str | None] = queue.Queue()
        try:
            self.process = subprocess.Popen(
                [python, "-m", "xana.benchmarks.worker", "--home", str(home),
                 "--workspace", str(workspace)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                text=True, bufsize=1, start_new_session=True,
            )
        except BaseException:
            self.stderr.close()
            remove_credentials(home)
            raise
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.send(init)
            response = self.receive()
            if response["kind"] != "ready":
                raise RuntimeError(f"Invalid Xana handshake: {response}")
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.events.put(line)
        finally:
            self.events.put(None)

    def send(self, message: dict) -> None:
        if self.process.poll() is not None:
            raise RuntimeError(f"Xana worker exited with {self.process.returncode}")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict:
        try:
            try:
                line = self.events.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"Xana produced no response within {self.timeout}s") from exc
            if line is None:
                raise RuntimeError("Xana worker closed its output; inspect worker.stderr.log")
            response = json.loads(line)
            if response["kind"] == "error":
                raise RuntimeError(response["error"])
            return response
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.reader.join(timeout=2)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.stderr.close()
        remove_credentials(self.home)
