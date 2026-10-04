"""Narrow Docker COPY omission proof over a complete, caller-supplied archive.

This is deliberately not a Docker interpreter. Unknown syntax, base images,
ignore patterns, file mutations or ambiguous mappings yield no candidate.
No repository code runs and no data-file content is read.
"""

import posixpath
import re

PATH = re.compile(r"[A-Za-z0-9_./-]{1,180}\Z")
BASE = re.compile(
    r"node:[0-9]+(?:\.[0-9]+){0,2}-(?:alpine[0-9.]*|bookworm(?:-slim)?|bullseye(?:-slim)?)\Z"
)


def path_token(value, *, absolute=False):
    if not PATH.fullmatch(value) or value.startswith("-"):
        return None
    if value.startswith("/") != absolute:
        return None
    if value in {".", "./"} and not absolute:
        return "."
    value = value.removeprefix("./").rstrip("/")
    if value == "." and not absolute:
        return value
    parts = value.split("/")[1:] if absolute else value.split("/")
    if not parts or any(p in {"", ".", ".."} for p in parts):
        return None
    return value


def instructions(lines):
    """Join only ordinary backslash continuations; reject parser directives."""
    result, pending, first = [], "", None
    for row in lines:
        text = row["text"].strip()
        if text.startswith("#"):
            if re.match(r"#\s*(?:syntax|escape|check)\s*=", text, re.IGNORECASE):
                return None
            continue
        if not text:
            continue
        first = first or row
        continued = text.endswith("\\")
        text = text[:-1].rstrip() if continued else text
        if "\\" in text or "`" in text or "<<" in text:
            return None
        pending += (" " if pending else "") + text
        if continued:
            continue
        match = re.fullmatch(r"([A-Za-z]+)\s+(.+)", pending)
        if not match:
            return None
        result.append((match[1].upper(), match[2], first))
        pending, first = "", None
    return None if pending else result


def ignore_allows(lines, paths):
    # Literal exclusions only. Negation/globs/escapes are intentionally unknown.
    for row in lines:
        pattern = row["text"].strip()
        if not pattern or pattern.startswith("#"):
            continue
        pattern = path_token(pattern.strip("/"))
        if pattern is None:
            return False
        if pattern == ".":
            continue  # Docker's historical special case.
        if any(path == pattern or path.startswith(pattern + "/") for path in paths):
            return False
    return True


def prove_copy_omission(source_lines, context, source_path, runtime_path, target):
    if not context or context.get("special_files_skipped"):
        return None
    if not re.fullmatch(r"[a-f0-9]{64}", context.get("archive_sha256", "")):
        return None
    files = set(context["regular_files"])
    if any(
        "/".join(path.split("/")[:i]) in files
        for path in files
        for i in range(1, len(path.split("/")))
    ):
        return None
    ignore_path = context["ignore_path"]
    required = {"Dockerfile"}
    if context["ignore_present"] and not context.get("empty_ignore"):
        required.add(ignore_path)
    if not required <= set(context.get("complete_paths", [])):
        return None
    if required & set(context.get("masked_paths", [])):
        return None
    docker = [r for r in source_lines if r["path"] == "Dockerfile"]
    ignore = [r for r in source_lines if r["path"] == ignore_path]
    parsed = instructions(docker)
    if not parsed or parsed[0][0] != "FROM" or not BASE.fullmatch(parsed[0][1]):
        return None
    workdir, copies, command, user = None, [], None, None
    for op, arg, row in parsed[1:]:
        if op == "WORKDIR":
            if workdir or copies:
                return None
            workdir = path_token(arg, absolute=True)
            if not workdir:
                return None
        elif op == "COPY":
            if not workdir:
                return None
            parts = arg.split()
            owner = ""
            if parts and parts[0].startswith("--chown="):
                owner = parts.pop(0)
                if not re.fullmatch(r"--chown=(?:node:node|[0-9]+:[0-9]+)", owner):
                    return None
            if len(parts) != 2:
                return None
            source = path_token(parts[0])
            dest = path_token(parts[1], absolute=parts[1].startswith("/"))
            if source is None or dest is None or source == ".":
                return None  # COPY . may already supply the target.
            directory = any(p.startswith(source + "/") for p in files)
            if source not in files and not directory:
                return None
            if directory and source in files:
                return None  # An inconsistent tree is not buildable.
            dest = posixpath.normpath(posixpath.join(workdir, dest))
            if not directory and (parts[1].endswith("/") or dest == workdir):
                dest = posixpath.join(dest, posixpath.basename(source))
            copies.append((source, dest, directory, owner, row))
        elif op == "CMD":
            # Node startup with a literal script, no shell/launcher generation.
            if command is not None or not re.fullmatch(
                r'\[\s*"node"\s*,\s*"(?:\./)?[A-Za-z0-9_/-]+\.(?:js|mjs|cjs)"\s*\]', arg
            ):
                return None
            command = arg
        elif op == "USER":
            if user is not None or arg != "node":
                return None
            user = arg
        elif op in {"ENV", "EXPOSE", "LABEL", "HEALTHCHECK"}:
            continue
        else:
            return None  # RUN/ADD/ARG/ENTRYPOINT/VOLUME/multiple FROM/etc.
    if not workdir or not copies or not command or user != "node":
        return None
    # Both observed paths must map to this exact build root, not a suffix guess.
    if runtime_path != posixpath.join(workdir, source_path) or not target.startswith(workdir + "/"):
        return None
    repo_target = target[len(workdir) + 1 :]
    if path_token(repo_target) is None or repo_target not in files or repo_target == source_path:
        return None
    if not ignore_allows(ignore, [repo_target, source_path]):
        return None
    mapped_source, owner = False, None
    for src, dst, directory, chown, _ in copies:
        if not ignore_allows(ignore, [src]):
            return None
        source_match = (
            directory
            and source_path.startswith(src + "/")
            and posixpath.join(dst, source_path[len(src) + 1 :]) == runtime_path
        ) or (not directory and src == source_path and dst == runtime_path)
        if source_match:
            mapped_source, owner = True, chown
        elif dst == runtime_path or (directory and runtime_path.startswith(dst + "/")):
            return None  # Another COPY may replace the source we just inspected.
        # Any COPY that could supply/cover the target, or put a file at a parent,
        # makes an omission claim unsafe. Renamed source directories count too.
        if dst == target or (directory and target.startswith(dst + "/")):
            return None
        if not directory and target.startswith(dst + "/"):
            return None
    if not mapped_source or owner != "--chown=node:node":
        return None
    # Copy only the missing file. This avoids accidentally embedding other data
    # or credentials from the directory; Docker creates missing parent dirs.
    snippet = f"COPY --chown=node:node {repo_target} ./{repo_target}"
    anchor = copies[-1][4]
    if any(
        op == "COPY" and row["line"] == anchor["line"] and "\\" in row["text"]
        for op, _, row in parsed
    ):
        return None  # Keep the insertion location unambiguous.
    return {
        "id": "P1",
        "kind": "docker_copy_omission",
        "rule": "docker-copy-omission.v1",
        "repo_path": repo_target,
        "target": target,
        "dockerfile": "Dockerfile",
        "archive_sha256": context["archive_sha256"],
        "root_directory": context["root_directory"],
        "ignore_path": ignore_path if context["ignore_present"] else None,
        "ignore_state": "empty" if context.get("empty_ignore") else "read" if ignore else "absent",
        "insert_after_line": anchor["line"],
        "anchor": anchor["text"],
        "snippet": snippet,
        "source_evidence_ids": [r["id"] for r in docker],
        "ignore_evidence_ids": [r["id"] for r in ignore],
        "claim": "제공 아카이브에 대상 파일이 있으나, 이 Dockerfile의 명시적 COPY에는 대상 경로로 공급하는 지시가 없다. 실제 배포에 동일 빌드 문맥·Dockerfile을 사용했는지는 미확인이다.",
    }
