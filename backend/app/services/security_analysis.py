from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.db.models import ReviewFile
from app.schemas.model_response import FindingCategory, FindingSeverity, ReviewFinding


class EvidenceVerdict(str, Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SinkSpec:
    name: str
    kind: str
    dst_arg: int
    len_arg: int | None
    fixed_len: int | None = None


@dataclass(frozen=True)
class BufferWriteEvidence:
    file_path: str
    line: int
    sink: str
    dst: str
    length: str | None
    verdict: EvidenceVerdict = EvidenceVerdict.UNKNOWN


DEFAULT_BUFFER_WRITE_SINKS: dict[str, SinkSpec] = {
    "memcpy": SinkSpec("memcpy", "write_buffer", 0, 2),
    "memmove": SinkSpec("memmove", "write_buffer", 0, 2),
    "memset": SinkSpec("memset", "write_buffer", 0, 2),
    "strcpy": SinkSpec("strcpy", "write_buffer", 0, None),
    "strncpy": SinkSpec("strncpy", "write_buffer", 0, 2),
    "strcat": SinkSpec("strcat", "write_buffer", 0, None),
    "strncat": SinkSpec("strncat", "write_buffer", 0, 2),
    "sprintf": SinkSpec("sprintf", "write_buffer", 0, None),
    "snprintf": SinkSpec("snprintf", "write_buffer", 0, 1),
    "scanf": SinkSpec("scanf", "write_buffer", 1, None),
    "fscanf": SinkSpec("fscanf", "write_buffer", 2, None),
    "sscanf": SinkSpec("sscanf", "write_buffer", 2, None),
    "read": SinkSpec("read", "write_buffer", 1, 2),
    "recv": SinkSpec("recv", "write_buffer", 1, 2),
    "fread": SinkSpec("fread", "write_buffer", 0, 1),
    "WL_ReceiveBytes": SinkSpec("WL_ReceiveBytes", "write_buffer", 0, 1),
    "WirelessModule_ReadFlash": SinkSpec("WirelessModule_ReadFlash", "write_buffer", 1, 2),
    "des3_crypt_ecb": SinkSpec("des3_crypt_ecb", "write_buffer", 2, None, fixed_len=8),
}

_CALL_NAME_RE = re.compile(r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
_COMMENT_RE = re.compile(r"//.*$|/\*.*?\*/")
_ARRAY_WRITE_RE = re.compile(r"(?P<dst>[A-Za-z_][A-Za-z0-9_.>\-]*)\s*\[[^\]]+\]\s*(?:=|\+\+|--)")
_CAST_RE = re.compile(r"\([A-Za-z_][A-Za-z0-9_\s]*(?:\s*\*)+\)")
_FUNCTION_START_RE = re.compile(
    r"^\s*(?!if\b|for\b|while\b|switch\b)(?:[A-Za-z_][\w\s*]*\s+)+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*\{"
)
_FUNCTION_HEADER_RE = re.compile(
    r"^\s*(?!if\b|for\b|while\b|switch\b)(?:[A-Za-z_][\w\s*]*\s+)+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*$"
)


def buffer_write_sink_names() -> tuple[str, ...]:
    return tuple(DEFAULT_BUFFER_WRITE_SINKS)


def has_buffer_write_sink_anchor(line_text: str) -> bool:
    return extract_buffer_write_evidence_from_line("<unknown>", 1, line_text) is not None


def extract_buffer_write_evidence(
    source: ReviewFile,
    line_number: int,
) -> BufferWriteEvidence | None:
    lines = source.source_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return None
    return extract_buffer_write_evidence_from_line(source.relative_path, line_number, lines[line_number - 1])


def extract_buffer_write_evidence_from_line(
    file_path: str,
    line_number: int,
    line_text: str,
) -> BufferWriteEvidence | None:
    sanitized = _strip_comments(line_text)
    for name, args in _iter_calls(sanitized):
        spec = _sink_spec(name)
        if spec is not None:
            if len(args) <= spec.dst_arg:
                continue
            if spec.len_arg is not None and len(args) <= spec.len_arg:
                continue
            length = str(spec.fixed_len) if spec.fixed_len is not None else (
                _normalize_expr(args[spec.len_arg]) if spec.len_arg is not None else None
            )
            return BufferWriteEvidence(
                file_path=file_path,
                line=line_number,
                sink=name,
                dst=_normalize_expr(args[spec.dst_arg]),
                length=length,
            )
        inferred = _infer_unknown_buffer_writer(file_path, line_number, name, args)
        if inferred is not None:
            return inferred
    direct_write = _direct_buffer_write_evidence(file_path, line_number, sanitized)
    if direct_write is not None:
        return direct_write
    return None


def classify_buffer_candidate_line(line_text: str) -> EvidenceVerdict:
    if has_buffer_write_sink_anchor(line_text):
        return EvidenceVerdict.UNKNOWN
    if _looks_like_unknown_buffer_writer(line_text):
        return EvidenceVerdict.UNKNOWN
    return EvidenceVerdict.SAFE


def is_evidence_backed_root_candidate(finding: ReviewFinding) -> bool:
    text = f"{finding.title}\n{finding.description}"
    return (
        finding.category == FindingCategory.BUFFER_OVERFLOW
        and "外部长度" in text
        and "未证明" in text
        and ("写入" in text or "控制" in text)
    )


def detect_protocol_length_buffer_findings(files: Sequence[ReviewFile]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for source in files:
        lines = source.source_text.splitlines()
        for start, end, function_name in _function_windows(lines):
            function_lines = lines[start - 1 : end]
            length_sources = _protocol_length_sources(function_lines, start)
            if not length_sources:
                continue
            emitted_subjects: set[str] = set()
            for line_number in range(start, end + 1):
                evidence = extract_buffer_write_evidence(source, line_number)
                if evidence is None:
                    continue
                subject = _root_dst(evidence.dst)
                if subject in emitted_subjects:
                    continue
                source_line = _matching_length_source(length_sources, function_lines, start, line_number, evidence)
                if source_line is None:
                    continue
                if _has_protocol_length_capacity_guard(function_lines, start, line_number):
                    continue
                emitted_subjects.add(subject)
                findings.append(
                    ReviewFinding(
                        severity=FindingSeverity.HIGH,
                        category=FindingCategory.BUFFER_OVERFLOW,
                        title="协议长度缺少边界校验",
                        description=(
                            f"{function_name} 使用外部长度控制 {evidence.sink} 写入 {subject}，"
                            "未证明长度受目标缓冲区容量约束。"
                        ),
                        file_path=source.relative_path,
                        line=source_line,
                    )
                )
    return findings


def _iter_calls(line_text: str) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    cursor = 0
    while True:
        match = _CALL_NAME_RE.search(line_text, cursor)
        if not match:
            return calls
        name = match.group("name")
        open_index = match.end() - 1
        close_index = _find_matching_paren(line_text, open_index)
        if close_index is None:
            cursor = match.end()
            continue
        if name in {"if", "for", "while", "switch", "sizeof", "return"}:
            cursor = match.end()
            continue
        calls.append((name, _split_args(line_text[open_index + 1 : close_index])))
        cursor = close_index + 1


def _find_matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_args(argument_text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    for char in argument_text:
        if char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        current.append(char)
    if current or argument_text.strip():
        args.append("".join(current).strip())
    return args


def _looks_like_unknown_buffer_writer(line_text: str) -> bool:
    sanitized = _strip_comments(line_text)
    for name, args in _iter_calls(sanitized):
        if name in DEFAULT_BUFFER_WRITE_SINKS:
            continue
        if _infer_unknown_buffer_writer("<unknown>", 1, name, args) is not None:
            return True
    return False


def _infer_unknown_buffer_writer(
    file_path: str,
    line_number: int,
    name: str,
    args: Sequence[str],
) -> BufferWriteEvidence | None:
    lowered = name.lower()
    if not any(token in lowered for token in ("recv", "receive", "read", "copy", "crypt", "decrypt", "load")):
        return None
    if len(args) < 2:
        return None
    dst_index = next((index for index, arg in enumerate(args) if _looks_like_pointer_arg(arg)), None)
    len_index = next((index for index, arg in enumerate(args) if _looks_like_length_arg(arg)), None)
    if dst_index is None or len_index is None or dst_index == len_index:
        return None
    return BufferWriteEvidence(
        file_path=file_path,
        line=line_number,
        sink=name,
        dst=_normalize_expr(args[dst_index]),
        length=_normalize_expr(args[len_index]),
    )


def _direct_buffer_write_evidence(
    file_path: str,
    line_number: int,
    line_text: str,
) -> BufferWriteEvidence | None:
    array_write = _ARRAY_WRITE_RE.search(line_text)
    if array_write is not None:
        return BufferWriteEvidence(
            file_path=file_path,
            line=line_number,
            sink="array_write",
            dst=_normalize_expr(array_write.group("dst")),
            length=None,
        )
    if "=" not in line_text:
        return None
    lhs = line_text.split("=", 1)[0].strip()
    if not lhs.startswith("*"):
        return None
    dst = _normalize_pointer_lhs(lhs)
    if not dst or ("->" not in dst and "[" not in dst and "." not in dst):
        return None
    return BufferWriteEvidence(
        file_path=file_path,
        line=line_number,
        sink="pointer_write",
        dst=dst,
        length=None,
    )


def _normalize_pointer_lhs(lhs: str) -> str:
    normalized = lhs.strip()
    while normalized.startswith("*"):
        normalized = normalized[1:].strip()
    normalized = _CAST_RE.sub("", normalized)
    normalized = normalized.strip("() ")
    return _normalize_expr(normalized)


def _sink_spec(name: str) -> SinkSpec | None:
    spec = DEFAULT_BUFFER_WRITE_SINKS.get(name)
    if spec is not None:
        return spec
    lowered = name.lower()
    for candidate, candidate_spec in DEFAULT_BUFFER_WRITE_SINKS.items():
        if candidate.lower() == lowered:
            return candidate_spec
    return None


def _looks_like_pointer_arg(arg: str) -> bool:
    normalized = _normalize_expr(arg)
    return (
        normalized.startswith("&")
        or "->" in normalized
        or re.search(r"\+\s*[A-Za-z_][A-Za-z0-9_]*$", normalized) is not None
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized) is not None
    )


def _looks_like_length_arg(arg: str) -> bool:
    normalized = _normalize_expr(arg)
    if re.fullmatch(r"\d+", normalized):
        return True
    return any(token in normalized.lower() for token in ("len", "length", "size", "count", "message"))


def _function_windows(lines: list[str]) -> list[tuple[int, int, str]]:
    windows: list[tuple[int, int, str]] = []
    index = 0
    while index < len(lines):
        match = _FUNCTION_START_RE.match(lines[index])
        body_start = index
        function_name = match.group("name") if match else None
        if match is None:
            header_match = _FUNCTION_HEADER_RE.match(lines[index])
            next_index = index + 1
            while header_match and next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if header_match and next_index < len(lines) and lines[next_index].strip().startswith("{"):
                body_start = next_index
                function_name = header_match.group("name")
        if not match:
            if function_name is None:
                index += 1
                continue
        depth = 0
        end = body_start
        for end in range(body_start, len(lines)):
            sanitized = _strip_comments(lines[end])
            depth += sanitized.count("{")
            depth -= sanitized.count("}")
            if depth <= 0:
                break
        windows.append((index + 1, end + 1, function_name))
        index = end + 1
    return windows


def _protocol_length_sources(function_lines: list[str], start_line: int) -> dict[str, int]:
    sources: dict[str, int] = {}
    decrypt_outputs: set[str] = set()
    for offset, line in enumerate(function_lines):
        for _name, args in _iter_calls(_strip_comments(line)):
            for arg in args:
                match = re.fullmatch(r"&\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)", arg.strip())
                if match:
                    decrypt_outputs.add(match.group("name"))
        assign = re.search(r"(?P<lhs>[A-Za-z_][A-Za-z0-9_.>\-]*)\s*=\s*(?P<rhs>[^;]+);", line)
        if not assign:
            continue
        lhs = _normalize_expr(assign.group("lhs"))
        rhs = _normalize_expr(assign.group("rhs"))
        lowered_lhs = lhs.lower()
        if "messagelen" not in lowered_lhs and not lowered_lhs.endswith("len"):
            continue
        if "<<" in rhs and lhs in rhs:
            sources[lhs] = start_line + offset
        elif (
            re.search(r"\b(?:rev|buf|pbuf|rb)\s*\[", rhs) is not None
            or any(re.search(rf"\b{re.escape(name)}\b", rhs) for name in decrypt_outputs)
        ):
            sources.setdefault(lhs, start_line + offset)
    return sources


def _matching_length_source(
    length_sources: dict[str, int],
    function_lines: list[str],
    start_line: int,
    sink_line: int,
    evidence: BufferWriteEvidence,
) -> int | None:
    nearby_loop_text = "\n".join(
        function_lines[max(0, sink_line - start_line - 8) : max(0, sink_line - start_line + 1)]
    )
    for length_name, source_line in length_sources.items():
        if source_line >= sink_line:
            continue
        length_uses_source = length_name in (evidence.length or "")
        loop_uses_source = length_name in _normalize_expr(nearby_loop_text)
        writes_protocol_payload = "uDataPar" in _root_dst(evidence.dst)
        writes_struct_member = "->" in _root_dst(evidence.dst)
        if not writes_protocol_payload and not writes_struct_member:
            continue
        if length_uses_source or (writes_protocol_payload and loop_uses_source):
            return source_line
    return None


def _has_protocol_length_capacity_guard(
    function_lines: list[str],
    start_line: int,
    sink_line: int,
) -> bool:
    prior = "\n".join(_normalize_expr(line) for line in function_lines[: max(0, sink_line - start_line)])
    if "DATA_REV_SERVER_MAX" not in prior and "sizeof" not in prior:
        return False
    guard_patterns = (
        r"uMessageLen\s*-\s*(?:18|16)\s*<=\s*(?:DATA_REV_SERVER_MAX|sizeof\s*\([^)]*uDataPar[^)]*\))",
        r"uMessageLen\s*<=\s*(?:DATA_REV_SERVER_MAX\s*\+\s*(?:18|16)|(?:18|16)\s*\+\s*DATA_REV_SERVER_MAX)",
        r"uMessageLen\s*>\s*(?:DATA_REV_SERVER_MAX\s*\+\s*(?:18|16)|(?:18|16)\s*\+\s*DATA_REV_SERVER_MAX)",
        r"uMessageLen\s*-\s*(?:18|16)\s*>\s*DATA_REV_SERVER_MAX",
    )
    return any(re.search(pattern, prior) for pattern in guard_patterns)


def _root_dst(dst: str) -> str:
    normalized = _normalize_expr(dst)
    normalized = normalized.lstrip("&*(").rstrip(")")
    normalized = re.split(r"\+|\[", normalized, maxsplit=1)[0]
    return normalized or dst.strip()


def _normalize_expr(expression: str) -> str:
    normalized = re.sub(r"\s+", "", expression.strip())
    normalized = re.sub(r"^\((?:uint8|uint16|uint32|char|unsignedchar|void)\*?\)", "", normalized)
    return normalized


def _strip_comments(line_text: str) -> str:
    return _COMMENT_RE.sub("", line_text)
