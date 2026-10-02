import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .contracts import DiagnosisRequest
from .direct_api import OpenAIResponsesRuntime, load_direct_settings
from .errors import DiagnosisError
from .preprocessing import prepare
from .runtime import ModelProfile, OpenCodeRuntime
from .service import diagnose
from .validation import strict_json, validate_analysis


def configure_console():
    # Use UTF-8 for redirected JSON as well as an interactive Windows terminal.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def read_text(path: Path, limit: int = 2 * 1024 * 1024) -> str:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise DiagnosisError("INPUT_TOO_LARGE", "입력 파일 크기가 한도를 초과했습니다.")
    return raw.decode("utf-8-sig")


def write_result(value: dict, output: Path | None):
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Do not silently replace a prior run or the input file.
        with output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        print(rendered, end="")


async def run_live(request, profile=None, *, env_file=Path(".env")):
    if profile is None:
        direct_profile, api_key = load_direct_settings(env_file, request.model_profile_id)
        runtime = OpenAIResponsesRuntime(direct_profile, api_key)
    else:
        runtime = OpenCodeRuntime(
            profile,
            username=os.environ.get("AI_ERROR_OPENCODE_USERNAME", "opencode"),
            password=os.environ.get("AI_ERROR_OPENCODE_PASSWORD"),
        )
    try:
        return await diagnose(request, runtime)
    finally:
        await runtime.close()


def main(argv=None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(prog="ai-error-check", description="AI_Error_Check_Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "validate", "diagnose"):
        command = commands.add_parser(name)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--output", type=Path)
        if name == "validate":
            command.add_argument("--analysis", type=Path, required=True)
        if name == "diagnose":
            options = command.add_mutually_exclusive_group()
            options.add_argument("--profile", type=Path, help="Optional legacy OpenCode profile")
            options.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.exists():
            raise DiagnosisError(
                "OUTPUT_EXISTS", "출력 파일이 이미 있습니다. 새 경로를 지정하세요."
            )
        request_text = read_text(args.request)
        strict_json(request_text)
        request = DiagnosisRequest.model_validate_json(request_text)
        if args.command == "diagnose":
            if args.profile:
                profile_text = read_text(args.profile, 16_384)
                strict_json(profile_text)
                profile = ModelProfile.model_validate_json(profile_text)
                result = asyncio.run(run_live(request, profile))
            else:
                result = asyncio.run(run_live(request, env_file=args.env_file))
        else:
            bundle = prepare(request)
            result = {"snapshot_sha256": bundle.snapshot_sha256, **bundle.model_payload()}
            if args.command == "validate":
                result = {
                    "valid": True,
                    "analysis": validate_analysis(read_text(args.analysis), bundle),
                }
        write_result(result, args.output)
        return 0 if result.get("job_status", "succeeded") == "succeeded" else 1
    except DiagnosisError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
    except (ValidationError, ValueError, RecursionError):
        # Pydantic and decoder errors can include the original input. Never print them.
        print(
            '{"error":{"code":"INVALID_REQUEST","message":"입력 JSON 또는 필드 규격을 확인하세요."}}',
            file=sys.stderr,
        )
    except OSError:
        print(
            '{"error":{"code":"FILE_ERROR","message":"입출력 파일을 확인하세요."}}', file=sys.stderr
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
