"""Durable, revision-bound mobile editing. Never rewrites the uploaded source."""

import copy
import hashlib
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.database import Database
from app.models import MobileEditorRevision, Resume
from app.resume_ownership import current_resume_owner
from app.schemas.models import DEFAULT_SECTION_META, ResumeData, ResumeLayoutSettings
from app.services.ats import compute_ats_score
from app.services.refiner import _extract_all_text, _keyword_in_text, job_keyword_terms

SCORING_VERSION = "mobile-visible-v1"
MAX_DOCUMENT_BYTES = 512_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def print_document(document: dict[str, Any]) -> dict[str, Any]:
    """Complete partial metadata so populated sections cannot silently disappear.

    Existing order, labels and explicit hidden choices remain intact. The same
    normalized document is used for scoring, PDF rendering and round-trip checks.
    """
    data = copy.deepcopy(document)
    custom_sections = data.get("customSections") or {}
    saved_metadata = data.get("sectionMeta") or []
    aliases = {
        f"customSections.{key}": key
        for key, section in custom_sections.items()
        if isinstance(section, dict) and key not in ResumeData.model_fields
    }
    # The mobile editor prefers prefixed metadata when both forms exist.
    legacy_metadata = {}
    for meta in saved_metadata:
        if (key := aliases.get(meta.get("key"))) is not None:
            legacy_metadata.setdefault(key, meta)

    metadata = []
    for meta in saved_metadata:
        saved_key = meta.get("key")
        if (key := aliases.get(saved_key)) is not None:
            if legacy_metadata[key] is meta:
                metadata.append(
                    {
                        **meta,
                        "key": key,
                        "isDefault": False,
                        "isVisible": meta.get("isVisible", True),
                    }
                )
        elif saved_key not in legacy_metadata:
            metadata.append(meta)

    known = {meta.get("key") for meta in metadata}
    next_order = max((meta.get("order", 0) for meta in metadata), default=-1) + 1
    for default in DEFAULT_SECTION_META:
        if default["key"] not in known:
            metadata.append({**copy.deepcopy(default), "order": next_order})
            next_order += 1
    for key, section in custom_sections.items():
        if key not in known and isinstance(section, dict):
            metadata.append(
                {
                    "id": key,
                    "key": key,
                    "displayName": key.replace("_", " ").strip().title(),
                    "sectionType": section.get("sectionType", "text"),
                    "isDefault": False,
                    "isVisible": True,
                    "order": next_order,
                }
            )
            next_order += 1
    data["sectionMeta"] = metadata
    return data


def visible_data(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize HTML and omit sections/condensed content absent from the PDF."""
    data = ResumeData.model_validate(print_document(document)).model_dump(mode="json")
    hidden = {
        m.get("key")
        for m in data.get("sectionMeta", [])
        if m.get("isVisible") is False and m.get("key") != "personalInfo"
    }
    for key in hidden:
        data.pop(key, None)
        data.get("customSections", {}).pop(key, None)
    data.pop("sectionMeta", None)
    for entry in data.get("workExperience", []):
        if entry.get("condensed"):
            entry["description"] = []
            entry["location"] = ""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            parser = _Text()
            parser.feed(value)
            return "".join(parser.parts)
        if isinstance(value, list):
            return [clean(x) for x in value]
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if k not in {"id", "descriptionStyles"}
            }
        return value

    return clean(data)


def calculate_analysis(
    document: dict[str, Any], job: dict[str, Any], revision: int
) -> dict[str, Any]:
    """Reuse the original deterministic scorer, with full keyword evidence."""
    job_hash = hashlib.sha256(str(job.get("content", "")).encode()).hexdigest()
    keywords = job.get("job_keywords")
    base = {
        "revision": revision,
        "calculated_at": now(),
        "scoring_version": SCORING_VERSION,
        "job_hash": job_hash,
        "matched_keywords": [],
        "missing_keywords": [],
        "required_skills": [],
        "preferred_skills": [],
        "total_keywords": 0,
        "matched_count": 0,
    }
    if (
        not keywords
        or job.get("job_keywords_hash") != job_hash
        or not job_keyword_terms(keywords)
    ):
        return {
            **base,
            "status": "unavailable",
            "score": None,
            "reason": "Job analysis is unavailable. Your CV can still be edited and exported.",
        }
    # Case-insensitive unique terms, retaining the job posting's order/spelling.
    terms = list(
        {
            term.strip().casefold(): term.strip()
            for term in job_keyword_terms(keywords)
            if term.strip()
        }.values()
    )
    if not terms:
        return {
            **base,
            "status": "unavailable",
            "score": None,
            "reason": "No scorable job keywords were found.",
        }
    data = visible_data(document)
    text = (
        str((data.get("personalInfo") or {}).get("title") or "")
        + " "
        + _extract_all_text(data)
    ).lower()
    matched = [term for term in terms if _keyword_in_text(term, text)]
    missing = [term for term in terms if term not in matched]
    raw = compute_ats_score(
        data, keywords, len(matched) / len(terms) * 100, missing, []
    )
    # Suggestions are evidence, never an instruction to invent experience.
    raw["missing_keywords"] = missing
    raw["recommendations"] = [
        tip.replace("in the Builder", "in Edit CV") for tip in raw["recommendations"]
    ]
    return {
        **base,
        "status": "current",
        "score": raw,
        "matched_keywords": matched,
        "missing_keywords": missing,
        "required_skills": keywords.get("required_skills") or [],
        "preferred_skills": keywords.get("preferred_skills") or [],
        "total_keywords": len(terms),
        "matched_count": len(matched),
    }


class EditorStore:
    """All writes reserve the engine DB writer before reading revision state."""

    def __init__(self, database: Database) -> None:
        self.db = database

    async def timestamps(self, resume_ids: list[str]) -> dict[str, str]:
        """One owner-scoped lightweight query for current library save dates."""
        if not resume_ids:
            return {}
        async with self.db._session() as session:
            rows = await session.execute(
                select(Resume.resume_id, Resume.updated_at).where(
                    Resume.resume_id.in_(resume_ids)
                )
            )
            return dict(rows.all())

    def query(self, resume_id: str) -> Any:
        owner = current_resume_owner()
        if owner is None:
            raise HTTPException(403, "An authenticated owner is required")
        return select(MobileEditorRevision).where(
            MobileEditorRevision.resume_id == resume_id,
            MobileEditorRevision.owner_id == owner,
        )

    async def _latest(self, session: Any, resume_id: str) -> MobileEditorRevision:
        resume = await session.scalar(
            select(Resume).where(Resume.resume_id == resume_id)
        )
        if not resume or resume.is_master or not resume.parent_id:
            raise HTTPException(404, "Saved tailored CV not found")
        row = await session.scalar(
            self.query(resume_id)
            .order_by(MobileEditorRevision.revision.desc())
            .limit(1)
        )
        if row:
            return row
        document = resume.processed_data
        if not document and resume.content_type == "json":
            document = json.loads(resume.content)
        if not isinstance(document, dict):
            raise HTTPException(409, "Saved CV has no editable structured content")
        row = MobileEditorRevision(
            resume_id=resume_id,
            owner_id=current_resume_owner(),
            revision=1,
            document=copy.deepcopy(document),
            confirmed_skills=[],
            created_at=resume.updated_at or now(),
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    def public(row: MobileEditorRevision) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "revision": row.revision,
                "updated_at": row.created_at,
                "resume": row.document,
                "layout": row.layout
                if row.layout is not None
                else ResumeLayoutSettings().model_dump(mode="json"),
                "confirmed_skills": row.confirmed_skills,
                "analysis": row.analysis,
            }
        )

    async def read(self, resume_id: str, revision: int | None = None) -> dict[str, Any]:
        async with self.db._write_session() as session:
            latest = await self._latest(session, resume_id)
            row = (
                latest
                if revision is None
                else await session.scalar(
                    self.query(resume_id).where(
                        MobileEditorRevision.revision == revision
                    )
                )
            )
            if not row:
                raise HTTPException(404, "CV revision not found")
            result = self.public(row)
            if not result["analysis"]:
                previous = await session.scalar(
                    self.query(resume_id)
                    .where(
                        MobileEditorRevision.analysis.is_not(None),
                        MobileEditorRevision.revision < row.revision,
                    )
                    .order_by(MobileEditorRevision.revision.desc())
                    .limit(1)
                )
                if previous and previous.analysis:
                    result["analysis"] = {
                        **copy.deepcopy(previous.analysis),
                        "status": "stale",
                    }
            await session.commit()
            return result

    async def save(
        self,
        resume_id: str,
        expected_revision: int,
        mutation_id: str,
        document: dict[str, Any],
        confirmed_skills: list[str],
        layout: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mutation = {
            "expected_revision": expected_revision,
            "resume": document,
            "confirmed_skills": confirmed_skills,
        }
        if layout is not None:
            mutation["layout"] = layout
        fingerprint = digest(mutation)
        async with self.db._write_session() as session:
            latest = await self._latest(session, resume_id)
            receipt = await session.scalar(
                self.query(resume_id).where(
                    MobileEditorRevision.mutation_id == mutation_id
                )
            )
            if receipt:
                if receipt.mutation_hash != fingerprint:
                    raise HTTPException(
                        409, "Mutation ID was already used for different edits"
                    )
                # Return the exact acknowledged revision. The client reconciles any newer remote edits.
                result = self.public(receipt)
                await session.commit()
                return result
            if latest.revision != expected_revision:
                raise HTTPException(
                    409,
                    {
                        "code": "revision_conflict",
                        "revision": latest.revision,
                        "message": "This CV changed elsewhere. Your local edits have been preserved.",
                    },
                )
            resume = await session.scalar(
                select(Resume).where(Resume.resume_id == resume_id)
            )
            saved_at = now()
            row = MobileEditorRevision(
                resume_id=resume_id,
                owner_id=current_resume_owner(),
                revision=latest.revision + 1,
                document=copy.deepcopy(document),
                layout=copy.deepcopy(layout if layout is not None else latest.layout),
                confirmed_skills=list(confirmed_skills),
                mutation_id=mutation_id,
                mutation_hash=fingerprint,
                created_at=saved_at,
                analysis=(
                    {**copy.deepcopy(latest.analysis), "status": "stale"}
                    if latest.analysis
                    else None
                ),
            )
            resume.processed_data = copy.deepcopy(document)
            resume.content = json.dumps(document, ensure_ascii=False)
            resume.content_type = "json"
            resume.updated_at = saved_at
            session.add(row)
            await session.flush()
            result = self.public(row)
            await session.commit()
            return result

    async def score(
        self, resume_id: str, expected_revision: int, job: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.db._write_session() as session:
            row = await self._latest(session, resume_id)
            if row.revision != expected_revision:
                raise HTTPException(
                    409, "Save the current CV before updating Job Match"
                )
            row.analysis = calculate_analysis(row.document, job, row.revision)
            result = copy.deepcopy(row.analysis)
            await session.commit()
            return result
