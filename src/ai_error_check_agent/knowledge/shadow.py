"""Bounded background observer with reusable subprocesses and local-only telemetry."""

import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import DiagnosisError
from .record import record_from_result

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgeSettings:
    mode: str = "off"
    output_dir: Path = Path(".runtime/knowledge")
    timeout_seconds: float = 5.0
    max_concurrent: int = 1
    max_triples: int = 10_000

    def __post_init__(self):
        if (
            self.mode not in {"off", "shadow"}
            or not math.isfinite(self.timeout_seconds)
            or not 0.05 <= self.timeout_seconds <= 30
            or not 1 <= self.max_concurrent <= 2
            or not 100 <= self.max_triples <= 20_000
        ):
            raise ValueError("invalid knowledge settings")

    @classmethod
    def from_config(cls, config):
        try:
            return cls(
                mode=config.get("AGENT_KG_MODE") or "off",
                output_dir=Path(config.get("AGENT_KG_OUTPUT_DIR") or ".runtime/knowledge"),
                timeout_seconds=float(config.get("AGENT_KG_TIMEOUT_SECONDS") or 5),
                max_concurrent=int(config.get("AGENT_KG_MAX_CONCURRENT") or 1),
                max_triples=int(config.get("AGENT_KG_MAX_TRIPLES") or 10_000),
            )
        except (TypeError, ValueError):
            raise DiagnosisError("INVALID_CONFIG", "그래프 병행 검증 설정을 확인하세요.") from None


class ShadowObserver:
    def __init__(self, settings):
        self.settings = settings
        self.active = 0
        self.stats = {
            key: 0 for key in ("submitted", "recorded", "failed", "timed_out", "busy", "too_large")
        }
        self.last_report = None
        self.last_submission = None
        self._workers = [None] * settings.max_concurrent
        self._jobs = [0] * settings.max_concurrent
        self._busy = [False] * settings.max_concurrent
        self._tasks = set()
        self._closed = False

    async def __aenter__(self):
        self._closed = False
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def _claim(self, result, stages, reasoning=()):
        started = time.perf_counter()
        if self._closed:
            return self._finish("failed")
        if self.active >= self.settings.max_concurrent:
            return self._finish("busy")
        try:
            record = record_from_result(result, stages)
            if reasoning:
                record["reasoning"] = reasoning
            data = json.dumps(record, ensure_ascii=False).encode()
            if len(data) > 262_144:
                return self._finish("too_large")
        except Exception:  # noqa: BLE001 - projection failures cannot affect the diagnosis
            return self._finish("failed")
        # Immutable bytes own the snapshot; reserve before the first await. No queue is kept.
        slot = self._busy.index(False)
        self._busy[slot] = True
        self.active += 1
        return slot, data, started, {"started": False}

    def submit(self, result, stages, *, reasoning=()):
        """Snapshot and schedule work without awaiting generation, validation or writes."""
        job = self._claim(result, stages, reasoning)
        if isinstance(job, str):
            return job
        slot, _, started, lease = job
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._run(*job))
        except RuntimeError:
            self._busy[slot] = False
            self.active -= 1
            return self._finish("failed")
        self._tasks.add(task)

        def completed(task):
            self._tasks.discard(task)
            if not lease["started"]:
                self._busy[slot] = False
                self.active -= 1

        task.add_done_callback(completed)
        self.last_submission = {"elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        return self._finish("submitted")

    async def observe(self, result, stages):
        """Await completion for benchmarks and offline callers, reusing the same pool."""
        job = self._claim(result, stages)
        return job if isinstance(job, str) else await self._run(*job)

    async def _run(self, slot, data, started, lease):
        lease["started"] = True
        recorded = False
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                process = self._workers[slot]
                reused = (
                    process is not None and process.returncode is None and self._jobs[slot] < 100
                )
                if not reused:
                    await self._stop_worker(slot)
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "ai_error_check_agent.knowledge.worker",
                        "--serve",
                        "--output-dir",
                        str(self.settings.output_dir),
                        "--max-triples",
                        str(self.settings.max_triples),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        limit=8193,
                    )
                    self._workers[slot] = process
                    self._jobs[slot] = 0
                process.stdin.write(data + b"\n")
                await process.stdin.drain()
                stdout = await process.stdout.readline()
            report = json.loads(stdout) if len(stdout) <= 8192 else {}
            if report.get("status") != "recorded":
                return self._finish("failed")
            report["observer_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            report["worker_reused"] = reused
            self.last_report = report
            self._jobs[slot] += 1
            recorded = True
            return self._finish("recorded")
        except TimeoutError:
            return self._finish("timed_out")
        except Exception:  # noqa: BLE001 - the observer cannot break the existing diagnosis
            return self._finish("failed")
        finally:
            if not recorded:
                await self._stop_worker(slot)
            self._busy[slot] = False
            self.active -= 1

    async def drain(self):
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def aclose(self):
        self._closed = True
        try:
            await self.drain()
        finally:
            for slot in range(len(self._workers)):
                await self._stop_worker(slot)

    async def _stop_worker(self, slot):
        process = self._workers[slot]
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        self._workers[slot] = None
        self._jobs[slot] = 0

    def _finish(self, status):
        self.stats[status] += 1
        # Status-only telemetry; no input, response text, URL, or credential is logged.
        LOGGER.info("knowledge_shadow status=%s", status)
        return status
