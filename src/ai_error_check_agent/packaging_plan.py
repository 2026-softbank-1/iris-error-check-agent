"""Assemble a grounded, conditional Dockerfile edit using the existing contract."""


def apply_packaging_plan(value, proof, packet, uncertainty):
    target, repo_path = proof["target"], proof["repo_path"]
    refs = packet["candidate"]["evidence_ids"]
    value["hypotheses"].append(
        {
            "id": "H2",
            "category": "other",
            "support_level": "supported",
            "statement": f"제공 아카이브에는 {repo_path} 파일이 있지만 Dockerfile의 명시적 COPY에 {target} 공급이 빠져 있습니다. 동일 빌드 설정을 사용했다면 ENOENT의 배포 원인 후보입니다.",
            "observation_ids": ["O1"],
            "evidence_ids": refs,
            "counter_evidence_ids": [],
            "uncertainty": "실제 배포가 같은 커밋·Dockerfile·빌드 문맥을 사용했는지, 마운트·시작 시 초기화가 경로를 바꿨는지는 미확인입니다. "
            + uncertainty,
        }
    )
    value["hypotheses"][0]["uncertainty"] = (
        "읽기 실패는 관찰됐으며, 제공 빌드 설정과 연결한 배포 원인 후보는 H2입니다. " + uncertainty
    )
    value["limitations"][1] = (
        "아카이브 목록, 스택의 소스, Dockerfile 및 적용되는 제외 규칙을 제한된 문법으로 대조했습니다. "
        "실제 이미지 빌드·기동·데이터 형식 검증은 실행하지 않았습니다."
    )
    value["next_checks"] = [
        {
            "id": "C1",
            "target": "Dockerfile·빌드 문맥·이미지와 파일 공급 계약",
            "method": f"실패 배포의 커밋·Dockerfile·컨텍스트 루트가 제공 소스와 같은지 확인하고, `{repo_path}` 파일이 이미지에 포함할 정적 초기 파일인지 또는 외부 마운트·영속 데이터인지 구분합니다.",
            "purpose": "COPY 누락 수정의 적용 가능성을 확인하고 외부 데이터 공급 문제와 구분합니다.",
            "hypothesis_ids": ["H1", "H2"],
        }
    ]
    value["missing_information"] = [
        {
            "requested_data": "실패 배포의 빌드 문맥·Dockerfile 선택·이미지 식별 정보, 마운트 설정, 해당 파일의 정적 초기 데이터·영속성 계약 및 후속 시작 로그",
            "reason": "저장소의 COPY 누락 후보가 실제 배포 원인인지와 이미지에 데이터를 포함해도 되는지를 확인해야 합니다.",
        }
    ]
    value["remediation"] = {
        "status": "proposed",
        "reason": uncertainty,
        "plans": [
            {
                "id": "R1",
                "title": "Dockerfile에 누락된 필수 파일 COPY 추가",
                "hypothesis_ids": ["H1", "H2"],
                "evidence_ids": refs,
                "apply_when": [
                    "실패한 배포가 제공 커밋의 Dockerfile과 프로젝트 루트를 빌드 문맥으로 사용했고, 해당 경로를 가리는 마운트나 별도 초기화가 없는 경우에 적용합니다.",
                    f"`{repo_path}` 파일이 이미지에 포함할 승인된 정적 초기 파일이며, 실제 형식·항목 계약과 비밀정보 포함 여부를 검토한 경우에만 적용합니다. 영속 운영 데이터나 외부 공급 계약이면 적용하지 않습니다.",
                ],
                "changes": [
                    {
                        "kind": "configuration",
                        "target": "Dockerfile",
                        "target_known": True,
                        "instruction": f"제공된 Dockerfile {proof['insert_after_line']}줄의 `{proof['anchor']}` 바로 뒤에 아래 한 줄을 추가합니다. 스냅샷과 현재 파일이 같은지 확인하고 기존 COPY를 유지합니다. 빈 데이터 생성이나 실행 중 파일 덮어쓰기는 하지 않습니다.",
                        "language": "text",
                        "snippet_kind": "template",
                        "snippet": proof["snippet"],
                        "placeholders": [],
                    }
                ],
                "verification": [
                    {
                        "instruction": f"확인된 프로젝트 루트에서 같은 빌드 설정으로 새 이미지를 만듭니다. 예: docker build --no-cache -t iris-copy-check -f Dockerfile . 이후 격리된 새 컨테이너의 node 사용자로 {target} 읽기·내용 일치·실제 데이터 형식을 검사합니다.",
                        "expected_result": f"빌드가 성공하고 `{target}` 파일 내용이 승인된 원본 `{repo_path}`의 내용과 일치하며 앱 사용자 권한으로 읽고 파싱할 수 있습니다.",
                    },
                    {
                        "instruction": "운영과 같은 마운트·환경 조건의 격리 환경에서 새 이미지의 앱을 시작하고 시작 로그·상태 검사·해당 파일을 사용하는 기능을 확인한 뒤 정규 재배포 절차를 따릅니다.",
                        "expected_result": "동일 ENOENT가 재발하지 않고 시작·상태 검사와 데이터 기능이 통과합니다. 마운트로 파일이 가려지면 COPY만으로 해결됐다고 판정하지 않습니다.",
                    },
                ],
                "rollback": [
                    "추가한 COPY 한 줄을 되돌리고 이전에 검증한 이미지·배포 설정으로 복귀합니다. 초기화 뒤 생성·변경된 영속 데이터는 먼저 보존합니다. 실패한 이전 이미지로 복귀하면 ENOENT가 재발할 수 있습니다."
                ],
                "risks": [
                    "이미지에 포함한 초기 파일은 이미지 레이어에 남습니다. 비밀값이나 운영 데이터를 포함하지 말고, 마운트 가림·실행 권한·실제 데이터 계약을 확인합니다. 이 변경은 외부 데이터 복구를 대신하지 않습니다."
                ],
            }
        ],
    }
    for code in packet.get("deployment_code", []):
        lines = code["lines"]
        if not lines:
            continue
        # Public finding anchors are bounded to 12; all complete source lines
        # remain available in source_analysis.evidence for independent review.
        if len(lines) <= 12:
            anchors = lines
        else:
            selected = {lines[0][1], lines[-1][1]}
            if code["path"] == "Dockerfile":
                selected.add(proof["insert_after_line"])
            selected.update(row[1] for row in lines if len(selected) < 12)
            anchors = [row for row in lines if row[1] in selected]
        description = (
            f"전체 제공 Dockerfile을 정적 대조한 결과 `{target}` 경로로 파일을 공급하는 COPY가 없습니다. {proof['insert_after_line']}줄 뒤에 대상 파일 COPY를 추가하는 후보입니다."
            if code["path"] == "Dockerfile"
            else f"이 Dockerfile에 적용되는 제공 제외 규칙에서 `{repo_path}` 파일을 제외하지 않는지 제한된 리터럴 패턴으로 확인했습니다."
        )
        value["source_findings"]["findings"].append(
            {
                "path": code["path"],
                "start_line": anchors[0][1],
                "end_line": anchors[-1][1],
                "explanation": description,
                "evidence_ids": refs,
                "source_evidence_ids": [r[0] for r in anchors],
                "hypothesis_ids": ["H2"],
            }
        )
