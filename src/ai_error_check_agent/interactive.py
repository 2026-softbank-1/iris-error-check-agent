"""Launch the native OpenCode model picker with the shared IRIS model catalog."""

import argparse
import json
import subprocess
from pathlib import Path

from .errors import DiagnosisError
from .model_catalog import load_catalog
from .opencode_process import ManagedOpenCode


def main(argv=None):
    parser = argparse.ArgumentParser(description="IRIS OpenCode interactive diagnosis")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model", help="Initial provider/model; use /models to switch in OpenCode")
    parser.add_argument("--port", type=int, default=4097)
    parser.add_argument(
        "--list", action="store_true", help="List choices without starting OpenCode"
    )
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.env_file)
        if args.list:
            print(json.dumps(catalog.public(), ensure_ascii=False, indent=2))
            return 0
        choice = catalog.select(args.model)
        with ManagedOpenCode(
            choice.profile,
            choice.key,
            root=Path.cwd(),
            port=args.port,
            catalog=catalog,
            interactive=True,
        ) as managed:
            print(
                "IRIS: select iris_diagnosis; /models switches models. Paste sanitized logs/source.",
                flush=True,
            )
            result = subprocess.run(
                [
                    str(managed.binary),
                    "attach",
                    managed.profile.base_url,
                    "--dir",
                    str(managed.work),
                ],
                env=managed.environment,
                cwd=managed.work,
                check=False,
            )
            return result.returncode
    except (DiagnosisError, OSError) as exc:
        parser.error(
            exc.message if isinstance(exc, DiagnosisError) else "OpenCode 실행 환경을 확인하세요."
        )


if __name__ == "__main__":
    raise SystemExit(main())
