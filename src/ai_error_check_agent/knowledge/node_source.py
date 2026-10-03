"""Restricted Node file-read recognizer, not a general JavaScript parser.

Recognize a direct named node:fs readFileSync import, one unshadowed call at
an observed stack line, and a literal path or a const URL relative to the
observed module. Unsupported syntax/bindings return no match, never a guess.
"""

import posixpath
import re
from dataclasses import dataclass

TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<string>'[^'\\\n]*'|\"[^\"\\\n]*\")"
    r"|(?P<identifier>[A-Za-z_$][A-Za-z0-9_$]*)|(?P<number>\d+)"
    r"|(?P<punct>[{}()\[\].,;:=+*/?!<>%-])"
)
ABS_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,180}\Z")


@dataclass(frozen=True)
class Token:
    value: str
    kind: str
    line: int


def tokenize(code):
    tokens, offset, line = [], 0, 1
    while offset < len(code):
        match = TOKEN.match(code, offset)
        if match is None:
            return None  # Escapes/templates/regex literals and other syntax are unsupported.
        text, kind = match.group(), match.lastgroup
        if kind == "punct" and text in {"/", "*"}:
            return None  # Do not mistake a regex literal for executable import/call text.
        if kind not in {"space", "comment"}:
            tokens.append(Token(text[1:-1] if kind == "string" else text, kind, line))
        line += text.count("\n")
        offset = match.end()
    return tokens


def file_read(lines, runtime_path, stack_line, missing_path):
    if not ABS_PATH.fullmatch(runtime_path) or not ABS_PATH.fullmatch(missing_path):
        return None
    if any(part in {".", "..", ""} for part in missing_path.split("/")[1:]):
        return None
    tokens = tokenize("\n".join(line["text"] for line in lines))
    if not tokens:
        return None
    names = [t.value if t.kind != "string" else "<string>" for t in tokens]
    # These constructs can change bindings/control flow outside this recognizer.
    if set(names) & {"try", "catch", "eval", "globalThis", "global", "Function", "with"}:
        return None
    reads = [
        i for i, t in enumerate(tokens) if t.kind == "identifier" and t.value == "readFileSync"
    ]
    if len(reads) != 2:
        return None
    imported, call = reads
    # Deliberately only this named import; aliasing/multiple bindings fall back.
    if imported < 2 or names[imported - 2 : imported + 3] != [
        "import",
        "{",
        "readFileSync",
        "}",
        "from",
    ]:
        return None
    if tokens[imported + 3].kind != "string" or tokens[imported + 3].value != "node:fs":
        return None
    if names[call + 1 : call + 2] != ["("] or tokens[call].line != stack_line:
        return None
    if call and names[call - 1] == ".":
        return None
    # One path argument, optionally a literal utf8 encoding; no dynamic options.
    end = call + 3
    if names[end : end + 1] == [","]:
        if tokens[end + 1].kind != "string" or tokens[end + 1].value not in {"utf8", "utf-8"}:
            return None
        end += 2
    if names[end : end + 1] != [")"]:
        return None
    argument = tokens[call + 2]
    related = {tokens[call].line}
    if argument.kind == "string":
        resolved = argument.value
    elif argument.kind == "identifier":
        occurrences = [
            i for i, t in enumerate(tokens) if t.kind == "identifier" and t.value == argument.value
        ]
        if len(occurrences) != 2:
            return None
        definition = occurrences[0]
        if definition < 1 or definition >= call:
            return None
        pattern = [
            "const",
            argument.value,
            "=",
            "new",
            "URL",
            "(",
            "<string>",
            ",",
            "import",
            ".",
            "meta",
            ".",
            "url",
            ")",
        ]
        if names[definition - 1 : definition + 13] != pattern:
            return None
        if names.count("URL") != 1:
            return None
        relative = tokens[definition + 5].value
        if not re.fullmatch(r"\.{1,2}/[A-Za-z0-9_./-]+", relative):
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(runtime_path), relative))
        related.update(range(tokens[definition].line, tokens[definition + 12].line + 1))
    else:
        return None
    if resolved != missing_path:
        return None
    refs = [line["id"] for line in lines if line["line"] in related]
    if not refs or len(refs) > 12:
        return None
    return {"target": resolved, "source_ids": refs, "lines": sorted(related)}
