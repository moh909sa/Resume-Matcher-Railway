"""Mirror real Resume-Matcher stages to an authenticated, durable mobile feed.

This does NOT invoke an LLM or calculate fake percentages. Engine stage history
comes from the same process running the POST; Supabase allows GET polling from
any Railway replica without sharing process memory.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.services.progress import get_progress_history
from app.services.supabase_backend import SupabaseBackend, SupabaseUnavailable

logger = logging.getLogger(__name__)

# The iOS timeline uses seven conceptual groups, not an artificial progress bar.
# An optional stage that the engine never visits must not gain a green check.
MOBILE_STEPS = (
    "read_job",
    "plan_skills",
    "tailor_resume",
    "keywords_facts",
    "critique_writing",
    "rewrite_lines",
    "verify_review",
)

ENGINE_STAGE_TO_MOBILE_STEP = {
    "load_job_keywords": "read_job",
    "load_original_resume": "read_job",
    "select_bullets": "plan_skills",
    "plan_skill_targets": "plan_skills",
    "generate_resume_diffs": "tailor_resume",
    "apply_resume_diffs": "tailor_resume",
    "generate_resume": "tailor_resume",
    "preserve_source_fields": "tailor_resume",
    "refine_resume": "keywords_facts",
    "critique_writing": "critique_writing",
    "rewrite_writing": "rewrite_lines",
    "final_page_check": "verify_review",
    "register_preview": "verify_review",
    "calculate_diff": "verify_review",
    "serialize_preview": "verify_review",
    "critique_writing_incomplete": "critique_writing",
    "rewrite_writing_incomplete": "rewrite_lines",
}


def _incomplete_steps(history: list[str] | None) -> set[str]:
    """A later stage does not prove a cancelled writing step completed."""
    return {
        ENGINE_STAGE_TO_MOBILE_STEP[stage]
        for stage in history or []
        if stage in ("critique_writing_incomplete", "rewrite_writing_incomplete")
    }


def timeline_from_engine_history(history: list[str] | None) -> dict[str, Any]:
    """Only mark *observed* stages as complete; never infer by elapsed time."""
    visited: list[str] = []
    for raw in history or []:
        step = ENGINE_STAGE_TO_MOBILE_STEP.get(raw)
        if step and step not in visited:
            visited.append(step)
    return {
        "current_step": visited[-1] if visited else None,
        "completed_steps": [step for step in visited[:-1]
                            if step not in _incomplete_steps(history)],
        "visited_steps": visited,
    }


async def mirror_engine_stages(
    backend: SupabaseBackend, user_id: str, progress_id: str
) -> None:
    """At most one Supabase UPDATE per new sequence of real stage transitions."""
    previous: tuple[str, ...] = ()
    while True:
        stages = get_progress_history(progress_id)
        if stages:
            signature = tuple(stages)
            if signature != previous:
                snapshot = timeline_from_engine_history(stages)
                try:
                    await backend.update_mobile_progress(
                        user_id, progress_id, snapshot
                    )
                    previous = signature
                except SupabaseUnavailable:
                    # Do not fail or double-charge the AI request if progress
                    # telemetry temporarily fails; retry the same state next pass.
                    logger.warning("Mobile progress write failed; will retry")
        await asyncio.sleep(0.65)


def finished_progress_update(
    progress_id: str, *, succeeded: bool, tailoring_id: str | None = None
) -> dict[str, Any]:
    """The final status is set only after the draft was actually persisted."""
    history = get_progress_history(progress_id)
    timeline = timeline_from_engine_history(history)
    if succeeded:
        timeline["completed_steps"] = [step for step in timeline["visited_steps"]
                                       if step not in _incomplete_steps(history)]
        timeline["current_step"] = None
    timeline["stage"] = "complete" if succeeded else "failed"
    timeline["status"] = "succeeded" if succeeded else "failed"
    timeline["tailoring_id"] = tailoring_id if succeeded else None
    timeline["finished_at"] = datetime.now(timezone.utc).isoformat()
    return timeline
