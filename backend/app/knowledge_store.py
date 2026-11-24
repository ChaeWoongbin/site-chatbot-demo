from __future__ import annotations

import hashlib
import io
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4



ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf"}
MIME_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
ACCESS_SCOPES = {"general", "dental", "medical", "dental_medical"}
TAXONOMY_NODE_TYPES = {"category", "product_family", "model"}
DOCUMENT_TYPES = {
    "company", "catalog", "product_guide", "manual", "service", "policy", "other",
}
CLASSIFICATION_STATUSES = {
    "unclassified", "pending_review", "reviewed", "failed",
}
LOGGER = logging.getLogger(__name__)
STAGED_DELETE_PATTERN = re.compile(
    r"^\.deleting-[0-9a-f]{32}-[0-9a-f]{32}\.(?:md|txt|pdf)$"
)
STAGED_DELETE_TTL_SECONDS = 60 * 60

_SEARCH_STOP_TOKENS = {
    "누구", "누구야", "누가", "뭐야", "무엇", "무엇인가", "알려줘", "알려주세요",
    "어디", "어디야", "어디인가", "해줘", "해주세요",
}
def normalize_access_scope(value: str) -> str:
    normalized = "general" if value == "unspecified" else value
    if normalized not in ACCESS_SCOPES:
        raise ValueError("문서 권한 설정이 올바르지 않습니다.")
    return normalized


def normalize_access_scopes(
    values: set[str] | frozenset[str] | None,
) -> tuple[str, ...]:
    """Return a validated, stable scope list for parameterized SQL filters."""
    if values is None:
        values = {"general"}
    return tuple(sorted({normalize_access_scope(value) for value in values}))


def normalize_taxonomy_text(value: str) -> str:
    """Stable application-side key; PostgreSQL and SQLite use the same semantics."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _clean_short_text(value: str, *, label: str, maximum: int = 255) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ValueError(f"{label}을(를) 입력해 주세요.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label}은(는) {maximum}자 이하여야 합니다.")
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError(f"{label}에 제어 문자를 넣을 수 없습니다.")
    return cleaned


def _clean_aliases(values: list[str]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in values:
        alias = _clean_short_text(raw, label="검색 별칭", maximum=200)
        normalized = normalize_taxonomy_text(alias)
        if normalized not in seen:
            seen.add(normalized)
            aliases.append(alias)
    if len(aliases) > 50:
        raise ValueError("검색 별칭은 최대 50개까지 저장할 수 있습니다.")
    return aliases


def _clean_taxonomy_paths(values: list[str]) -> list[list[str]]:
    paths: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in values:
        parts = [
            _clean_short_text(part, label="분류명", maximum=200)
            for part in re.split(r"\s*(?:>|/)\s*", str(raw or "").strip())
            if part.strip()
        ]
        if not parts:
            continue
        if len(parts) > 8:
            raise ValueError("분류 경로는 최대 8단계까지 저장할 수 있습니다.")
        normalized = tuple(normalize_taxonomy_text(part) for part in parts)
        if normalized not in seen:
            seen.add(normalized)
            paths.append(parts)
    if len(paths) > 20:
        raise ValueError("문서 분류 경로는 최대 20개까지 저장할 수 있습니다.")
    return paths


def _validate_classification_shape(
    paths: list[list[str]], products: list[str], models: list[str]
) -> None:
    """Reject flat input that cannot be mapped to one unambiguous tree."""
    if products and len(paths) != 1:
        raise ValueError("제품군을 입력할 때 상위 분류 경로는 하나만 지정해 주세요.")
    if models and len({normalize_taxonomy_text(value) for value in products}) != 1:
        raise ValueError("모델을 입력할 때 제품군은 하나만 지정해 주세요.")


def _document_taxonomy_paths(
    nodes: list[dict[str, Any]], links: list[dict[str, Any]]
) -> dict[str, str]:
    """Build complete ancestor paths for each document's directly linked leaf nodes."""
    by_id = {str(node["id"]): node for node in nodes}
    linked_by_document: dict[str, set[str]] = {}
    for link in links:
        node_id = str(link["node_id"])
        if node_id in by_id:
            linked_by_document.setdefault(str(link["document_id"]), set()).add(node_id)

    def ancestors(node_id: str) -> set[str]:
        found: set[str] = set()
        current = by_id.get(node_id)
        while current:
            parent_id = str(current.get("parent_id") or "")
            if not parent_id or parent_id in found:
                break
            found.add(parent_id)
            current = by_id.get(parent_id)
        return found

    def full_path(node_id: str) -> str:
        names: list[str] = []
        seen: set[str] = set()
        current = by_id.get(node_id)
        while current and str(current["id"]) not in seen:
            seen.add(str(current["id"]))
            names.append(str(current["name"]))
            current = by_id.get(str(current.get("parent_id") or ""))
        return " > ".join(reversed(names))

    result: dict[str, str] = {}
    for document_id, linked_ids in linked_by_document.items():
        non_leaf_ids = {
            ancestor_id
            for node_id in linked_ids
            for ancestor_id in ancestors(node_id)
            if ancestor_id in linked_ids
        }
        paths = {
            full_path(node_id) for node_id in linked_ids - non_leaf_ids
            if full_path(node_id)
        }
        result[document_id] = " | ".join(sorted(paths))
    return result


def _apply_document_aliases(
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
) -> None:
    """Attach a document alias to one representative node, never every mapped node."""
    by_id = {str(node["id"]): node for node in nodes}
    linked_by_document: dict[str, list[str]] = {}
    for link in links:
        node_id = str(link["node_id"])
        if node_id in by_id:
            linked_by_document.setdefault(str(link["document_id"]), []).append(node_id)
    representative: dict[str, str] = {}
    for document_id, node_ids in linked_by_document.items():
        products = [
            node_id for node_id in node_ids
            if by_id[node_id].get("node_type") == "product_family"
        ]
        categories = [
            node_id for node_id in node_ids
            if by_id[node_id].get("node_type") == "category"
        ]
        # A document-level alias has no explicit target. Use it only when the
        # representative parent is unambiguous; never guess among products.
        if len(products) == 1:
            representative[document_id] = products[0]
        elif not products and len(categories) == 1:
            representative[document_id] = categories[0]
    for item in aliases:
        node = by_id.get(representative.get(str(item["document_id"]), ""))
        alias = str(item["alias"])
        if node is not None and alias not in node["aliases"]:
            node["aliases"].append(alias)


def _taxonomy_match_score(query: str, candidate: str) -> int:
    normalized_query = normalize_taxonomy_text(query)
    normalized_candidate = normalize_taxonomy_text(candidate)
    if not normalized_candidate:
        return 0
    if normalized_query == normalized_candidate:
        return 10_000 + len(normalized_candidate)
    if len(normalized_candidate) == 1 and normalized_candidate.isascii():
        return 8_000 if re.search(
            rf"(?<![0-9a-z]){re.escape(normalized_candidate)}(?![0-9a-z])",
            normalized_query,
        ) else 0
    query_tokens = set(TOKEN_PATTERN.findall(normalized_query))
    if normalized_candidate in query_tokens:
        return 8_000 + len(normalized_candidate)
    if normalized_candidate in normalized_query:
        return 4_000 + len(normalized_candidate)
    return 0


def _tree_from_taxonomy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = {str(row["id"]): {**row, "children": []} for row in rows}
    roots: list[dict[str, Any]] = []
    for item in items.values():
        parent = items.get(str(item.get("parent_id") or ""))
        if parent is None:
            roots.append(item)
        else:
            parent["children"].append(item)
    sort_key = lambda item: (str(item.get("name", "")).casefold(), str(item["id"]))
    for item in items.values():
        item["children"].sort(key=sort_key)
    roots.sort(key=sort_key)
    return roots


def _resolve_hierarchy_rows(
    query: str,
    nodes: list[dict[str, Any]],
    document_links: list[dict[str, Any]],
) -> HierarchyResolution:
    """Resolve only reviewed nodes backed by currently accessible documents."""
    by_id = {str(item["id"]): item for item in nodes}
    children: dict[str, set[str]] = {}
    for item in nodes:
        parent_id = item.get("parent_id")
        if parent_id:
            children.setdefault(str(parent_id), set()).add(str(item["id"]))
    direct_documents: dict[str, set[str]] = {}
    for link in document_links:
        direct_documents.setdefault(str(link["node_id"]), set()).add(
            str(link["document_id"])
        )

    def descendants(node_id: str) -> set[str]:
        found = {node_id}
        pending = list(children.get(node_id, ()))
        while pending:
            child_id = pending.pop()
            if child_id in found:
                continue
            found.add(child_id)
            pending.extend(children.get(child_id, ()))
        return found

    def path_for(node_id: str) -> str:
        names: list[str] = []
        seen: set[str] = set()
        current = by_id.get(node_id)
        while current and str(current["id"]) not in seen:
            seen.add(str(current["id"]))
            names.append(str(current["name"]))
            current = by_id.get(str(current.get("parent_id") or ""))
        return " > ".join(reversed(names))

    scored: list[tuple[int, dict[str, Any]]] = []
    for node in nodes:
        candidates = [str(node["name"]), *(str(value) for value in node.get("aliases", []))]
        score = max((_taxonomy_match_score(query, value) for value in candidates), default=0)
        # A clarification button sends the reviewed full path.  Prefer that
        # path decisively over another node that happens to share the same leaf
        # name (for example "회사 정책 > 임플란트" vs "제품 > 임플란트").
        full_path = path_for(str(node["id"]))
        path_score = _taxonomy_match_score(query, full_path)
        if ">" in full_path and path_score:
            score = max(score, path_score + 10_000)
        if score:
            scored.append((score, node))
    if not scored:
        return HierarchyResolution(query, (), frozenset(), False)
    best_score = max(score for score, _ in scored)
    best = [node for score, node in scored if score == best_score]
    matches: list[dict[str, Any]] = []
    all_document_ids: set[str] = set()
    for node in sorted(best, key=lambda value: (str(value["name"]).casefold(), str(value["id"])))[:4]:
        node_id = str(node["id"])
        related_node_ids = descendants(node_id)
        document_ids = sorted({
            document_id
            for related_id in related_node_ids
            for document_id in direct_documents.get(related_id, ())
        })
        if not document_ids:
            continue
        eligible_children: list[dict[str, Any]] = []
        for child_id in sorted(
            children.get(node_id, ()),
            key=lambda value: (str(by_id[value]["name"]).casefold(), value),
        ):
            child_document_ids = sorted({
                document_id
                for related_id in descendants(child_id)
                for document_id in direct_documents.get(related_id, ())
            })
            if child_document_ids:
                child = by_id[child_id]
                eligible_children.append({
                    "id": child_id,
                    "name": child["name"],
                    "node_type": child["node_type"],
                    "path": path_for(child_id),
                    "document_ids": child_document_ids,
                })
        all_document_ids.update(document_ids)
        matches.append({
            "id": node_id,
            "name": node["name"],
            "node_type": node["node_type"],
            "parent_id": node.get("parent_id"),
            "path": path_for(node_id),
            "aliases": list(node.get("aliases", [])),
            "document_ids": document_ids,
            "children": eligible_children,
        })
    return HierarchyResolution(
        query=query,
        matches=tuple(matches),
        document_ids=frozenset(all_document_ids),
        ambiguous=len(matches) > 1,
    )


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int | None
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_index: int
    page_number: int | None
    section_title: str | None
    content: str


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    document_id: str
    title: str
    original_filename: str
    page_number: int | None
    section_title: str | None
    content: str
    document_type: str | None = None
    taxonomy_path: str | None = None
    document_summary: str | None = None


@dataclass(frozen=True, slots=True)
class HierarchyResolution:
    """Reviewed taxonomy matches that are safe to use for retrieval or choices."""

    query: str
    matches: tuple[dict[str, Any], ...]
    document_ids: frozenset[str]
    ambiguous: bool

    @property
    def taxonomy_context(self) -> str:
        return "\n".join(
            f"분류: {item['path']} ({item['node_type']})"
            for item in self.matches
        )


class KnowledgeRepository(Protocol):
    storage_root: Path

    def create_document(self, *, title: str, original_filename: str, data: bytes,
                        created_by: str, visibility: str = "admin_only",
                        access_scope: str = "general",
                        use_for_ai: bool = False,
                        download_enabled: bool = False) -> dict[str, Any]: ...
    def list_documents(self) -> list[dict[str, Any]]: ...
    def get_document(self, document_id: str) -> dict[str, Any] | None: ...
    def update_document(self, document_id: str, *, title: str | None = None,
                        status: str | None = None, use_for_ai: bool | None = None,
                        download_enabled: bool | None = None,
                        visibility: str | None = None,
                        access_scope: str | None = None) -> dict[str, Any] | None: ...
    def delete_document(self, document_id: str) -> bool: ...
    def retrieve(
        self,
        query: str,
        limit: int,
        access_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[RetrievedChunk]: ...
    def list_search_catalog(
        self,
        access_scopes: set[str] | frozenset[str] | None = None,
        limit: int = 100,
        max_chars: int = 12_000,
    ) -> str: ...
    def has_active_documents(self) -> bool: ...
    def list_taxonomy_nodes(self) -> list[dict[str, Any]]: ...
    def create_taxonomy_node(self, *, name: str, node_type: str,
                             parent_id: str | None, aliases: list[str],
                             description: str = "", status: str = "active",
                             review_status: str = "pending_review",
                             updated_by: str) -> dict[str, Any]: ...
    def update_taxonomy_node(self, node_id: str, **changes: Any) -> dict[str, Any] | None: ...
    def delete_taxonomy_node(self, node_id: str) -> bool: ...
    def get_document_classification(self, document_id: str) -> dict[str, Any] | None: ...
    def save_document_classification(self, document_id: str, *, document_type: str,
                                     summary: str, taxonomy_paths: list[str],
                                     product_families: list[str], model_names: list[str],
                                     aliases: list[str], review_status: str,
                                     updated_by: str,
                                     expected_version: int | None = None) -> dict[str, Any] | None: ...
    def mark_document_profile_failed(self, document_id: str, *, updated_by: str,
                                     error_code: str = "classification_failed") -> dict[str, Any] | None: ...
    def mark_document_classification_failed(self, document_id: str, *, error_code: str,
                                            updated_by: str) -> dict[str, Any] | None: ...
    def resolve_hierarchy(self, query: str,
                          access_scopes: set[str] | frozenset[str] | None = None
                          ) -> HierarchyResolution: ...


def _safe_original_filename(filename: str) -> str:
    name = Path(filename or "").name.strip()
    if not name or name in {".", ".."}:
        raise ValueError("파일명이 올바르지 않습니다.")
    if len(name) > 255:
        raise ValueError("파일명은 255자 이하여야 합니다.")
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("MD, TXT, 일반 텍스트 PDF 파일만 업로드할 수 있습니다.")
    return name


def _stored_file_path(storage_root: Path, stored_filename: str) -> Path:
    """Resolve a repository-owned file without ever following a path outside storage."""
    root = storage_root.resolve()
    relative = Path(stored_filename)
    if relative.name != stored_filename or stored_filename in {"", ".", ".."}:
        raise ValueError("저장된 문서 파일 경로가 올바르지 않습니다.")
    raw_path = root / relative
    if raw_path.is_symlink():
        raise ValueError("심볼릭 링크로 저장된 문서는 삭제할 수 없습니다.")
    path = raw_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("저장된 문서 파일 경로가 올바르지 않습니다.") from error
    if path == root:
        raise ValueError("저장된 문서 파일 경로가 올바르지 않습니다.")
    return path


def _stage_file_for_deletion(storage_root: Path, stored_filename: str) -> tuple[Path, Path] | None:
    """Atomically hide a file so a failed database deletion can restore it."""
    target = _stored_file_path(storage_root, stored_filename)
    if not target.exists():
        return None
    if not target.is_file():
        raise ValueError("저장된 문서 파일이 일반 파일이 아닙니다.")
    staged = storage_root.resolve() / f".deleting-{uuid4().hex}-{target.name}"
    # replace() preserves mtime. Refresh it before the atomic rename so another
    # worker's stale-file cleanup cannot mistake this active deletion for old work.
    target.touch()
    target.replace(staged)
    return target, staged


def _restore_staged_file(staged_file: tuple[Path, Path] | None) -> None:
    if staged_file is None:
        return
    target, staged = staged_file
    if staged.exists() and not target.exists():
        staged.replace(target)


def _purge_staged_file(staged_file: tuple[Path, Path] | None) -> bool:
    if staged_file is None:
        return True
    try:
        staged_file[1].unlink(missing_ok=True)
    except OSError:
        LOGGER.warning(
            "삭제 완료 문서의 임시 파일을 정리하지 못했습니다: %s",
            staged_file[1].name,
            exc_info=True,
        )
        return False
    return True


def _cleanup_staged_files(storage_root: Path) -> None:
    """Best-effort cleanup limited to temporary names created by this repository."""
    root = storage_root.resolve()
    stale_before = time.time() - STAGED_DELETE_TTL_SECONDS
    try:
        entries = tuple(root.iterdir())
    except OSError:
        LOGGER.warning("지식 문서 저장소의 임시 파일을 확인하지 못했습니다.", exc_info=True)
        return
    for path in entries:
        if not STAGED_DELETE_PATTERN.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            if path.is_file() and path.stat().st_mtime <= stale_before:
                path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning(
                "이전 삭제 작업의 임시 파일을 정리하지 못했습니다: %s",
                path.name,
                exc_info=True,
            )


def extract_pages(filename: str, data: bytes) -> list[ExtractedPage]:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("MD, TXT, 일반 텍스트 PDF 파일만 업로드할 수 있습니다.")
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("PDF 파일 형식이 올바르지 않습니다.")
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = [
                ExtractedPage(index + 1, (page.extract_text() or "").strip())
                for index, page in enumerate(reader.pages)
            ]
        except Exception as error:
            raise ValueError("PDF 텍스트를 추출하지 못했습니다.") from error
        if not any(page.text for page in pages):
            raise ValueError("텍스트가 없는 PDF입니다. 스캔 PDF는 OCR 적용 후 업로드해 주세요.")
        return pages
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("MD와 TXT 파일은 UTF-8 형식이어야 합니다.") from error
    text = text.strip()
    if not text:
        raise ValueError("내용이 없는 파일입니다.")
    return [ExtractedPage(None, text)]


def _split_oversized(text: str, *, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            candidates = [text.rfind(marker, start + size // 2, end) for marker in ("\n", ". ", "다. ", "요. ")]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces


def chunk_pages(pages: list[ExtractedPage], *, size: int, overlap: int) -> list[KnowledgeChunk]:
    if overlap >= size:
        raise ValueError("청크 겹침 길이는 청크 크기보다 작아야 합니다.")
    chunks: list[KnowledgeChunk] = []
    current_heading: str | None = None
    for page in pages:
        blocks = [block.strip() for block in re.split(r"\n\s*\n+", page.text) if block.strip()]
        buffer = ""
        for block in blocks:
            heading_match = re.match(r"^#{1,6}\s+(.+)$", block.splitlines()[0])
            if heading_match:
                if buffer:
                    for part in _split_oversized(buffer, size=size, overlap=overlap):
                        chunks.append(KnowledgeChunk(len(chunks), page.page_number, current_heading, part))
                    buffer = ""
                current_heading = heading_match.group(1).strip()[:500]
            candidate = f"{buffer}\n\n{block}".strip() if buffer else block
            if len(candidate) <= size:
                buffer = candidate
                continue
            if buffer:
                for part in _split_oversized(buffer, size=size, overlap=overlap):
                    chunks.append(KnowledgeChunk(len(chunks), page.page_number, current_heading, part))
            buffer = block
        if buffer:
            for part in _split_oversized(buffer, size=size, overlap=overlap):
                chunks.append(KnowledgeChunk(len(chunks), page.page_number, current_heading, part))
    return chunks


def _query_intents(query: str) -> tuple[bool, bool, bool, bool]:
    normalized = unicodedata.normalize("NFKC", query).casefold()
    compact = re.sub(r"[^0-9a-z가-힣]+", "", normalized)
    representative = bool(
        any(term in compact for term in ("대표자", "대표이사", "사장", "ceo"))
        or re.search(r"대표(?:는|가|은)?(?:누구|누가|인물)", compact)
        or compact in {"대표", "대표는", "대표가"}
    )
    explicit_address = any(term in compact for term in ("주소", "소재지"))
    location_where = bool(re.search(r"어디(?:야|에|인가|있|위치|$)", compact))
    location_marker = "위치" in compact or location_where
    company_location = any(term in compact for term in ("본사", "회사"))
    commerce_context = any(
        term in compact
        for term in ("제품", "카탈로그", "구매", "주문", "살", "다운로드", "받아", "구해")
    )
    address = bool(
        explicit_address
        or (company_location and location_marker and not commerce_context)
    )
    phone = any(term in compact for term in ("전화", "연락처", "전화번호"))
    company_name = any(term in compact for term in ("회사명", "법인명", "회사이름"))
    return representative, address, phone, company_name


def search_query_terms(query: str) -> set[str]:
    """Expand stable aliases only when the surrounding intent is unambiguous."""
    normalized = unicodedata.normalize("NFKC", query).casefold()
    tokens = {
        token for token in TOKEN_PATTERN.findall(normalized)
        if token not in _SEARCH_STOP_TOKENS
    }
    compact = re.sub(r"[^0-9a-z가-힣]+", "", normalized)
    if ("어디서" in compact
            and any(term in compact for term in (
                "구매", "주문", "살", "다운로드", "받아", "구해",
            ))):
        # In an action question, 본사/회사는 a product qualifier rather than a
        # request for the head-office address.
        tokens.discard("본사")
        tokens.discard("회사")
    representative, address, phone, company_name = _query_intents(query)
    if representative:
        tokens.update(("대표", "대표자", "대표이사", "사장", "ceo"))
    if address:
        tokens.update(("주소", "소재지", "위치"))
    if phone:
        tokens.update(("전화", "전화번호", "대표전화", "연락처"))
    if company_name:
        tokens.update(("회사명", "법인명", "주식회사"))
    return tokens


def substantive_relevance_score(query: str, item: RetrievedChunk) -> int:
    """Score section/body evidence, excluding the repeated document title."""
    tokens = search_query_terms(query)
    if not tokens:
        return 0
    content = unicodedata.normalize("NFKC", item.content).casefold()
    heading = unicodedata.normalize("NFKC", item.section_title or "").casefold()
    content_score = sum(4 * min(content.count(token), 3) for token in tokens)
    heading_score = sum(6 * (token in heading) for token in tokens)
    representative, address, phone, _ = _query_intents(query)
    field_bonus = 0
    if representative:
        if re.search(r"(?:대표자|대표이사|ceo)\s*[:：]", content):
            field_bonus += 30
    if address:
        if re.search(r"(?:본사\s*)?(?:주소|소재지)\s*[:：]", content):
            field_bonus += 30
    if phone:
        if re.search(r"(?:대표\s*)?(?:전화|전화번호|연락처)\s*[:：]", content):
            field_bonus += 30
    return content_score + heading_score + field_bonus


def _score(query: str, item: RetrievedChunk) -> int:
    tokens = search_query_terms(query)
    title = unicodedata.normalize("NFKC", item.title).casefold()
    # The title is repeated on every chunk. It may select a document, but cannot
    # outrank an actual body or section match from that document.
    title_score = min(sum(token in title for token in tokens), 2)
    metadata = unicodedata.normalize(
        "NFKC", f"{item.taxonomy_path or ''} {item.document_summary or ''}"
    ).casefold()
    metadata_score = sum(8 * (token in metadata) for token in tokens)
    return substantive_relevance_score(query, item) + title_score + metadata_score


def _rank_chunks(query: str, items: list[RetrievedChunk], limit: int) -> list[RetrievedChunk]:
    ranked = sorted(
        ((_score(query, item), item) for item in items),
        key=lambda pair: (
            -pair[0],
            pair[1].title.casefold(),
            pair[1].section_title or "",
            pair[1].page_number or 0,
            pair[1].document_id,
            pair[1].content,
        ),
    )
    ranked = [(score, item) for score, item in ranked if score > 0]
    if limit <= 0:
        return []

    selected: list[RetrievedChunk] = []
    seen: set[tuple[str, int | None, str | None, str]] = set()

    def append(item: RetrievedChunk) -> None:
        key = (item.document_id, item.page_number, item.section_title, item.content)
        if key not in seen and len(selected) < limit:
            seen.add(key)
            selected.append(item)

    if ranked:
        append(ranked[0][1])

    # A catalog rewrite may intentionally contain several alternatives (for
    # example 본사/영업소/공장). Preserve one strong chunk for rare query terms
    # before filling the remaining slots by total score.
    normalized_query = unicodedata.normalize("NFKC", query).casefold()
    original_tokens = {
        token for token in TOKEN_PATTERN.findall(normalized_query)
        if token not in _SEARCH_STOP_TOKENS
    }
    coverage: list[tuple[int, str, RetrievedChunk]] = []
    for token in original_tokens:
        matches = [
            item for _, item in ranked
            if token in unicodedata.normalize(
                "NFKC", f"{item.section_title or ''} {item.content}"
            ).casefold()
        ]
        if matches:
            coverage.append((len(matches), token, matches[0]))
    for _, _, item in sorted(coverage, key=lambda value: (value[0], value[1])):
        append(item)
    for _, item in ranked:
        append(item)
    return selected


def _rank_with_hierarchy(
    query: str,
    items: list[RetrievedChunk],
    limit: int,
    resolution: HierarchyResolution,
) -> list[RetrievedChunk]:
    if not resolution.document_ids:
        return _rank_chunks(query, items, limit)
    if not resolution.ambiguous and re.search(r"[^>\n]+\s*>\s*[^>\n]+", query):
        items = [
            item for item in items if item.document_id in resolution.document_ids
        ]
    context = resolution.taxonomy_context
    enriched = [
        replace(
            item,
            taxonomy_path=" | ".join(
                value for value in (item.taxonomy_path, context) if value
            ),
        )
        if item.document_id in resolution.document_ids else item
        for item in items
    ]
    return _rank_chunks(query, enriched, limit)


def _format_search_catalog(
    rows: list[dict[str, Any]],
    *,
    max_chars: int,
) -> str:
    if max_chars <= 0:
        return ""
    entries: list[str] = []
    used = 0
    for row in rows:
        preview = " ".join(str(row.get("content") or "").split())[:240]
        parts = [f"문서: {str(row.get('title') or '').strip()}"]
        document_type = str(row.get("document_type") or "").strip()
        if document_type:
            parts.append(f"문서 종류: {document_type}")
        taxonomy_path = str(row.get("taxonomy_path") or "").strip()
        if taxonomy_path:
            parts.append(f"검토된 분류: {taxonomy_path}")
        section = str(row.get("section_title") or "").strip()
        if section:
            parts.append(f"섹션: {section}")
        if preview:
            parts.append(f"미리보기: {preview}")
        entry = " | ".join(parts)
        separator_size = 1 if entries else 0
        remaining = max_chars - used - separator_size
        if remaining <= 0:
            break
        if len(entry) > remaining:
            entry = entry[:remaining]
        entries.append(entry)
        used += len(entry) + separator_size
        if len(entry) == remaining:
            break
    return "\n".join(entries)


class SQLiteKnowledgeRepository:
    def __init__(self, database_path: Path, storage_root: Path,
                 chunk_size: int, chunk_overlap: int, max_pages: int = 300,
                 max_extracted_chars: int = 2_000_000,
                 max_chunks: int = 5_000) -> None:
        self.database_path = database_path
        self.storage_root = storage_root
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_pages = max_pages
        self.max_extracted_chars = max_extracted_chars
        self.max_chunks = max_chunks
        database_path.parent.mkdir(parents=True, exist_ok=True)
        storage_root.mkdir(parents=True, exist_ok=True)
        _cleanup_staged_files(storage_root)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_document (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, original_filename TEXT NOT NULL,
                    stored_filename TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
                    visibility TEXT NOT NULL DEFAULT 'admin_only',
                    access_scope TEXT NOT NULL DEFAULT 'general',
                    use_for_ai INTEGER NOT NULL DEFAULT 0,
                    download_enabled INTEGER NOT NULL DEFAULT 0,
                    page_count INTEGER, char_count INTEGER NOT NULL DEFAULT 0,
                    chunk_count INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunk (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL REFERENCES knowledge_document(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL, page_number INTEGER,
                    section_title TEXT, content TEXT NOT NULL,
                    UNIQUE(document_id, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS ix_knowledge_document_status
                    ON knowledge_document(status, use_for_ai);
                CREATE INDEX IF NOT EXISTS ix_knowledge_chunk_document
                    ON knowledge_chunk(document_id, chunk_index);
                CREATE TABLE IF NOT EXISTS knowledge_taxonomy_node (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT REFERENCES knowledge_taxonomy_node(id) ON DELETE RESTRICT,
                    node_type TEXT NOT NULL CHECK (node_type IN ('category','product_family','model')),
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
                    review_status TEXT NOT NULL DEFAULT 'pending_review'
                        CHECK (review_status IN ('pending_review','reviewed')),
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(parent_id,node_type,normalized_name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_taxonomy_root
                    ON knowledge_taxonomy_node(node_type,normalized_name)
                    WHERE parent_id IS NULL;
                CREATE TABLE IF NOT EXISTS knowledge_taxonomy_alias (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL REFERENCES knowledge_taxonomy_node(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    UNIQUE(node_id,normalized_alias)
                );
                CREATE INDEX IF NOT EXISTS ix_knowledge_taxonomy_alias_normalized
                    ON knowledge_taxonomy_alias(normalized_alias);
                CREATE TABLE IF NOT EXISTS knowledge_document_profile (
                    document_id TEXT PRIMARY KEY REFERENCES knowledge_document(id) ON DELETE CASCADE,
                    document_type TEXT NOT NULL DEFAULT 'other'
                        CHECK (document_type IN ('company','catalog','product_guide','manual','service','policy','other')),
                    summary TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'unclassified'
                        CHECK (review_status IN ('unclassified','pending_review','reviewed','failed')),
                    error_code TEXT,
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_document_taxonomy (
                    document_id TEXT NOT NULL REFERENCES knowledge_document(id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL REFERENCES knowledge_taxonomy_node(id) ON DELETE RESTRICT,
                    relation_type TEXT NOT NULL DEFAULT 'primary'
                        CHECK (relation_type IN ('primary','related')),
                    PRIMARY KEY(document_id,node_id)
                );
                CREATE INDEX IF NOT EXISTS ix_knowledge_document_taxonomy_node
                    ON knowledge_document_taxonomy(node_id,document_id);
                CREATE TABLE IF NOT EXISTS knowledge_document_alias (
                    document_id TEXT NOT NULL REFERENCES knowledge_document(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    PRIMARY KEY(document_id,normalized_alias)
                );
                CREATE INDEX IF NOT EXISTS ix_knowledge_document_alias_normalized
                    ON knowledge_document_alias(normalized_alias);
            """)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(knowledge_document)").fetchall()
            }
            if "visibility" not in columns:
                connection.execute(
                    "ALTER TABLE knowledge_document ADD COLUMN visibility TEXT NOT NULL DEFAULT 'admin_only'"
                )
            if "access_scope" not in columns:
                connection.execute(
                    "ALTER TABLE knowledge_document ADD COLUMN access_scope TEXT NOT NULL DEFAULT 'general'"
                )
            connection.execute(
                "UPDATE knowledge_document SET access_scope='general' WHERE access_scope='unspecified'"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_document_access "
                "ON knowledge_document(status, visibility, access_scope, use_for_ai)"
            )
            profile_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(knowledge_document_profile)"
                ).fetchall()
            }
            if "error_code" not in profile_columns:
                connection.execute(
                    "ALTER TABLE knowledge_document_profile ADD COLUMN error_code TEXT"
                )

    def create_document(self, *, title: str, original_filename: str, data: bytes,
                        created_by: str, visibility: str = "admin_only",
                        access_scope: str = "general",
                        use_for_ai: bool = False,
                        download_enabled: bool = False) -> dict[str, Any]:
        original_filename = _safe_original_filename(original_filename)
        title = title.strip()[:255]
        if not title:
            raise ValueError("문서 제목을 입력해 주세요.")
        if visibility not in {"public", "admin_only"}:
            raise ValueError("문서 공개 범위가 올바르지 않습니다.")
        access_scope = normalize_access_scope(access_scope)
        if visibility == "admin_only":
            use_for_ai = False
            download_enabled = False
        pages = extract_pages(original_filename, data)
        if len(pages) > self.max_pages:
            raise ValueError(f"PDF 페이지 수는 최대 {self.max_pages}페이지입니다.")
        extracted_chars = sum(len(page.text) for page in pages)
        if extracted_chars > self.max_extracted_chars:
            raise ValueError(f"추출 텍스트는 최대 {self.max_extracted_chars:,}자입니다.")
        chunks = chunk_pages(pages, size=self.chunk_size, overlap=self.chunk_overlap)
        if len(chunks) > self.max_chunks:
            raise ValueError(f"생성 가능한 청크는 최대 {self.max_chunks:,}개입니다.")
        if not chunks:
            raise ValueError("검색에 사용할 텍스트가 없습니다.")
        document_id = uuid4().hex
        suffix = Path(original_filename).suffix.lower()
        stored_filename = f"{document_id}{suffix}"
        target = self.storage_root / stored_filename
        target.write_bytes(data)
        now = datetime.now(UTC).isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO knowledge_document
                       (id,title,original_filename,stored_filename,mime_type,file_size,sha256,
                       status,visibility,access_scope,use_for_ai,download_enabled,page_count,char_count,chunk_count,
                        created_by,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?)""",
                    (document_id, title, original_filename, stored_filename, MIME_TYPES[suffix],
                     len(data), hashlib.sha256(data).hexdigest(), visibility, access_scope, int(use_for_ai),
                     int(download_enabled), len(pages) if suffix == ".pdf" else None,
                     extracted_chars, len(chunks), created_by, now, now),
                )
                connection.executemany(
                    "INSERT INTO knowledge_chunk (document_id,chunk_index,page_number,section_title,content) VALUES (?,?,?,?,?)",
                    [(document_id, c.chunk_index, c.page_number, c.section_title, c.content) for c in chunks],
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.get_document(document_id)  # type: ignore[return-value]

    def list_documents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM knowledge_document ORDER BY created_at DESC").fetchall()
        items = [self._normalize(dict(row)) for row in rows]
        for item in items:
            item["classification"] = self.get_document_classification(item["id"])
        return items

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_document WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return None
        item = self._normalize(dict(row))
        item["classification"] = self.get_document_classification(document_id)
        return item

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        item["use_for_ai"] = bool(item["use_for_ai"])
        item["download_enabled"] = bool(item["download_enabled"])
        return item

    def update_document(self, document_id: str, *, title: str | None = None,
                        status: str | None = None, use_for_ai: bool | None = None,
                        download_enabled: bool | None = None,
                        visibility: str | None = None,
                        access_scope: str | None = None) -> dict[str, Any] | None:
        if status is not None and status not in {"draft", "published", "disabled"}:
            raise ValueError("문서 상태가 올바르지 않습니다.")
        if visibility is not None and visibility not in {"public", "admin_only"}:
            raise ValueError("문서 공개 범위가 올바르지 않습니다.")
        if access_scope is not None:
            access_scope = normalize_access_scope(access_scope)
        current = self.get_document(document_id)
        if current is None:
            return None
        values = {
            "title": title.strip()[:255] if title is not None else current["title"],
            "status": status if status is not None else current["status"],
            "use_for_ai": int(use_for_ai if use_for_ai is not None else current["use_for_ai"]),
            "download_enabled": int(download_enabled if download_enabled is not None else current["download_enabled"]),
            "visibility": visibility if visibility is not None else current["visibility"],
            "access_scope": access_scope if access_scope is not None else current["access_scope"],
        }
        if values["visibility"] == "admin_only":
            values["use_for_ai"] = 0
            values["download_enabled"] = 0
        if not values["title"]:
            raise ValueError("문서 제목을 입력해 주세요.")
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge_document SET title=?,status=?,use_for_ai=?,download_enabled=?,visibility=?,access_scope=?,updated_at=? WHERE id=?",
                (values["title"], values["status"], values["use_for_ai"], values["download_enabled"], values["visibility"], values["access_scope"], datetime.now(UTC).isoformat(), document_id),
            )
        return self.get_document(document_id)

    def delete_document(self, document_id: str) -> bool:
        _cleanup_staged_files(self.storage_root)
        staged_file: tuple[Path, Path] | None = None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT stored_filename FROM knowledge_document WHERE id = ?",
                    (document_id,),
                ).fetchone()
                if row is None:
                    return False
                staged_file = _stage_file_for_deletion(
                    self.storage_root, row["stored_filename"]
                )
                connection.execute(
                    "DELETE FROM knowledge_document WHERE id = ?", (document_id,)
                )
        except Exception:
            _restore_staged_file(staged_file)
            raise
        _purge_staged_file(staged_file)
        return True

    def _taxonomy_rows(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM knowledge_taxonomy_node ORDER BY name,id"
        ).fetchall()]
        aliases: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT node_id,alias FROM knowledge_taxonomy_alias ORDER BY alias,id"
        ).fetchall():
            aliases.setdefault(str(row["node_id"]), []).append(str(row["alias"]))
        for row in rows:
            row["aliases"] = aliases.get(str(row["id"]), [])
        return rows

    def list_taxonomy_nodes(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return _tree_from_taxonomy_rows(self._taxonomy_rows(connection))

    @staticmethod
    def _validate_taxonomy_node(
        *, name: str, node_type: str, description: str, status: str,
        review_status: str, aliases: list[str],
    ) -> tuple[str, str, str, str, str, list[str]]:
        clean_name = _clean_short_text(name, label="분류명", maximum=200)
        if node_type not in TAXONOMY_NODE_TYPES:
            raise ValueError("분류 유형이 올바르지 않습니다.")
        if status not in {"active", "disabled"}:
            raise ValueError("분류 상태가 올바르지 않습니다.")
        if review_status not in {"pending_review", "reviewed"}:
            raise ValueError("분류 검토 상태가 올바르지 않습니다.")
        clean_description = str(description or "").strip()
        if len(clean_description) > 4000:
            raise ValueError("분류 설명은 4,000자 이하여야 합니다.")
        return (
            clean_name, normalize_taxonomy_text(clean_name), node_type,
            clean_description, status, _clean_aliases(aliases),
        )

    @staticmethod
    def _validate_parent(
        connection: sqlite3.Connection,
        parent_id: str | None,
        node_type: str,
        *,
        current_id: str | None = None,
    ) -> None:
        if parent_id is None:
            if node_type != "category":
                raise ValueError("제품군과 모델에는 상위 분류가 필요합니다.")
            return
        parent = connection.execute(
            "SELECT id,parent_id,node_type FROM knowledge_taxonomy_node WHERE id=?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            raise ValueError("상위 분류를 찾을 수 없습니다.")
        if current_id and str(parent["id"]) == current_id:
            raise ValueError("분류를 자기 자신의 하위로 이동할 수 없습니다.")
        allowed = {
            "category": {"category"},
            "product_family": {"category"},
            "model": {"product_family"},
        }
        if str(parent["node_type"]) not in allowed[node_type]:
            raise ValueError("상위 분류와 하위 분류 유형이 올바르지 않습니다.")
        ancestor_id = str(parent["parent_id"] or "")
        while current_id and ancestor_id:
            if ancestor_id == current_id:
                raise ValueError("분류 계층에 순환 구조를 만들 수 없습니다.")
            ancestor = connection.execute(
                "SELECT parent_id FROM knowledge_taxonomy_node WHERE id=?",
                (ancestor_id,),
            ).fetchone()
            ancestor_id = str(ancestor["parent_id"] or "") if ancestor else ""

    @staticmethod
    def _replace_sqlite_aliases(
        connection: sqlite3.Connection, node_id: str, aliases: list[str]
    ) -> None:
        connection.execute("DELETE FROM knowledge_taxonomy_alias WHERE node_id=?", (node_id,))
        connection.executemany(
            "INSERT INTO knowledge_taxonomy_alias(node_id,alias,normalized_alias) VALUES (?,?,?)",
            [(node_id, alias, normalize_taxonomy_text(alias)) for alias in aliases],
        )

    def create_taxonomy_node(
        self, *, name: str, node_type: str, parent_id: str | None,
        aliases: list[str], description: str = "", status: str = "active",
        review_status: str = "pending_review", updated_by: str,
    ) -> dict[str, Any]:
        name, normalized, node_type, description, status, aliases = self._validate_taxonomy_node(
            name=name, node_type=node_type, description=description, status=status,
            review_status=review_status, aliases=aliases,
        )
        actor = _clean_short_text(updated_by, label="수정자", maximum=200)
        node_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        try:
            with self._connect() as connection:
                self._validate_parent(connection, parent_id, node_type)
                connection.execute(
                    """INSERT INTO knowledge_taxonomy_node
                       (id,parent_id,node_type,name,normalized_name,description,status,review_status,
                        created_by,updated_by,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (node_id,parent_id,node_type,name,normalized,description,status,review_status,
                     actor,actor,now,now),
                )
                self._replace_sqlite_aliases(connection, node_id, aliases)
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 위치에 동일한 분류가 이미 있습니다.") from error
        return next(
            item for item in self._flatten_taxonomy_tree(self.list_taxonomy_nodes())
            if item["id"] == node_id
        )

    @staticmethod
    def _flatten_taxonomy_tree(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        pending = list(reversed(items))
        while pending:
            item = pending.pop()
            flattened.append(item)
            pending.extend(reversed(item.get("children", [])))
        return flattened

    def update_taxonomy_node(self, node_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed_keys = {
            "name", "node_type", "parent_id", "aliases", "description",
            "status", "review_status", "updated_by",
        }
        unknown = set(changes) - allowed_keys
        if unknown:
            raise TypeError(f"지원하지 않는 분류 필드: {', '.join(sorted(unknown))}")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM knowledge_taxonomy_node WHERE id=?", (node_id,)
            ).fetchone()
            if current is None:
                return None
            current = dict(current)
            aliases = changes.get("aliases")
            if aliases is None:
                aliases = [row["alias"] for row in connection.execute(
                    "SELECT alias FROM knowledge_taxonomy_alias WHERE node_id=? ORDER BY id",
                    (node_id,),
                ).fetchall()]
            values = {
                **current,
                **{key: value for key, value in changes.items() if value is not None},
                "aliases": aliases,
            }
            validated = self._validate_taxonomy_node(
                name=values["name"], node_type=values["node_type"],
                description=values["description"], status=values["status"],
                review_status=values["review_status"], aliases=values["aliases"],
            )
            name, normalized, node_type, description, status, aliases = validated
            parent_id = values.get("parent_id")
            self._validate_parent(connection, parent_id, node_type, current_id=node_id)
            actor = _clean_short_text(
                str(changes.get("updated_by") or current["updated_by"]),
                label="수정자", maximum=200,
            )
            try:
                connection.execute(
                    """UPDATE knowledge_taxonomy_node SET parent_id=?,node_type=?,name=?,
                       normalized_name=?,description=?,status=?,review_status=?,updated_by=?,updated_at=?
                       WHERE id=?""",
                    (parent_id,node_type,name,normalized,description,status,values["review_status"],
                     actor,datetime.now(UTC).isoformat(),node_id),
                )
                self._replace_sqlite_aliases(connection, node_id, aliases)
            except sqlite3.IntegrityError as error:
                raise ValueError("같은 위치에 동일한 분류가 있거나 연결된 분류를 변경할 수 없습니다.") from error
        return next(
            item for item in self._flatten_taxonomy_tree(self.list_taxonomy_nodes())
            if item["id"] == node_id
        )

    def delete_taxonomy_node(self, node_id: str) -> bool:
        try:
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM knowledge_taxonomy_node WHERE id=?", (node_id,)
                ).fetchone()
                if exists is None:
                    return False
                connection.execute("DELETE FROM knowledge_taxonomy_node WHERE id=?", (node_id,))
        except sqlite3.IntegrityError as error:
            raise ValueError("하위 분류 또는 문서가 연결되어 있어 삭제할 수 없습니다.") from error
        return True

    def _classification_from_connection(
        self, connection: sqlite3.Connection, document_id: str
    ) -> dict[str, Any] | None:
        document = connection.execute(
            "SELECT id,created_by FROM knowledge_document WHERE id=?", (document_id,)
        ).fetchone()
        if document is None:
            return None
        profile = connection.execute(
            "SELECT * FROM knowledge_document_profile WHERE document_id=?", (document_id,)
        ).fetchone()
        linked = [dict(row) for row in connection.execute(
            """SELECT n.* FROM knowledge_document_taxonomy dt
               JOIN knowledge_taxonomy_node n ON n.id=dt.node_id
               WHERE dt.document_id=? ORDER BY n.node_type,n.name""",
            (document_id,),
        ).fetchall()]
        all_nodes = {str(row["id"]): row for row in self._taxonomy_rows(connection)}
        category_paths: set[str] = set()
        products: set[str] = set()
        models: set[str] = set()
        aliases: set[str] = {
            str(row["alias"])
            for row in connection.execute(
                "SELECT alias FROM knowledge_document_alias WHERE document_id=? ORDER BY alias",
                (document_id,),
            ).fetchall()
        }
        for node in linked:
            chain: list[dict[str, Any]] = []
            current = node
            seen: set[str] = set()
            while current and str(current["id"]) not in seen:
                seen.add(str(current["id"]))
                chain.append(current)
                current = all_nodes.get(str(current.get("parent_id") or ""))
            categories = [str(item["name"]) for item in reversed(chain) if item["node_type"] == "category"]
            if categories:
                category_paths.add(" > ".join(categories))
            for item in chain:
                if item["node_type"] == "product_family":
                    products.add(str(item["name"]))
                elif item["node_type"] == "model":
                    models.add(str(item["name"]))
        now = datetime.now(UTC).isoformat()
        base = dict(profile) if profile else {
            "document_id": document_id,
            "document_type": "other",
            "summary": "",
            "review_status": "unclassified",
            "version": 0,
            "reviewed_by": None,
            "reviewed_at": None,
            "updated_by": str(document["created_by"]),
            "updated_at": now,
        }
        return {
            **base,
            "taxonomy_paths": sorted(category_paths),
            "product_families": sorted(products),
            "model_names": sorted(models),
            "aliases": sorted(aliases, key=str.casefold),
        }

    def get_document_classification(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._classification_from_connection(connection, document_id)

    def _get_or_create_sqlite_node(
        self, connection: sqlite3.Connection, *, parent_id: str | None,
        node_type: str, name: str, actor: str,
    ) -> str:
        normalized = normalize_taxonomy_text(name)
        if parent_id is None:
            row = connection.execute(
                "SELECT id FROM knowledge_taxonomy_node WHERE parent_id IS NULL AND node_type=? AND normalized_name=?",
                (node_type, normalized),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id FROM knowledge_taxonomy_node WHERE parent_id=? AND node_type=? AND normalized_name=?",
                (parent_id, node_type, normalized),
            ).fetchone()
        if row:
            return str(row["id"])
        node_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """INSERT INTO knowledge_taxonomy_node
               (id,parent_id,node_type,name,normalized_name,description,status,review_status,
                created_by,updated_by,created_at,updated_at)
               VALUES (?,?,?,?,?,'','active','pending_review',?,?,?,?)""",
            (node_id,parent_id,node_type,name,normalized,actor,actor,now,now),
        )
        return node_id

    def save_document_classification(
        self, document_id: str, *, document_type: str, summary: str,
        taxonomy_paths: list[str], product_families: list[str], model_names: list[str],
        aliases: list[str], review_status: str, updated_by: str,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        if document_type not in DOCUMENT_TYPES:
            raise ValueError("문서 종류가 올바르지 않습니다.")
        if review_status not in CLASSIFICATION_STATUSES:
            raise ValueError("문서 분류 검토 상태가 올바르지 않습니다.")
        clean_summary = str(summary or "").strip()
        if len(clean_summary) > 10_000:
            raise ValueError("문서 요약은 10,000자 이하여야 합니다.")
        paths = _clean_taxonomy_paths(taxonomy_paths)
        products = [_clean_short_text(value, label="제품군", maximum=200) for value in product_families]
        models = [_clean_short_text(value, label="모델명", maximum=200) for value in model_names]
        _validate_classification_shape(paths, products, models)
        clean_aliases = _clean_aliases(aliases)
        actor = _clean_short_text(updated_by, label="수정자", maximum=200)
        with self._connect() as connection:
            document = connection.execute(
                "SELECT 1 FROM knowledge_document WHERE id=?", (document_id,)
            ).fetchone()
            if document is None:
                return None
            current = connection.execute(
                "SELECT version FROM knowledge_document_profile WHERE document_id=?",
                (document_id,),
            ).fetchone()
            current_version = int(current["version"]) if current else 0
            if expected_version is not None and expected_version != current_version:
                raise ValueError("분류 버전이 변경되었습니다. 새로고침 후 다시 저장해 주세요.")
            deepest_categories: list[str] = []
            all_node_ids: set[str] = set()
            for path in paths:
                parent_id: str | None = None
                for name in path:
                    parent_id = self._get_or_create_sqlite_node(
                        connection, parent_id=parent_id, node_type="category",
                        name=name, actor=actor,
                    )
                    all_node_ids.add(parent_id)
                if parent_id:
                    deepest_categories.append(parent_id)
            primary_category = deepest_categories[0] if deepest_categories else None
            product_ids = [self._get_or_create_sqlite_node(
                connection, parent_id=primary_category, node_type="product_family",
                name=name, actor=actor,
            ) for name in dict.fromkeys(products)] if primary_category else []
            all_node_ids.update(product_ids)
            if products and not primary_category:
                raise ValueError("제품군을 저장하려면 상위 분류 경로가 필요합니다.")
            primary_product = product_ids[0] if product_ids else None
            model_ids = [self._get_or_create_sqlite_node(
                connection, parent_id=primary_product, node_type="model",
                name=name, actor=actor,
            ) for name in dict.fromkeys(models)] if primary_product else []
            all_node_ids.update(model_ids)
            if models and not primary_product:
                raise ValueError("모델을 저장하려면 제품군이 필요합니다.")
            mapped_ids = set(product_ids) | set(model_ids)
            if not mapped_ids:
                mapped_ids.update(deepest_categories)
            now = datetime.now(UTC).isoformat()
            reviewed_by = actor if review_status == "reviewed" else None
            reviewed_at = now if review_status == "reviewed" else None
            if review_status == "reviewed" and all_node_ids:
                connection.executemany(
                    """UPDATE knowledge_taxonomy_node
                       SET review_status='reviewed',updated_by=?,updated_at=? WHERE id=?""",
                    [(actor,now,node_id) for node_id in sorted(all_node_ids)],
                )
            new_version = current_version + 1
            connection.execute(
                """INSERT INTO knowledge_document_profile
                   (document_id,document_type,summary,review_status,error_code,version,reviewed_by,
                    reviewed_at,updated_by,updated_at)
                   VALUES (?,?,?,?,NULL,?,?,?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                    document_type=excluded.document_type,summary=excluded.summary,
                    review_status=excluded.review_status,error_code=NULL,version=excluded.version,
                    reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,
                    updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (document_id,document_type,clean_summary,review_status,new_version,
                 reviewed_by,reviewed_at,actor,now),
            )
            connection.execute(
                "DELETE FROM knowledge_document_taxonomy WHERE document_id=?", (document_id,)
            )
            connection.executemany(
                "INSERT INTO knowledge_document_taxonomy(document_id,node_id,relation_type) VALUES (?,?,'primary')",
                [(document_id,node_id) for node_id in sorted(mapped_ids)],
            )
            connection.execute(
                "DELETE FROM knowledge_document_alias WHERE document_id=?", (document_id,)
            )
            connection.executemany(
                "INSERT INTO knowledge_document_alias(document_id,alias,normalized_alias) VALUES (?,?,?)",
                [(document_id,alias,normalize_taxonomy_text(alias)) for alias in clean_aliases],
            )
        return self.get_document_classification(document_id)

    def mark_document_classification_failed(
        self, document_id: str, *, error_code: str, updated_by: str
    ) -> dict[str, Any] | None:
        actor = _clean_short_text(updated_by, label="수정자", maximum=200)
        code = _clean_short_text(error_code, label="오류 코드", maximum=100)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            document = connection.execute(
                "SELECT 1 FROM knowledge_document WHERE id=?", (document_id,)
            ).fetchone()
            if document is None:
                return None
            connection.execute(
                """INSERT INTO knowledge_document_profile
                   (document_id,document_type,summary,review_status,error_code,version,
                    reviewed_by,reviewed_at,updated_by,updated_at)
                   VALUES (?,'other','','failed',?,1,NULL,NULL,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET review_status='failed',
                    error_code=excluded.error_code,version=knowledge_document_profile.version+1,
                    reviewed_by=NULL,reviewed_at=NULL,updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                   WHERE knowledge_document_profile.review_status<>'reviewed'""",
                (document_id,code,actor,now),
            )
        return self.get_document_classification(document_id)

    def mark_document_profile_failed(
        self, document_id: str, *, updated_by: str,
        error_code: str = "classification_failed",
    ) -> dict[str, Any] | None:
        return self.mark_document_classification_failed(
            document_id, error_code=error_code, updated_by=updated_by
        )

    def resolve_hierarchy(
        self, query: str,
        access_scopes: set[str] | frozenset[str] | None = None,
    ) -> HierarchyResolution:
        normalized_scopes = normalize_access_scopes(access_scopes)
        if not normalized_scopes:
            return HierarchyResolution(query, (), frozenset(), False)
        placeholders = ",".join("?" for _ in normalized_scopes)
        with self._connect() as connection:
            nodes = [row for row in self._taxonomy_rows(connection)
                     if row["status"] == "active" and row["review_status"] == "reviewed"]
            links = [dict(row) for row in connection.execute(
                f"""SELECT DISTINCT dt.node_id,dt.document_id
                    FROM knowledge_document_taxonomy dt
                    JOIN knowledge_document_profile p ON p.document_id=dt.document_id
                    JOIN knowledge_document d ON d.id=dt.document_id
                    WHERE p.review_status='reviewed' AND d.status='published'
                      AND d.use_for_ai=1 AND d.visibility='public'
                      AND d.access_scope IN ({placeholders})""",
                normalized_scopes,
            ).fetchall()]
            document_alias_rows = [dict(row) for row in connection.execute(
                f"""SELECT DISTINCT da.document_id,da.alias
                    FROM knowledge_document_alias da
                    JOIN knowledge_document_profile p ON p.document_id=da.document_id
                    JOIN knowledge_document d ON d.id=da.document_id
                    WHERE p.review_status='reviewed' AND d.status='published'
                      AND d.use_for_ai=1 AND d.visibility='public'
                      AND d.access_scope IN ({placeholders})""",
                normalized_scopes,
            ).fetchall()]
        _apply_document_aliases(nodes, links, document_alias_rows)
        reviewed_ids = {str(item["id"]) for item in nodes}
        return _resolve_hierarchy_rows(
            query, nodes, [item for item in links if str(item["node_id"]) in reviewed_ids]
        )

    def retrieve(
        self,
        query: str,
        limit: int,
        access_scopes: set[str] | frozenset[str] | None = None,
        *, document_ids: set[str] | None = None,
    ) -> list[RetrievedChunk]:
        normalized_scopes = normalize_access_scopes(access_scopes)
        if not normalized_scopes:
            return []
        placeholders = ",".join("?" for _ in normalized_scopes)
        with self._connect() as connection:
            rows = connection.execute(f"""
                SELECT d.id AS document_id,d.title,d.original_filename,c.page_number,
                       c.section_title,c.content,p.document_type,
                       p.summary AS document_summary
                FROM knowledge_chunk c JOIN knowledge_document d ON d.id=c.document_id
                LEFT JOIN knowledge_document_profile p
                  ON p.document_id=d.id AND p.review_status='reviewed'
                WHERE d.status='published' AND d.use_for_ai=1 AND d.visibility='public'
                  AND d.access_scope IN ({placeholders})
            """, normalized_scopes).fetchall()
            taxonomy_nodes = [row for row in self._taxonomy_rows(connection)
                              if row["status"] == "active" and row["review_status"] == "reviewed"]
            taxonomy_links = [dict(row) for row in connection.execute(
                """SELECT dt.document_id,dt.node_id
                   FROM knowledge_document_taxonomy dt
                   JOIN knowledge_document_profile p ON p.document_id=dt.document_id
                   WHERE p.review_status='reviewed'"""
            ).fetchall()]
        paths = _document_taxonomy_paths(taxonomy_nodes, taxonomy_links)
        items = [RetrievedChunk(
            **dict(row), taxonomy_path=paths.get(str(row["document_id"]))
        ) for row in rows]
        if document_ids is not None:
            items = [item for item in items if item.document_id in document_ids]
        resolution = self.resolve_hierarchy(query, access_scopes)
        return _rank_with_hierarchy(query, items, limit, resolution)

    def list_search_catalog(
        self,
        access_scopes: set[str] | frozenset[str] | None = None,
        limit: int = 100,
        max_chars: int = 12_000,
    ) -> str:
        normalized_scopes = normalize_access_scopes(access_scopes)
        safe_limit = max(0, min(int(limit), 100))
        if not normalized_scopes or safe_limit == 0 or max_chars <= 0:
            return ""
        placeholders = ",".join("?" for _ in normalized_scopes)
        parameters = (*normalized_scopes, safe_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                WITH catalog_rows AS (
                    SELECT d.id AS document_id,d.title,c.section_title,c.content,
                           p.document_type,p.summary,
                           ROW_NUMBER() OVER (
                               PARTITION BY d.id,COALESCE(c.section_title,'')
                               ORDER BY c.chunk_index
                           ) AS row_number
                    FROM knowledge_chunk c
                    JOIN knowledge_document d ON d.id=c.document_id
                    LEFT JOIN knowledge_document_profile p
                      ON p.document_id=d.id AND p.review_status='reviewed'
                    WHERE d.status='published' AND d.use_for_ai=1
                      AND d.visibility='public'
                      AND d.access_scope IN ({placeholders})
                )
                SELECT document_id,title,section_title,content,document_type,summary
                FROM catalog_rows
                WHERE row_number=1
                ORDER BY title,COALESCE(section_title,'')
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            taxonomy_nodes = [row for row in self._taxonomy_rows(connection)
                              if row["status"] == "active" and row["review_status"] == "reviewed"]
            taxonomy_links = [dict(row) for row in connection.execute(
                """SELECT dt.document_id,dt.node_id
                   FROM knowledge_document_taxonomy dt
                   JOIN knowledge_document_profile p ON p.document_id=dt.document_id
                   WHERE p.review_status='reviewed'"""
            ).fetchall()]
        paths = _document_taxonomy_paths(taxonomy_nodes, taxonomy_links)
        formatted_rows = [
            {**dict(row), "taxonomy_path": paths.get(str(row["document_id"]))}
            for row in rows
        ]
        return _format_search_catalog(
            formatted_rows,
            max_chars=min(int(max_chars), 12_000),
        )

    def has_active_documents(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM knowledge_document WHERE status='published' AND use_for_ai=1 AND visibility='public' AND access_scope='general' LIMIT 1"
            ).fetchone()
        return row is not None
