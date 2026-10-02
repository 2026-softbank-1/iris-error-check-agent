import json
from pathlib import Path

from ai_error_check_agent.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_prepare_cli_writes_sanitized_evidence(tmp_path):
    output = tmp_path / "prepared.json"
    code = main(
        [
            "prepare",
            "--request",
            str(ROOT / "examples/configuration.request.json"),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    result = json.loads(output.read_text("utf-8"))
    assert result["logs"][0]["id"] == "EV000001"
    assert "tenant_id" not in result


def test_validate_cli_accepts_design_example(capsys):
    code = main(
        [
            "validate",
            "--request",
            str(ROOT / "examples/configuration.request.json"),
            "--analysis",
            str(ROOT / "examples/configuration.analysis.json"),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_bad_request_never_echoes_secret(tmp_path, request_data, capsys):
    request_data["tenant_id"] = "invalid/value/SUPER_PRIVATE"
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_data), "utf-8")
    assert main(["prepare", "--request", str(request)]) == 1
    output = capsys.readouterr()
    assert "SUPER_PRIVATE" not in output.err + output.out
    assert "INVALID_REQUEST" in output.err


def test_existing_output_is_not_overwritten(tmp_path, capsys):
    output = tmp_path / "existing.json"
    output.write_text("keep", "utf-8")
    assert (
        main(
            [
                "prepare",
                "--request",
                str(ROOT / "examples/configuration.request.json"),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert output.read_text("utf-8") == "keep"
    assert "OUTPUT_EXISTS" in capsys.readouterr().err
