from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import CodeParseCache
from app.services.code_index.parser import PARSER_VERSION, ParsedCall, ParsedFile, ParsedInclude, ParsedSymbol


def parser_settings_hash(settings: Settings) -> str:
    payload = "|".join(
        [
            PARSER_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def load_cached_parse(
    db: Session,
    *,
    relative_path: str,
    content_hash: str,
    settings: Settings,
) -> ParsedFile | None:
    if not settings.rag_cache_enabled:
        return None
    cache = db.scalar(
        select(CodeParseCache).where(
            CodeParseCache.content_hash == content_hash,
            CodeParseCache.parser_version == PARSER_VERSION,
            CodeParseCache.settings_hash == parser_settings_hash(settings),
        )
    )
    if cache is None:
        return None
    cache.hit_count += 1
    parsed = parsed_file_from_json(cache.parsed_json)
    if parsed.relative_path == relative_path:
        return parsed
    return ParsedFile(
        relative_path=relative_path,
        line_count=parsed.line_count,
        includes=parsed.includes,
        symbols=parsed.symbols,
        calls=parsed.calls,
    )


def store_cached_parse(
    db: Session,
    *,
    relative_path: str,
    content_hash: str,
    parsed: ParsedFile,
    settings: Settings,
) -> None:
    if not settings.rag_cache_enabled:
        return
    settings_hash = parser_settings_hash(settings)
    current = db.scalar(
        select(CodeParseCache).where(
            CodeParseCache.content_hash == content_hash,
            CodeParseCache.parser_version == PARSER_VERSION,
            CodeParseCache.settings_hash == settings_hash,
        )
    )
    payload = parsed_file_to_json(parsed)
    if current is None:
        db.add(
            CodeParseCache(
                content_hash=content_hash,
                parser_version=PARSER_VERSION,
                settings_hash=settings_hash,
                relative_path=relative_path,
                parsed_json=payload,
                hit_count=0,
            )
        )
        return
    current.relative_path = relative_path
    current.parsed_json = payload


def load_cached_chunk_templates(
    db: Session,
    *,
    content_hash: str,
    settings: Settings,
) -> list[dict] | None:
    if not settings.rag_cache_enabled:
        return None
    cache = db.scalar(
        select(CodeParseCache).where(
            CodeParseCache.content_hash == content_hash,
            CodeParseCache.parser_version == PARSER_VERSION,
            CodeParseCache.settings_hash == parser_settings_hash(settings),
        )
    )
    if cache is None or cache.chunks_json is None:
        return None
    return [dict(item) for item in cache.chunks_json if isinstance(item, dict)]


def store_cached_chunk_templates(
    db: Session,
    *,
    content_hash: str,
    settings: Settings,
    chunks: list[dict],
) -> None:
    if not settings.rag_cache_enabled:
        return
    cache = db.scalar(
        select(CodeParseCache).where(
            CodeParseCache.content_hash == content_hash,
            CodeParseCache.parser_version == PARSER_VERSION,
            CodeParseCache.settings_hash == parser_settings_hash(settings),
        )
    )
    if cache is not None:
        cache.chunks_json = chunks


def parsed_file_to_json(parsed: ParsedFile) -> dict:
    return {
        "relative_path": parsed.relative_path,
        "line_count": parsed.line_count,
        "includes": [include.__dict__ for include in parsed.includes],
        "symbols": [symbol.__dict__ for symbol in parsed.symbols],
        "calls": [call.__dict__ for call in parsed.calls],
    }


def parsed_file_from_json(payload: dict) -> ParsedFile:
    return ParsedFile(
        relative_path=str(payload.get("relative_path") or ""),
        line_count=int(payload.get("line_count") or 1),
        includes=[
            ParsedInclude(target=str(item.get("target") or ""), line=int(item.get("line") or 1))
            for item in payload.get("includes", [])
            if isinstance(item, dict)
        ],
        symbols=[
            ParsedSymbol(
                kind=str(item.get("kind") or "symbol"),
                name=str(item.get("name") or ""),
                signature=item.get("signature") if isinstance(item.get("signature"), str) else None,
                start_line=int(item.get("start_line") or 1),
                end_line=int(item.get("end_line") or item.get("start_line") or 1),
                confidence=float(item.get("confidence") or 0.5),
                source_tool=str(item.get("source_tool") or PARSER_VERSION),
            )
            for item in payload.get("symbols", [])
            if isinstance(item, dict) and item.get("name")
        ],
        calls=[
            ParsedCall(
                caller_name=str(item.get("caller_name") or ""),
                callee_name=str(item.get("callee_name") or ""),
                line=int(item.get("line") or 1),
            )
            for item in payload.get("calls", [])
            if isinstance(item, dict) and item.get("caller_name") and item.get("callee_name")
        ],
    )
