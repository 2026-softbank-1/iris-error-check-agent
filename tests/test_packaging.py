"""Deployment solution tests: provenance, negative controls and executable repair."""

import asyncio
import copy
import json
import shutil
import subprocess
import tarfile

import httpx
import pytest
from test_api import send
from test_backend_api import envelope, tarball
from test_diagnosis_routing import CODE, LOG, payload, response
from test_graph_compact import ACCEPT
from test_source_analysis import factory

from ai_error_check_agent.api import create_app
from ai_error_check_agent.diagnosis_routing import DiagnosisSettings
from ai_error_check_agent.knowledge.compact import build, packaging_query, resources, validate
from ai_error_check_agent.preprocessing import prepare
from ai_error_check_agent.source_analysis import diagnose_with_source
from ai_error_check_agent.source_contracts import DiagnoseAPIResult

DOCKER = """FROM node:22-alpine
WORKDIR /app
COPY --chown=node:node package.json ./
COPY --chown=node:node src ./src
COPY --chown=node:node public ./public
ENV NODE_ENV=production HOST=0.0.0.0 PORT=3000
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \\
  CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "src/server.js"]
"""
IGNORE = ".git\nnode_modules\n.env\n.DS_Store\ntest\nexamples\n"
MODE = DiagnosisSettings("graph_compact")
PACKAGING_ACCEPT = {
    **ACCEPT,
    "plan": "add_docker_copy",
    "summary": "제공 Dockerfile에서 필수 파일의 COPY 누락 후보를 확인했습니다.",
}


def repo(docker=DOCKER, ignore=IGNORE):
    result = {
        "src/tasks.js": CODE,
        "src/server.js": 'import "./tasks.js";',
        "package.json": '{"type":"module"}',
        "public/index.html": "<p>OK</p>",
        "Dockerfile": docker,
        "data/tasks.json": "DATA_CONTENT_CANARY",
    }
    if ignore is not None:
        result[".dockerignore"] = ignore
    return result


def execute(files, outputs, *, root=".", special=None, log=LOG):
    data = envelope()
    data["data"]["logs"][0]["text"] = log
    data["data"]["source"]["rootDirectory"] = root
    downloads = []

    def transport(request):
        downloads.append(request)
        return httpx.Response(200, content=tarball(list(files.items()), special=special))

    create, seen, runtimes = factory(outputs)
    app = create_app(
        create, dev=True, diagnosis_settings=MODE, archive_transport=httpx.MockTransport(transport)
    )
    response = send(app, json=data)
    assert response.status_code == 200, response.text
    assert all(r.closed for r in runtimes)
    return response.json(), seen, downloads


def test_archive_copy_plan_one_call_no_data_content_and_contract_unchanged():
    result, seen, downloads = execute(repo(), [PACKAGING_ACCEPT])
    assert result["job_status"] == "succeeded", result["error"]
    DiagnoseAPIResult.model_validate(result)
    assert len(seen) == len(downloads) == 1
    packet = seen[0][1]
    assert packet["packaging_candidate"]["repo_path"] == "data/tasks.json"
    assert packet["packaging_candidate"]["insert_after_line"] == 5
    assert "DATA_CONTENT_CANARY" not in json.dumps(seen) + json.dumps(result)
    assert "secret-signature" not in json.dumps(seen) + json.dumps(result)
    change = result["analysis"]["remediation"]["plans"][0]["changes"][0]
    assert change["target"] == "Dockerfile" and change["target_known"]
    assert change["snippet"] == "COPY --chown=node:node data/tasks.json ./data/tasks.json"
    assert change["snippet_kind"] == "template" and not change["placeholders"]
    assert result["analysis"]["hypotheses"][1]["support_level"] == "supported"
    assert {r["path"] for r in result["source_analysis"]["read_ranges"]} == {
        "src/tasks.js",
        "Dockerfile",
        ".dockerignore",
    }
    ids = [r["id"] for r in result["source_analysis"]["evidence"]]
    assert len(ids) == len(set(ids))
    assert result["remediation_execution"] == "not_executed"


def test_packaging_preserves_human_intervention_reason():
    uncertainty = (
        "실제 배포 이미지와 제공 Dockerfile의 일치 여부 및 데이터 공급 계약을 "
        "사람이 확인해야 하므로 자동 적용할 수 없습니다."
    )
    result, seen, downloads = execute(repo(), [{**PACKAGING_ACCEPT, "uncertainty": uncertainty}])
    assert result["job_status"] == "succeeded", result["error"]
    assert len(seen) == len(downloads) == 1
    assert result["analysis"]["remediation"]["reason"] == uncertainty
    assert result["analysis"]["remediation"]["plans"][0]["changes"][0]["target"] == "Dockerfile"


@pytest.mark.parametrize("ignore", [None, "", "# empty policy\n", IGNORE])
def test_absent_empty_and_literal_ignore_are_distinct_known_states(ignore):
    result, seen, _ = execute(repo(ignore=ignore), [PACKAGING_ACCEPT])
    assert result["job_status"] == "succeeded", result["error"]
    expected = "absent" if ignore is None else "empty" if ignore == "" else "read"
    assert seen[0][1]["packaging_candidate"]["ignore_state"] == expected


def test_specific_ignore_overrides_root_ignore():
    files = repo(ignore="data\n")
    files["Dockerfile.dockerignore"] = ""
    result, seen, _ = execute(files, [PACKAGING_ACCEPT])
    assert result["job_status"] == "succeeded", result["error"]
    proof = seen[0][1]["packaging_candidate"]
    assert proof["ignore_path"] == "Dockerfile.dockerignore" and proof["ignore_state"] == "empty"


@pytest.mark.parametrize(
    "case",
    [
        "excluded",
        "wildcard",
        "negation",
        "already_copied",
        "copy_all",
        "multi_stage",
        "generated",
        "volume",
        "arg",
        "add",
        "json_copy",
        "variable",
        "custom_base",
        "wrong_workdir",
        "wrong_source_dest",
        "target_absent",
        "oversized_ignore",
        "oversized_docker",
        "specific_excludes",
        "masked",
        "shell_entry",
        "renamed_copy",
        "escape_directive",
        "unknown_copy_flag",
        "unread_ignore",
    ],
)
def test_ambiguous_or_conflicting_packaging_uses_one_full_source_call(case, analysis_data):
    files = repo()
    edits = {
        "excluded": ("ignore", "data\n"),
        "wildcard": ("ignore", "*.json\n"),
        "negation": ("ignore", "*\n!src\n!data\n"),
        "already_copied": ("extra", "COPY --chown=node:node data ./data\n"),
        "copy_all": ("extra", "COPY --chown=node:node . .\n"),
        "multi_stage": ("extra", "FROM node:22-alpine AS final\n"),
        "generated": ("extra", "RUN mkdir -p /app/data\n"),
        "volume": ("extra", "VOLUME /app/data\n"),
        "arg": ("extra", "ARG ROOT=/app\n"),
        "add": ("extra", "ADD data ./data\n"),
        "json_copy": ("extra", 'COPY ["data", "./data"]\n'),
        "variable": ("extra", "COPY ${DATA} ./data\n"),
        "renamed_copy": ("extra", "COPY --chown=node:node public ./data\n"),
        "unknown_copy_flag": ("extra", "COPY --link data ./data\n"),
        "shell_entry": ("extra", 'ENTRYPOINT ["sh", "-c", "initialize"]\n'),
    }
    if case in edits:
        kind, content = edits[case]
        if kind == "ignore":
            files[".dockerignore"] = content
        else:
            files["Dockerfile"] += content
    elif case == "custom_base":
        files["Dockerfile"] = DOCKER.replace("node:22-alpine", "my-custom-base:latest")
    elif case == "wrong_workdir":
        files["Dockerfile"] = DOCKER.replace("/app", "/srv")
    elif case == "wrong_source_dest":
        files["Dockerfile"] = DOCKER.replace("src ./src", "src ./other")
    elif case == "target_absent":
        del files["data/tasks.json"]
    elif case == "oversized_ignore":
        files[".dockerignore"] = "# many lines\n" * 121
    elif case == "oversized_docker":
        files["Dockerfile"] += "# many lines\n" * 121
    elif case == "specific_excludes":
        files["Dockerfile.dockerignore"] = "data/tasks.json\n"
    elif case == "masked":
        files["Dockerfile"] += "ENV API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n"
    elif case == "escape_directive":
        files["Dockerfile"] = "# escape=`\n" + DOCKER
    elif case == "unread_ignore":
        files[".dockerignore"] = "\0"
    result, seen, _ = execute(files, [response(analysis_data)])
    assert result["job_status"] == "succeeded", (case, result["error"])
    assert len(seen) == 1
    assert "source_findings" in seen[0][2]["properties"]
    assert "packaging_candidate" not in seen[0][1]


@pytest.mark.parametrize("name", [".dockerignore", "Dockerfile.dockerignore", "data/tasks.json"])
def test_links_never_support_absence_or_supply_claim(name, analysis_data):
    files = repo()
    files.pop(name, None)
    special = tarfile.TarInfo(name)
    special.type, special.linkname = tarfile.SYMTYPE, "somewhere"
    result, seen, _ = execute(files, [response(analysis_data)], special=special)
    assert result["job_status"] == "succeeded"
    assert "source_findings" in seen[0][2]["properties"]


def test_project_root_scope_does_not_use_sibling_files():
    files = {"owner-repo-abcdef1/apps/api/" + k: v for k, v in repo().items()}
    files["owner-repo-abcdef1/apps/other/.dockerignore"] = "data\n"
    result, seen, _ = execute(files, [PACKAGING_ACCEPT], root="apps/api")
    assert result["job_status"] == "succeeded", result["error"]
    assert seen[0][1]["packaging_candidate"]["root_directory"] == "apps/api"


def test_defer_reuses_loaded_deployment_source_once(analysis_data):
    defer = {**PACKAGING_ACCEPT, "decision": "defer", "plan": "defer"}
    result, seen, downloads = execute(repo(), [defer, response(analysis_data)])
    assert result["job_status"] == "succeeded", result["error"]
    assert len(seen) == 2 and len(downloads) == 1
    assert {r["path"] for r in seen[1][1]["source_evidence"]} == {
        "src/tasks.js",
        "Dockerfile",
        ".dockerignore",
    }
    assert result["execution"]["provider_call_count"] == 2


def test_wrong_plan_cannot_bypass_packaging_review(analysis_data):
    result, seen, _ = execute(repo(), [ACCEPT, response(analysis_data)])
    assert result["job_status"] == "succeeded"
    assert len(seen) == 2
    assert result["execution"]["stages"][0]["error"]["code"] == "INVALID_COMPACT_RESULT"


def test_partial_inline_snapshot_does_not_prove_archive_absence(request_data, analysis_data):
    p = payload(request_data)
    data = p.model_dump()
    data["source_snapshot"]["files"].extend(
        [{"path": k, "content": v} for k, v in repo().items() if k != "src/tasks.js"]
    )
    # Validate replacement instead of relying on assignment coercion.
    p = type(p).model_validate(data)
    create, seen, _ = factory([response(analysis_data)])
    result = asyncio.run(diagnose_with_source(p, create, diagnosis_settings=MODE))
    assert result["job_status"] == "succeeded"
    assert "source_findings" in seen[0][2]["properties"]


def test_packaging_sparql_cannot_join_other_archive_or_scope(request_data):
    result, seen, _ = execute(repo(), [PACKAGING_ACCEPT])
    p = payload(request_data)
    evidence = result["source_analysis"]["evidence"]
    proof = seen[0][1]["packaging_candidate"]
    context = {
        "regular_files": list(repo()),
        "archive_sha256": proof["archive_sha256"],
        "special_files_skipped": False,
        "ignore_path": ".dockerignore",
        "ignore_present": True,
        "root_directory": ".",
        "complete_paths": ["Dockerfile", ".dockerignore"],
    }
    g = build(p.diagnosis, prepare(p.diagnosis), evidence, context)
    assert g["report"]["metrics"]["candidate_count"] == 2
    from rdflib import RDF, Graph, Literal

    c, shapes, _, _ = resources()
    graph = Graph().parse(data=g["report"]["graph_ttl"], format="turtle")
    validate(graph, shapes)
    node = next(graph.subjects(RDF.type, c.IgnorePolicy))
    for prop in (c.scope, c.archiveHash, c.repoPath):
        altered = copy.deepcopy(graph)
        altered.set((node, prop, Literal("unrelated")))
        assert len(altered.query(packaging_query()).graph) == 0
    graph.remove((node, c.allows, None))
    with pytest.raises(ValueError, match="compact_graph_invalid"):
        validate(graph, shapes)


def test_proposed_file_copy_fixes_isolated_node_startup(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for executable repair check")
    code = "import { readFileSync } from 'node:fs';\nconst tasks = JSON.parse(readFileSync(new URL('../data/tasks.json', import.meta.url), 'utf8'));\nconsole.log(tasks.length);"
    files = repo()
    # Use the same accepted evidence pair to select the exact COPY template.
    result, _, _ = execute(files, [PACKAGING_ACCEPT])
    snippet = result["analysis"]["remediation"]["plans"][0]["changes"][0]["snippet"]
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "tasks.js").write_text(code)
    (root / "package.json").write_text('{"type":"module"}')
    before = subprocess.run(
        [node, "src/tasks.js"], cwd=root, capture_output=True, text=True, check=False
    )
    assert before.returncode != 0 and "ENOENT" in before.stderr
    # Apply the proposed literal single-file mapping to this isolated fixture.
    _, owner, src, dst = snippet.split()
    assert owner == "--chown=node:node"
    approved = tmp_path / src
    approved.parent.mkdir()
    approved.write_text('[{"title":"expected task"}]')
    destination = root / dst
    destination.parent.mkdir(parents=True)
    shutil.copyfile(approved, destination)
    after = subprocess.run(
        [node, "src/tasks.js"], cwd=root, capture_output=True, text=True, check=False
    )
    assert after.returncode == 0 and after.stdout.strip() == "1"
    assert destination.read_bytes() == approved.read_bytes()


def test_long_dockerfile_still_cites_exact_insertion_anchor():
    docker = DOCKER.replace("WORKDIR /app", "# explanatory comment\n" * 15 + "WORKDIR /app")
    result, seen, _ = execute(repo(docker=docker), [PACKAGING_ACCEPT])
    assert result["job_status"] == "succeeded", result["error"]
    anchor = seen[0][1]["packaging_candidate"]["insert_after_line"]
    source = result["source_analysis"]
    finding = next(f for f in source["findings"] if f["path"] == "Dockerfile")
    selected = [r for r in source["evidence"] if r["id"] in finding["source_evidence_ids"]]
    assert len(selected) <= 12 and anchor in {r["line"] for r in selected}


def test_overwritten_app_source_is_not_a_safe_mapping(analysis_data):
    files = repo(docker=DOCKER + "COPY --chown=node:node public ./src\n")
    result, seen, _ = execute(files, [response(analysis_data)])
    assert result["job_status"] == "succeeded"
    assert "source_findings" in seen[0][2]["properties"]


def test_ignore_directory_is_not_treated_as_absent_policy(analysis_data):
    files = repo(ignore=None)
    special = tarfile.TarInfo(".dockerignore")
    special.type = tarfile.DIRTYPE
    result, seen, _ = execute(files, [response(analysis_data)], special=special)
    assert result["job_status"] == "succeeded"
    assert "source_findings" in seen[0][2]["properties"]
