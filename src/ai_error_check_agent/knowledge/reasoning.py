"""Optional bounded inference service, without importing RDF in the API process."""

import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import asdict, dataclass

from ..errors import DiagnosisError
from ..preprocessing import redact_object
from .context import augment

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReasoningSettings:
    mode: str = "off"
    timeout_seconds: float = 0.2
    variant: str = "rules"  # Evaluation-only ablation; not a public request/config field.

    def __post_init__(self):
        if (
            self.mode not in {"off", "assist"}
            or self.variant not in {"facts", "rules"}
            or not math.isfinite(self.timeout_seconds)
            or not 0.01 <= self.timeout_seconds <= 2
        ):
            raise ValueError("invalid reasoning settings")

    @classmethod
    def from_config(cls, config):
        try:
            return cls(
                mode=config.get("AGENT_REASONING_MODE") or "off",
                timeout_seconds=float(config.get("AGENT_REASONING_TIMEOUT_MS") or 200) / 1000,
            )
        except (ValueError, TypeError):
            raise DiagnosisError("INVALID_CONFIG", "관계 추론 설정을 확인하세요.") from None


class ReasoningService:
    def __init__(self, settings):
        self.settings = settings
        self.process = None
        self.ready = False
        self.busy = False
        self.closed = False
        self.jobs = 0
        self._maintenance = None
        self._active_task = None
        self.stats = {}
        self.last_report = None

    def start(self):
        if (
            self.settings.mode == "assist"
            and not self.closed
            and not self.ready
            and not self.busy
            and self._maintenance is None
        ):
            self._maintenance = asyncio.create_task(self._replace())

    async def wait_ready(self):
        """Explicit warmup for offline evaluation/tests, never awaited by HTTP requests."""
        self.start()
        if self._maintenance:
            await asyncio.shield(self._maintenance)
        return self.ready

    async def _replace(self):
        self.ready = False
        try:
            old, self.process = self.process, None
            if old is not None:
                await self._stop(old)
            if self.closed:
                return
            async with asyncio.timeout(5):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "ai_error_check_agent.knowledge.reasoning_worker",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=262146,
                )
                self.process = process
                if json.loads(await process.stdout.readline()) != {"status": "ready"}:
                    raise ValueError("worker_not_ready")
            self.ready = True
            self.jobs = 0
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - isolate startup failures
            if self.process is not None:
                await self._stop(self.process)
                self.process = None
            self.ready = False
        finally:
            self._maintenance = None

    @staticmethod
    async def _stop(process):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def aclose(self):
        self.closed = True
        self.ready = False
        if self._active_task is not None and self._active_task is not asyncio.current_task():
            self._active_task.cancel()
            await asyncio.gather(self._active_task, return_exceptions=True)
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        if self.process is not None:
            await self._stop(self.process)
            self.process = None

    def _finish(self, status, report=None):
        self.stats[status] = self.stats.get(status, 0) + 1
        result = report if report is not None else {"status": status}
        self.last_report = result
        LOGGER.info("knowledge_reasoning status=%s", status)
        return result

    async def infer(self, payload):
        if self.closed or self.settings.mode != "assist":
            return self._finish("off")
        if not self.ready or self.process is None or self.process.returncode is not None:
            self.ready = False
            self.start()
            return self._finish("not_ready")
        if self.busy:
            return self._finish("busy")
        raw = json.dumps(redact_object(payload), ensure_ascii=False).encode()
        if len(raw) > 262144:
            return self._finish("too_large")
        self.busy = True
        self._active_task = asyncio.current_task()
        started = time.perf_counter()
        discard = False
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                self.process.stdin.write(raw + b"\n")
                await self.process.stdin.drain()
                response = await self.process.stdout.readline()
            if len(response) > 262145:
                raise ValueError("response_limit")
            report = json.loads(response)
            if not isinstance(report, dict) or report.get("status") not in {"ok", "no_match"}:
                raise ValueError("invalid_worker_response")
            self.jobs += 1
            report["observer_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return self._finish(report["status"], report)
        except TimeoutError:
            discard = True
            return self._finish("timed_out")
        except asyncio.CancelledError:
            discard = True
            raise
        except Exception:  # noqa: BLE001 - all inference failures return to original diagnosis
            discard = True
            return self._finish("failed")
        finally:
            self.busy = False
            self._active_task = None
            if discard or self.jobs >= 100:
                self.ready = False
                self.start()

    async def apply(
        self, request, bundle, prompt, data, schema, profile, *, service_id=None, stage="logs"
    ):
        """Project only supplied evidence, not model assertions or source archives."""
        scope = {
            k: getattr(request, k)
            for k in ("tenant_id", "project_id", "deployment_id", "attempt_id")
        }
        scope["service_id"] = service_id
        payload = {
            "scope": scope,
            "inference": self.settings.variant == "rules",
            "stage": stage,
            "logs": [asdict(line) for line in bundle.lines],
            "source_evidence": data.get("source_evidence", []),
        }
        try:
            report = await self.infer(payload)
            new_prompt, new_data, context = augment(
                prompt,
                data,
                schema,
                report,
                profile.max_prompt_bytes,
                variant=self.settings.variant,
            )
            report = {
                **report,
                "stage": stage,
                "applied_context": context,
                "variant": self.settings.variant,
            }
            self.last_report = report
            return new_prompt, new_data, report
        except Exception:  # noqa: BLE001 - preserve the original prompt and inputs
            return prompt, data, {"stage": stage, "status": "failed", "applied_context": None}
