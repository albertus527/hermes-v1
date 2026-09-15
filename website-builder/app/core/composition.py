"""One persisted policy for first build, revision, and bounded QA repair."""
from app.core.contact_form import compose_contact_form_instructions, decide_contact_method
from app.core.design_dna import validate_typography, typography_violation_message
from app.core.references import persisted_reference_instructions, validate_reference_synthesis
from app.projects.directions import direction_build_instructions, direction_choice_pending


def prebuild_error(state):
    """Admission before workspace creation; queue status alone is insufficient."""
    if state.lifecycle != "QUEUED" or state.revisions.source_revision != 0:
        return "BUILD_NOT_ALLOWED_IN_LIFECYCLE"
    if state.pause_state.get("paused"):
        return "PROJECT_PAUSED"
    if direction_choice_pending(state):
        return "DIRECTION_CHOICE_PENDING"
    if not all(isinstance(state.brief.get(key), str) and state.brief[key].strip()
               for key in ("name", "what", "why")):
        return "REQUIREMENTS_INCOMPLETE"
    return None


def compose_project_instructions(state, *, access_key=None, task=None):
    """Never omit the explicit no-form policy when no backend/link is available."""
    decision = decide_contact_method(
        access_key, state.brief.get("why_destination"),
        enrollment=state.deployment.get("contact"),
    )
    return "\n\n".join(part for part in (
        persisted_reference_instructions(state),
        direction_build_instructions(state.selected_direction),
        compose_contact_form_instructions(decision), task,
    ) if part)


def validate_composed_dna(dna, state):
    if not isinstance(dna, dict) or not dna:
        raise ValueError("MISSING_DESIGN_DNA")
    validate_reference_synthesis(dna, state.design_references)
    if not validate_typography(dna):
        raise ValueError(typography_violation_message(dna))


def invalidate_artifact(state):
    state.revisions.qa_revision = 0
    state.revisions.preview_revision = 0
    state.revisions.approved_revision = 0
    for key in ("qa", "approval", "checked", "tested_snapshot",
                "latest_shown_preview", "preview_intent"):
        state.deployment.pop(key, None)
