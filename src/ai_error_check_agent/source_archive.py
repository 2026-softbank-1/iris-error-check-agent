"""Bounded S3 downloads and tar reads. Never extract repository files to disk."""

import asyncio
import gzip
import hashlib
import io
import logging
import re
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .errors import DiagnosisError
from .preprocessing import normalize
from .source_contracts import SourceFile, SourceSnapshot, safe_source_path

S3_HOST = re.compile(r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?\Z")
GITHUB_ROOT = re.compile(r".+-[a-fA-F0-9]{7,40}\Z")
EXCLUDED_DIRS = {"node_modules", "vendor", ".venv", "venv", ".git", "__pycache__", "dist", "build"}
COMMON_FILES = (
    "Dockerfile",
    "package.json",
    "tsconfig.json",
    "pyproject.toml",
    "requirements.txt",
    "iris.json",
    "src/main.py",
    "src/app.py",
    "app.py",
    "src/index.ts",
)


class _RedactS3Query(logging.Filter):
    def filter(self, record):
        # HTTPX logs request URLs at INFO; a presigned query is a credential.
        if isinstance(record.args, tuple):
            record.args = tuple(
                item.copy_with(query=b"REDACTED")
                if isinstance(item, httpx.URL) and S3_HOST.fullmatch(item.host) and item.query
                else item
                for item in record.args
            )
        return True


@dataclass(frozen=True)
class ArchivePolicy:
    allowed_hosts: tuple[str, ...] = ()
    max_download_bytes: int = 32 * 1024 * 1024
    max_expanded_bytes: int = 128 * 1024 * 1024
    max_members: int = 5000
    max_file_bytes: int = 65_536
    download_timeout: float = 20.0

    def __post_init__(self):
        if any(not S3_HOST.fullmatch(host) for host in self.allowed_hosts):
            raise DiagnosisError(
                "INVALID_CONFIG", "소스 허용 호스트는 S3 호스트 이름이어야 합니다."
            )
        if (
            min(
                self.max_download_bytes,
                self.max_expanded_bytes,
                self.max_members,
                self.max_file_bytes,
                self.download_timeout,
            )
            <= 0
        ):
            raise ValueError("archive limits must be positive")


def validated_url(source, policy):
    url = source.download_url.get_secret_value()
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        valid = (
            parts.scheme == "https"
            and S3_HOST.fullmatch(host)
            and parts.port in {None, 443}
            and not parts.username
            and not parts.password
            and not parts.fragment
            and parts.path not in {"", "/"}
            and not re.search(r"[\x00-\x20\x7f\\]", url)
            and (not policy.allowed_hosts or host in policy.allowed_hosts)
        )
    except ValueError:
        valid = False
    if not valid:
        raise DiagnosisError("SOURCE_URL_NOT_ALLOWED", "허용된 HTTPS S3 파일 URL이 아닙니다.")
    if source.expires_at and source.expires_at <= datetime.now(UTC):
        raise DiagnosisError(
            "SOURCE_URL_EXPIRED", "소스 다운로드 URL이 만료됐습니다. 새 URL이 필요합니다."
        )
    return url


async def download_archive(source, policy, *, transport=None):
    url = validated_url(source, policy)
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, _RedactS3Query) for item in logger.filters):
        logger.addFilter(_RedactS3Query())
    try:
        async with (
            asyncio.timeout(policy.download_timeout),
            httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=policy.download_timeout,
                headers={"Accept-Encoding": "identity"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code == 403:
                raise DiagnosisError(
                    "SOURCE_ACCESS_DENIED", "소스 URL의 권한 또는 만료 여부를 확인하세요."
                )
            if response.status_code != 200:
                raise DiagnosisError(
                    "SOURCE_DOWNLOAD_FAILED", "소스 파일을 다운로드하지 못했습니다."
                )
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise DiagnosisError(
                    "SOURCE_ENCODING_UNSUPPORTED",
                    "HTTP 압축이 없는 원본 tar.gz URL이 필요합니다.",
                )
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > policy.max_download_bytes:
                        raise DiagnosisError(
                            "SOURCE_TOO_LARGE", "소스 압축 파일 크기 한도를 초과했습니다."
                        )
                except ValueError:
                    raise DiagnosisError(
                        "SOURCE_DOWNLOAD_FAILED", "소스 응답 크기 정보가 잘못됐습니다."
                    ) from None
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                if len(body) + len(chunk) > policy.max_download_bytes:
                    raise DiagnosisError(
                        "SOURCE_TOO_LARGE", "소스 압축 파일 크기 한도를 초과했습니다."
                    )
                body.extend(chunk)
            return bytes(body)
    except (TimeoutError, httpx.TimeoutException):
        raise DiagnosisError("SOURCE_TIMEOUT", "소스 다운로드 제한시간을 초과했습니다.") from None
    except httpx.HTTPError:
        raise DiagnosisError("SOURCE_DOWNLOAD_FAILED", "소스 파일 연결에 실패했습니다.") from None


def member_path(name):
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if (
        not name
        or name.startswith("/")
        or re.search(r"[\\:\x00-\x1f\x7f]", name)
        or any(p in {"", ".", ".."} for p in name.split("/"))
    ):
        raise DiagnosisError("UNSAFE_ARCHIVE", "압축 파일에 허용되지 않은 경로가 있습니다.")
    return name


def read_archive(blob, source, requests, bundle, policy):
    """Decompress under a hard byte bound, then read only chosen regular UTF-8 files."""
    limits = []
    try:
        with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as expanded:
            total = 0
            with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as compressed:
                while chunk := compressed.read(min(65_536, policy.max_expanded_bytes - total + 1)):
                    total += len(chunk)
                    if total > policy.max_expanded_bytes:
                        raise DiagnosisError(
                            "SOURCE_TOO_LARGE", "압축 해제 크기 한도를 초과했습니다."
                        )
                    expanded.write(chunk)
            expanded.seek(0)
            with tarfile.open(fileobj=expanded, mode="r:") as archive:
                members, seen = [], set()
                skipped_special = False
                for count, member in enumerate(archive, 1):
                    if count > policy.max_members:
                        raise DiagnosisError(
                            "SOURCE_TOO_MANY_FILES", "압축 파일의 항목 수 한도를 초과했습니다."
                        )
                    if member.name in {".", "./"} and member.isdir():
                        continue
                    name = member_path(member.name)
                    if name in seen:
                        raise DiagnosisError("UNSAFE_ARCHIVE", "압축 파일에 중복 경로가 있습니다.")
                    seen.add(name)
                    if member.isdir():
                        continue
                    if not member.isfile() or member.issparse():
                        skipped_special = True
                        continue
                    if member.size < 0 or member.offset_data + member.size > total:
                        raise DiagnosisError(
                            "INVALID_ARCHIVE", "압축 파일의 내용 길이가 잘못됐습니다."
                        )
                    members.append((name, member))
                if skipped_special:
                    limits.append("심볼릭 링크·하드 링크·특수 파일은 읽지 않았습니다.")
                # GitHub tarballs add an owner-repository-SHA folder. Do not strip arbitrary
                # sole directories such as src/ from ordinary repository-root archives.
                tops = {name.split("/", 1)[0] for name, _ in members}
                prefix = ""
                if len(tops) == 1 and GITHUB_ROOT.fullmatch(next(iter(tops))):
                    prefix = next(iter(tops)) + "/"
                root = "" if source.root_directory == "." else source.root_directory + "/"
                available = {}
                for name, member in members:
                    repo_path = name.removeprefix(prefix)
                    if not repo_path.startswith(root):
                        continue
                    path = repo_path[len(root) :]
                    if any(p.lower() in EXCLUDED_DIRS for p in path.split("/")):
                        continue
                    try:
                        safe_source_path(path)
                    except ValueError:
                        continue
                    available[path] = member
                if not available:
                    raise DiagnosisError(
                        "SOURCE_FILES_UNAVAILABLE",
                        "프로젝트 경로에 읽을 수 있는 소스 파일이 없습니다.",
                    )
                selected = []
                for request in requests:
                    path = request["path"]
                    if path not in available and root and path.startswith(root):
                        path = path[len(root) :]
                    if path not in available:
                        limits.append(f"{request['path']}: 압축 파일의 프로젝트 경로에 없습니다.")
                        continue
                    selected.append({**request, "path": path})
                if not requests:
                    log_text = "\n".join(line.text for line in bundle.lines)
                    mentioned = sorted(path for path in available if path in log_text)
                    defaults = [path for path in COMMON_FILES if path in available]
                    candidates = list(dict.fromkeys(mentioned + defaults))[:3]
                    for path in candidates:
                        selected.append(
                            {
                                "path": path,
                                "start_line": 1,
                                "end_line": 40,
                                "reason": "파일 범위가 없어 서버가 로그 언급·일반 설정 파일을 우선 선택했습니다.",
                                "evidence_ids": [bundle.lines[0].id],
                            }
                        )
                    limits.append(
                        "정확한 파일 범위가 없어 로그 언급·일반 설정 파일의 앞부분을 제한적으로 선택했습니다."
                    )
                files, ranges, selected_paths = [], [], set()
                for request in selected[:3]:
                    path = request["path"]
                    if path in selected_paths:
                        continue
                    selected_paths.add(path)
                    member = available[path]
                    if member.size > min(policy.max_file_bytes, 65_536):
                        limits.append(f"{path}: 파일별 읽기 크기 한도를 초과하여 제외했습니다.")
                        continue
                    with archive.extractfile(member) as handle:
                        raw = handle.read(min(policy.max_file_bytes, 65_536) + 1)
                    try:
                        content = raw.decode("utf-8-sig")
                    except UnicodeError:
                        limits.append(f"{path}: UTF-8 텍스트가 아니어서 제외했습니다.")
                        continue
                    if "\x00" in content:
                        limits.append(f"{path}: 바이너리 내용을 포함하여 제외했습니다.")
                        continue
                    physical = normalize(content).split("\n")
                    if physical[-1] == "":
                        physical.pop()
                    count = len(physical)
                    if request["start_line"] > count:
                        limits.append(f"{path}: 요청 시작 줄이 실제 파일 범위를 초과합니다.")
                        continue
                    end = min(request["end_line"], count)
                    if end != request["end_line"]:
                        limits.append(
                            f"{path}: 요청 끝 줄을 실제 파일 마지막 줄({end})로 제한했습니다."
                        )
                    files.append(SourceFile(path=path, content=content))
                    ranges.append({**request, "end_line": end})
                snapshot = (
                    SourceSnapshot(commit_sha=source.commit_sha, files=files) if files else None
                )
                return snapshot, ranges, limits, hashlib.sha256(blob).hexdigest()
    except DiagnosisError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError, RecursionError):
        raise DiagnosisError("INVALID_ARCHIVE", "소스 tar.gz 파일을 읽을 수 없습니다.") from None


class ArchiveLoader:
    def __init__(self, source, *, policy=None, transport=None):
        self.source = source
        self.policy = policy or ArchivePolicy()
        self.transport = transport
        self.archive_sha256 = None

    async def __call__(self, requests, bundle):
        blob = await download_archive(self.source, self.policy, transport=self.transport)
        snapshot, ranges, limits, digest = await asyncio.to_thread(
            read_archive, blob, self.source, requests, bundle, self.policy
        )
        self.archive_sha256 = digest
        return snapshot, ranges, limits
