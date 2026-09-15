"""Thin Website Builder adapter to the existing Hermes runtime.

Uses the existing Hermes scripted one-shot CLI boundary (`hermes -z`) for
FRONTEND, and a direct programmatic `AIAgent(enabled_toolsets=[])` construction
for FAST (to guarantee zero tool definitions).

This adapter does NOT:
- create a direct 9Router HTTP client
- create an OpenAI client
- duplicate provider resolution
- duplicate authentication
- duplicate model routing
- create another agent framework
- reimplement Hermes

It preserves:
- Website-specific HERMES_HOME (via environment)
- existing provider/runtime resolution
- model selection
- toolset restrictions
- skills
- workspace isolation
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.state import ProjectStateStore

# Hermes runtime seams. These are the exact helpers the Hermes oneshot path
# (`hermes_cli.oneshot._run_agent`) itself uses — no new provider client,
# routing layer, or abstraction is introduced.
#
# Imports are module-level so tests patch the REAL execution seam
# (`app.hermes.adapter.AIAgent`, `.resolve_runtime_provider`, ...), but guarded
# so `import app.hermes.adapter` still succeeds when the Hermes repo root is
# not on sys.path (e.g. tooling that only needs the CLI-boundary FRONTEND
# path). `_run_fast_programmatic` checks for None and returns a failure
# HermesResult instead of raising ImportError at module import time.
try:  # pragma: no cover - exercised indirectly in environments with Hermes
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.models import detect_provider_for_model
    from hermes_cli.oneshot import (
        _build_preloaded_skills_prompt,
        _create_session_db_for_oneshot,
        _oneshot_clarify_callback,
    )
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    _HERMES_IMPORT_ERROR: Optional[str] = None
except ImportError as exc:  # pragma: no cover - depends on host environment
    load_config = None  # type: ignore[assignment]
    get_fallback_chain = None  # type: ignore[assignment]
    detect_provider_for_model = None  # type: ignore[assignment]
    _build_preloaded_skills_prompt = None  # type: ignore[assignment]
    _create_session_db_for_oneshot = None  # type: ignore[assignment]
    _oneshot_clarify_callback = None  # type: ignore[assignment]
    resolve_runtime_provider = None  # type: ignore[assignment]
    AIAgent = None  # type: ignore[assignment]
    _HERMES_IMPORT_ERROR = str(exc)


@dataclass
class HermesResult:
    """Result from a Hermes invocation."""

    success: bool
    response: str = ""
    error: Optional[str] = None
    exit_code: int = 0


class HermesAdapter:
    """Thin adapter to existing Hermes runtime."""

    # Website Builder skills that must be present in the profile-local
    # $HERMES_HOME/skills/ directory so they are discoverable regardless of
    # the current working directory.  The repo copies under .hermes/skills/
    # are the source of truth; this list names the directories to sync.
    _PROFILE_SKILL_NAMES: List[str] = [
        "website-builder-environment",
        "website-builder-product-scope",
        "website-builder-design-dna",
    ]

    def __init__(
        self,
        store: ProjectStateStore,
        hermes_home: Optional[Path] = None,
        repo_root: Optional[Path] = None,
    ):
        self.store = store
        self.hermes_home = hermes_home or Path.home() / ".hermes-website"
        self.repo_root = repo_root or Path(__file__).parent.parent.parent.parent

    # ------------------------------------------------------------------
    # Profile-local skill sync
    # ------------------------------------------------------------------

    def _repo_skills_dir(self) -> Path:
        """Source-of-truth skills directory inside the repository."""
        return self.repo_root / ".hermes" / "skills"

    def _profile_skills_dir(self) -> Path:
        """Profile-local skills directory ($HERMES_HOME/skills/)."""
        return self.hermes_home / "skills"

    @staticmethod
    def _dir_fingerprint(path: Path) -> str:
        """Content-hash of every file under *path*, order-independent."""
        h = hashlib.sha256()
        if not path.is_dir():
            return ""
        for f in sorted(path.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(path)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()

    def sync_skills_to_profile(self) -> None:
        """Copy repo-local Website Builder skills into $HERMES_HOME/skills/.

        Hermes discovers profile-local skills ($HERMES_HOME/skills/) regardless
        of cwd, which makes them available even when FRONTEND runs inside an
        isolated external workspace.  The repo copies under .hermes/skills/
        remain the source of truth; this sync is idempotent — it only writes
        when the content actually differs.
        """
        src_base = self._repo_skills_dir()
        dst_base = self._profile_skills_dir()

        for name in self._PROFILE_SKILL_NAMES:
            src = src_base / name
            dst = dst_base / name

            if not src.is_dir():
                logging.warning("Repo skill source missing: %s", src)
                continue

            # Idempotency: skip when content is identical.
            if dst.is_dir() and self._dir_fingerprint(src) == self._dir_fingerprint(dst):
                continue

            # Remove stale destination and copy fresh.
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            logging.debug("Synced skill %s -> %s", name, dst)

    def _load_role_config(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(self.hermes_home)
        try:
            if load_config is None:
                raise ValueError("Hermes configuration unavailable")
            return load_config()
        finally:
            reset_hermes_home_override(token)

    @staticmethod
    def _role_selection(cfg, role):
        if role not in {"FAST", "FRONTEND", "VISION"}:
            raise ValueError("Unknown Website Builder model role")
        try:
            selection = cfg["website_builder"]["models"][role]
            if not isinstance(selection, dict):
                raise ValueError()
            model, provider = selection["model"], selection["provider"]
            if not all(isinstance(v, str) and v.strip() for v in (model, provider)):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError("Missing or malformed Website Builder model role: " + role) from None
        return model.strip(), provider.strip()

    def _run_hermes_cli(
        self,
        prompt: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        toolsets: Optional[List[str]] = None,
        skills: Optional[List[str]] = None,
        cwd: Optional[Path] = None,
        env_extra: Optional[Dict[str, str]] = None,
        timeout_seconds: int = 300,
        role: Optional[str] = None,
    ) -> HermesResult:
        """Run a single Hermes oneshot turn via the scripted CLI boundary.

        Uses `hermes -z` (or `python -m hermes_cli.main -z`) with explicit
        toolsets and skills. stdout is the final response text.
        """
        if role is not None:
            try:
                if load_config is None:
                    raise ValueError("Hermes configuration unavailable")
                model, provider = self._role_selection(self._load_role_config(), role)
            except Exception as exc:
                return HermesResult(False, error=str(exc), exit_code=1)
        # Ensure profile-local skills are up-to-date so they resolve
        # regardless of the cwd Hermes will run in.
        self.sync_skills_to_profile()

        # Build the command to run hermes oneshot
        # Use the existing Hermes CLI entry point
        # `-z`/`--oneshot` takes the prompt as its immediate argument, so the
        # prompt must directly follow the flag. Any options appended between
        # `-z` and the prompt are consumed by argparse as the oneshot value,
        # which then fails with "argument -z/--oneshot: expected one argument".
        cmd = [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "-z",
            prompt,
        ]

        if model:
            cmd.extend(["--model", model])
        if provider:
            cmd.extend(["--provider", provider])
        if toolsets is not None:
            # Pass explicit toolsets. Empty list means no toolsets flag at all.
            if toolsets:
                cmd.extend(["--toolsets", ",".join(toolsets)])
            # For zero tools, we must NOT pass --toolsets at all, and we must
            # ensure the fallback to config toolsets does not happen.
            # The CLI boundary cannot guarantee zero tools when toolsets is
            # empty because _normalize_toolsets converts [] to None, which
            # falls back to config. For zero-tool FAST, use the programmatic
            # boundary instead.
        if skills:
            for skill in skills:
                cmd.extend(["--skills", skill])

        # Build environment
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        env["HERMES_YOLO_MODE"] = "1"
        env["HERMES_ACCEPT_HOOKS"] = "1"
        if env_extra:
            env.update(env_extra)

        # Run in the specified working directory
        cwd = cwd or self.repo_root

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )

            # stdout is the final one-shot response text
            response = proc.stdout.strip()

            return HermesResult(
                success=proc.returncode == 0,
                response=response,
                error=proc.stderr if proc.returncode != 0 else None,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return HermesResult(
                success=False,
                error=f"Hermes oneshot timed out after {timeout_seconds}s",
                exit_code=124,
            )
        except Exception as exc:
            return HermesResult(
                success=False,
                error=str(exc),
                exit_code=1,
            )

    def _run_fast_programmatic(
        self,
        prompt: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        skills: Optional[List[str]] = None,
        content_parts: Optional[List[Dict[str, Any]]] = None,
        require_vision: bool = False,
        role: Optional[str] = None,
    ) -> HermesResult:
        """Run FAST with a guaranteed zero-tool agent.

        ZERO-TOOLS INVARIANT (verified against checked-out Hermes source):

        - ``hermes_cli.oneshot._normalize_toolsets([])`` returns ``None``
          (``if not toolsets: return None``), so the oneshot helper
          ``_run_agent(toolsets=[], use_config_toolsets=False)`` ends up
          calling ``AIAgent(enabled_toolsets=None)``.
        - ``model_tools._compute_tool_definitions`` treats
          ``enabled_toolsets=None`` as "Default: start with everything"
          (all toolsets enabled). Only a literal empty list produces zero
          tool definitions.

        Therefore FAST must NOT route through ``_run_agent``. It constructs
        ``AIAgent(enabled_toolsets=[])`` directly, reusing the exact same
        Hermes provider/model/fallback/session helpers that ``_run_agent``
        uses (``load_config``, ``detect_provider_for_model``,
        ``resolve_runtime_provider``, ``get_fallback_chain``,
        ``_build_preloaded_skills_prompt``, ``_create_session_db_for_oneshot``,
        ``_oneshot_clarify_callback``). No new provider client, routing layer,
        or abstraction is introduced.
        """
        # Ensure profile-local skills are up-to-date so they resolve
        # regardless of the cwd Hermes will run in.
        self.sync_skills_to_profile()

        if _HERMES_IMPORT_ERROR is not None:
            return HermesResult(
                success=False,
                error=f"Failed to import Hermes oneshot: {_HERMES_IMPORT_ERROR}",
                exit_code=1,
            )

        try:
            cfg = self._load_role_config()

            if role is not None:
                model, provider = self._role_selection(cfg, role)

            # Resolve effective model: explicit arg -> env var -> config.
            # Mirrors hermes_cli.oneshot._run_agent exactly.
            model_cfg = cfg.get("model") or {}
            if isinstance(model_cfg, str):
                cfg_model = model_cfg
            else:
                _raw = model_cfg.get("default") or model_cfg.get("model") or ""
                if isinstance(_raw, dict):
                    from hermes_cli.config import split_model_config_default

                    cfg_model, _ = split_model_config_default(_raw)
                else:
                    cfg_model = str(_raw or "")

            env_model = os.getenv("HERMES_INFERENCE_MODEL", "").strip()
            effective_model = (model or "").strip() or env_model or cfg_model

            # Resolve effective provider: explicit arg -> auto-detect from an
            # explicitly requested model -> env/config default.
            effective_provider = (provider or "").strip() or None
            explicit_base_url_from_alias: Optional[str] = None
            if effective_provider is None and (model or env_model):
                explicit_model = (model or "").strip() or env_model
                if explicit_model:
                    try:
                        from hermes_cli import model_switch as _ms

                        _ms._ensure_direct_aliases()
                        direct = _ms.DIRECT_ALIASES.get(explicit_model.strip().lower())
                    except Exception:
                        direct = None
                    if direct is not None:
                        effective_model = direct.model
                        effective_provider = direct.provider
                        if direct.base_url:
                            explicit_base_url_from_alias = direct.base_url.rstrip("/")
                    else:
                        cfg_provider = ""
                        if isinstance(model_cfg, dict):
                            cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
                        current_provider = (
                            cfg_provider
                            or os.getenv("HERMES_INFERENCE_PROVIDER", "").strip().lower()
                            or "auto"
                        )
                        detected = detect_provider_for_model(explicit_model, current_provider)
                        if detected:
                            effective_provider, effective_model = detected

            runtime = resolve_runtime_provider(
                requested=effective_provider,
                target_model=effective_model or None,
                explicit_base_url=explicit_base_url_from_alias,
            )

            # VISION must resolve to a model/provider that actually supports
            # image input. The logical role name is NOT proof of capability —
            # verify via the same capability lookup the rest of Hermes uses
            # (agent.image_routing._lookup_supports_vision: config override ->
            # models.dev capability data). Fail closed rather than silently
            # sending images to a text-only model.
            if require_vision:
                try:
                    from agent.image_routing import _lookup_supports_vision

                    _vision_provider = str(
                        runtime.get("requested_provider") or runtime.get("provider") or ""
                    ).strip()
                    _vision_model = str(effective_model or "").strip()
                    _supports = _lookup_supports_vision(_vision_provider, _vision_model, cfg)
                except Exception:
                    _supports = None
                if _supports is not True:
                    return HermesResult(
                        success=False,
                        error=(
                            f"VISION model/provider ({_vision_provider}/{_vision_model}) "
                            "does not support image input. Configure a vision-capable "
                            "model for the Website Builder Hermes profile."
                        ),
                        exit_code=1,
                    )

            # Preload skills via the same helper the oneshot path uses.
            # Raises ValueError on unknown skills -> surfaced as failure below.
            skills_prompt = _build_preloaded_skills_prompt(skills)

            session_db = _create_session_db_for_oneshot()
            agent = None
            try:
                # Generic profile fallbacks are not qualified for these roles.
                _fb = [] if role is not None else get_fallback_chain(cfg)

                agent = AIAgent(
                    api_key=runtime.get("api_key"),
                    base_url=runtime.get("base_url"),
                    provider=runtime.get("provider"),
                    requested_provider=runtime.get("requested_provider"),
                    api_mode=runtime.get("api_mode"),
                    model=effective_model,
                    enabled_toolsets=[],  # ZERO-TOOLS INVARIANT: literal empty list
                    quiet_mode=True,
                    platform="cli",
                    session_db=session_db,
                    credential_pool=runtime.get("credential_pool"),
                    fallback_model=_fb or None,
                    ephemeral_system_prompt=skills_prompt,
                    clarify_callback=_oneshot_clarify_callback,
                )

                agent.suppress_status_output = True
                agent.stream_delta_callback = None
                agent.tool_gen_callback = None

                result = agent.run_conversation(content_parts if content_parts else prompt)
                response = result.get("final_response") or ""
                return HermesResult(
                    success=True,
                    response=response,
                    exit_code=0,
                )
            finally:
                if agent is not None:
                    try:
                        session_messages = getattr(agent, "_session_messages", None)
                        if isinstance(session_messages, list):
                            agent.shutdown_memory_provider(session_messages)
                        else:
                            agent.shutdown_memory_provider()
                    except Exception:
                        logging.debug("FAST memory/context cleanup failed", exc_info=True)
                    try:
                        agent.close()
                    except Exception:
                        logging.debug("FAST agent cleanup failed", exc_info=True)
                if session_db is not None:
                    try:
                        session_db.close()
                    except Exception:
                        logging.debug("FAST session store cleanup failed", exc_info=True)
        except Exception as exc:
            return HermesResult(
                success=False,
                error=str(exc),
                exit_code=1,
            )

    def fast_interpret(
        self,
        text: str,
        project_id: Optional[str] = None,
        conversation_context: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Use Hermes FAST role to interpret scope and extract NAME/WHAT/WHY.

        FAST owns semantic interpretation. Application code enforces state transitions.
        FAST receives ZERO tool definitions via direct AIAgent(enabled_toolsets=[]).
        """
        # Build the FAST prompt
        prompt = self._build_fast_prompt(text, conversation_context)

        # FAST uses the programmatic boundary to guarantee zero tools.
        result = self._run_fast_programmatic(
            prompt=prompt,
            role="FAST",
            skills=["website-builder-environment", "website-builder-product-scope"],
        )

        if not result.success:
            # Fallback to deterministic heuristic if Hermes fails
            return self._fallback_fast_interpret(text)

        return self._parse_fast_response(result.response)

    def _build_fast_prompt(
        self, text: str, context: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """Build the FAST interpretation prompt."""
        context_str = ""
        if context:
            context_str = "\n\nPrevious conversation:\n" + "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')}" for m in context[-5:]
            )

        return f"""You are FAST, the scope and requirements interpreter for Website Builder R1.

Your job:
1. Interpret the scope of the user's request as exactly one of:
   WEBSITE, WEBSITE_RELATED, MIXED, OUT_OF_SCOPE, UNCLEAR

2. Extract from the user's text:
   - NAME: the website/business name (if present)
   - WHAT: what the website/business is (if present)
   - WHY: what the visitor should primarily understand or do (if present)

3. Apply the readiness rule exactly:
   - The minimum sufficient website brief is NAME + WHAT + WHY.
   - If NAME, WHAT, and WHY are all materially present, the project proceeds:
     set readiness to DISCOVERY_READY, clarification_needed to false, and
     clarification_question to null.
   - Only set readiness to NEEDS_CLARIFICATION when NAME, WHAT, or WHY itself
     is materially missing or genuinely ambiguous enough that the website
     intent cannot safely proceed. Ask only the smallest question that
     resolves the missing NAME/WHAT/WHY.

4. Missing downstream business facts are NOT blocking and are NOT a
   clarification. A missing WhatsApp number, phone number, email address,
   physical address, booking URL, social URL, opening hours, prices, or any
   other CTA destination/contact detail must NEVER set clarification_needed
   to true by itself. Leave such facts unresolved (null) and still return
   DISCOVERY_READY when NAME + WHAT + WHY are present.

5. Do NOT invent business facts. If WHY mentions a destination (e.g. WhatsApp)
   but no actual phone number or URL is provided, set why_destination to null
   (unresolved). Never fabricate a number, URL, email, address, booking link,
   or any other business fact.

Respond in this exact JSON format:
{{
  "scope": "WEBSITE|WEBSITE_RELATED|MIXED|OUT_OF_SCOPE|UNCLEAR",
  "name": "extracted name or null",
  "what": "extracted what or null",
  "why": "extracted why or null",
  "why_destination": "explicit URL/phone if provided, else null",
  "ambiguity": "description of any material ambiguity or null",
  "clarification_needed": true|false,
  "clarification_question": "smallest blocking question or null",
  "readiness": "DISCOVERY_READY|NEEDS_CLARIFICATION"
}}

User text:{context_str}
{text}
"""

    def _parse_fast_response(self, response: str) -> Dict[str, Any]:
        """Parse FAST JSON response. Falls back to heuristic on parse failure."""
        try:
            # Extract JSON from response (may have markdown fences)
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                return {
                    "scope": data.get("scope", "UNCLEAR"),
                    "name": data.get("name"),
                    "what": data.get("what"),
                    "why": data.get("why"),
                    "why_destination": data.get("why_destination"),
                    "ambiguity": data.get("ambiguity"),
                    "clarification_needed": data.get("clarification_needed", True),
                    "clarification_question": data.get("clarification_question"),
                    "readiness": data.get("readiness", "NEEDS_CLARIFICATION"),
                    "source": "hermes_fast",
                }
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback
        return self._fallback_fast_interpret(response)

    def _fallback_fast_interpret(self, text: str) -> Dict[str, Any]:
        """Deterministic fallback when Hermes FAST is unavailable or fails.

        Delegates to the single canonical deterministic fallback authority:
        ``IntakeProcessor``'s ``_fallback_*`` methods. There is no second
        semantic interpreter here and no duplicated FAST logic. Explicit
        URLs/phones are extracted as ``why_destination``; nothing is fabricated.
        """
        # Import here to avoid a circular dependency at module import time.
        from app.core.intake import IntakeProcessor, Scope

        intake = IntakeProcessor(self.store, hermes_adapter=None)
        scope = intake._fallback_scope(text)
        brief = intake._fallback_extract(text)
        readiness = intake._fallback_readiness(scope, brief)
        clarification_question = intake._fallback_clarification(brief)
        clarification_needed = clarification_question is not None

        return {
            "scope": scope.value if isinstance(scope, Scope) else str(scope),
            "name": brief.get("name"),
            "what": brief.get("what"),
            "why": brief.get("why"),
            "why_destination": brief.get("why_destination"),  # explicit only, never fabricated
            "ambiguity": None,
            "clarification_needed": clarification_needed,
            "clarification_question": clarification_question,
            "readiness": readiness.value if hasattr(readiness, "value") else str(readiness),
            "source": "fallback_heuristic",
        }

    def frontend_propose_directions(
        self,
        brief: Dict[str, Any],
        workspace: Path,
    ) -> Dict[str, Any]:
        """Phase 13: FRONTEND proposes 2-3 LIGHTWEIGHT design directions.

        Bounded and cheap by construction: this call uses the same zero-tool
        programmatic FAST/VISION boundary (``_run_fast_programmatic`` with
        ``enabled_toolsets=[]``) as ``fast_interpret``/``vision_inspect`` — it
        NEVER grants file/terminal tools, so it is structurally incapable of
        writing a workspace or running a full FRONTEND build. Each direction
        is a short text/style descriptor plus a palette swatch only, never a
        rendered mockup or a build artifact.

        Returns ``{"success": bool, "directions": [{"label", "descriptor",
        "palette": {...}}, ...2-3 entries...], "error": Optional[str]}``.
        Fails closed (success=False, empty directions) on any parse failure
        or Hermes error — callers must never fabricate a fallback direction
        set from an empty/failed response.
        """
        prompt = self._build_directions_prompt(brief)

        result = self._run_fast_programmatic(
            prompt=prompt,
            role="FRONTEND",
            skills=["website-builder-product-scope", "website-builder-design-dna"],
        )

        if not result.success:
            return {
                "success": False,
                "directions": [],
                "error": result.error or "FRONTEND direction proposal failed",
            }

        return self._parse_directions_response(result.response)

    def _build_directions_prompt(self, brief: Dict[str, Any]) -> str:
        name = brief.get("name", "Website")
        what = brief.get("what", "")
        why = brief.get("why", "")

        return f"""You are FRONTEND, proposing LIGHTWEIGHT design directions for
Website Builder R1 Phase 13 (no-reference, non-delegated design choice).

Brief:
- Name: {name}
- What: {what}
- Why: {why}

The user has NOT delegated design authority and has NOT provided any
reference images/URLs. Propose exactly 2 or 3 distinct, coherent design
directions. Each direction must be a SHORT, cheap descriptor only:
- a one-line style label
- a one-paragraph descriptor of brand personality / layout feel
- a small palette (primary/secondary/accent hex colors)

Do NOT design a full page. Do NOT write any code. Do NOT invent business
facts (services, pricing, addresses, contact details, testimonials, claims).
This is a lightweight menu of directions for the user to pick from, not a
build.

Respond in this exact JSON format:
{{
  "directions": [
    {{
      "label": "short style label",
      "descriptor": "one paragraph description",
      "palette": {{"primary": "#000000", "secondary": "#ffffff", "accent": "#000000"}}
    }}
  ]
}}
"""

    def _parse_directions_response(self, response: str) -> Dict[str, Any]:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                raw = data.get("directions")
                if isinstance(raw, list) and 2 <= len(raw) <= 3:
                    directions = []
                    for entry in raw:
                        if not isinstance(entry, dict):
                            return {
                                "success": False,
                                "directions": [],
                                "error": "Malformed direction entry",
                            }
                        label = entry.get("label")
                        descriptor = entry.get("descriptor")
                        palette = entry.get("palette")
                        if not (
                            isinstance(label, str)
                            and label
                            and isinstance(descriptor, str)
                            and descriptor
                            and isinstance(palette, dict)
                        ):
                            return {
                                "success": False,
                                "directions": [],
                                "error": "Incomplete direction entry",
                            }
                        directions.append(
                            {"label": label, "descriptor": descriptor, "palette": palette}
                        )
                    return {"success": True, "directions": directions}
                return {
                    "success": False,
                    "directions": [],
                    "error": "FRONTEND must propose exactly 2-3 directions",
                }
        except (json.JSONDecodeError, ValueError):
            pass
        return {
            "success": False,
            "directions": [],
            "error": "Failed to parse FRONTEND directions response",
        }

    def frontend_build(
        self,
        project_id: str,
        brief: Dict[str, Any],
        workspace: Path,
        design_dna_instructions: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Use Hermes FRONTEND role to derive design direction and build the site.

        FRONTEND owns design/build decisions. Application owns workspace/lifecycle.
        FRONTEND does NOT run npm cheap checks — application code does.
        """
        # Build the FRONTEND prompt
        prompt = self._build_frontend_prompt(brief, workspace, design_dna_instructions)

        # FRONTEND uses explicit toolsets: file, terminal, skills
        # It does NOT silently inherit the broader default hermes-cli toolset.
        result = self._run_hermes_cli(
            prompt=prompt,
            role="FRONTEND",
            toolsets=["file", "terminal", "skills"],
            skills=[
                "website-builder-environment",
                "website-builder-product-scope",
                "website-builder-design-dna",
            ],
            cwd=workspace,
            env_extra={
                "PROJECT_ID": project_id,
                "WORKSPACE_ROOT": str(workspace),
            },
            timeout_seconds=900,
        )

        if not result.success:
            # Recoverable timeout: Hermes produced the required artifacts but
            # missed its final response / exit before the subprocess timeout.
            # The generated workspace artifacts and deterministic checks are
            # authoritative for this recovery path.
            if result.exit_code == 124 and self._has_complete_frontend_artifacts(workspace):
                return self._parse_frontend_response("", workspace)
            return {
                "success": False,
                "error": result.error or "FRONTEND build failed",
                "design_dna": None,
            }

        # Parse FRONTEND response for Design DNA
        return self._parse_frontend_response(result.response, workspace)

    def _has_complete_frontend_artifacts(self, workspace: Path) -> bool:
        """Deterministically validate that Phase 7 artifacts were produced.

        Checks:
        - design-dna.json exists and contains valid JSON
        - src/App.tsx exists
        - src/App.tsx is no longer the untouched fixed-starter placeholder

        Returns True only when all conditions hold.
        """
        dna_path = workspace / "design-dna.json"
        if not dna_path.is_file():
            return False
        try:
            with dna_path.open("r", encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, IOError):
            return False

        app_path = workspace / "src" / "App.tsx"
        if not app_path.is_file():
            return False

        starter_app = self.repo_root / "templates" / "frontend-starter" / "src" / "App.tsx"
        if not starter_app.is_file():
            return False

        try:
            current = app_path.read_bytes()
            starter = starter_app.read_bytes()
        except IOError:
            return False

        return current != starter

    def _build_frontend_prompt(
        self,
        brief: Dict[str, Any],
        workspace: Path,
        extra_instructions: Optional[str] = None,
    ) -> str:
        """Build the FRONTEND build prompt.

        FRONTEND does NOT run npm cheap checks. Application code does.
        """
        name = brief.get("name", "Website")
        what = brief.get("what", "")
        why = brief.get("why", "")
        why_destination = brief.get("why_destination")

        cta_note = ""
        if why_destination:
            cta_note = f"\nVerified CTA destination: {why_destination}"
        elif why:
            cta_note = f"\nCTA intent: {why} (destination UNRESOLVED — do not fabricate a URL)"

        instructions = extra_instructions or ""

        return f"""You are FRONTEND, the website designer and builder for Website Builder R1.

Brief:
- Name: {name}
- What: {what}
- Why: {why}{cta_note}

Workspace: {workspace}

Your tasks:
1. Inspect the fixed frontend starter already in the workspace.
2. Derive a concrete design direction based on the brief and UI UX Pro Max guidance.
3. Create a Design DNA document at {workspace}/design-dna.json following the
   minimal Design DNA contract. Do NOT invent business facts.
4. IMMEDIATELY after writing design-dna.json, edit the website source in
   {workspace}/src/ using the fixed starter. Replace the placeholder App.tsx
   with a real implementation.
5. Stop. Do NOT run npm ci, npm run build, or npm run typecheck.
   The application will run deterministic checks after you finish.

Rules:
- Use the fixed frontend starter already in the workspace.
- Do not introduce another frontend stack.
- Never invent business facts (services, pricing, addresses, contact details,
  testimonials, claims).
- If a CTA destination is unresolved, use a placeholder and mark it clearly.
- Keep the build sequential.
- Keep design discovery bounded. Choose a coherent direction quickly rather
  than exhaustively exploring alternatives. Once Design DNA is written,
  immediately implement the website. Creating design-dna.json alone does
  not complete this task.
- The task is incomplete until the starter placeholder in src/ has actually
  been replaced with the website implementation. Do not stop after producing
  Design DNA.

{instructions}

Respond with a JSON summary:
{{
  "success": true|false,
  "design_dna_path": "path to design-dna.json",
  "error": "error message if failed"
}}
"""

    def vision_inspect(
        self,
        desktop_screenshot: Path,
        mobile_screenshot: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Use Hermes VISION role to inspect QA screenshots. Evidence only.

        VISION receives ZERO tool access — it runs through the same zero-tool
        programmatic boundary as FAST (enabled_toolsets=[]), so it never
        writes to the workspace and never calls delegate/terminal/file tools.

        The screenshots are attached as REAL multimodal image input (OpenAI-
        style ``image_url`` content parts) via the existing Hermes-native
        seam ``agent.image_routing.build_native_content_parts`` — the same
        helper the CLI/gateway/TUI image-attachment paths use. This is NOT a
        text prompt containing filesystem paths: VISION receives actual
        pixels. Both desktop and mobile screenshots must attach successfully;
        if either cannot be attached (missing file, unreadable, or otherwise
        skipped by the routing helper), this fails closed rather than running
        VISION half-blind.
        """
        try:
            from agent.image_routing import build_native_content_parts
        except ImportError as exc:
            return {
                "pass": False,
                "critical": [],
                "major": [],
                "minor": [],
                "summary": "",
                "error": f"Failed to import Hermes image routing: {exc}",
            }

        prompt_text = self._build_vision_prompt(brief, design_dna)

        image_paths = [str(desktop_screenshot), str(mobile_screenshot)]
        parts, skipped = build_native_content_parts(prompt_text, image_paths)

        # Fail closed: both screenshots MUST attach. A partial/failed
        # attachment must never let VISION silently run text-only or with
        # only one viewport — that is exactly the "VISION is blind" failure
        # mode this seam exists to prevent.
        if skipped:
            return {
                "pass": False,
                "critical": [],
                "major": [],
                "minor": [],
                "summary": "",
                "error": (
                    "VISION failed to attach required screenshot(s): "
                    f"{skipped}. Both desktop and mobile screenshots must be "
                    "attached as multimodal image input; this is a blocking "
                    "QA condition."
                ),
            }

        result = self._run_fast_programmatic(
            prompt=prompt_text,
            skills=["website-builder-design-dna"],
            content_parts=parts,
            require_vision=True,
            role="VISION",
        )

        if not result.success:
            return {
                "pass": False,
                "critical": [],
                "major": [],
                "minor": [],
                "summary": "",
                "error": result.error or "VISION inspection failed",
            }

        return self._parse_vision_response(result.response)

    def _build_vision_prompt(
        self,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
    ) -> str:
        """Build the VISION inspection prompt. Evidence-only — no edit authority."""
        name = brief.get("name", "Website")
        what = brief.get("what", "")
        why = brief.get("why", "")
        dna_summary = json.dumps(design_dna, indent=2) if design_dna else "(none)"

        return f"""You are VISION, the evidence-only visual QA inspector for Website Builder R1.

Two screenshots are attached to this message:
- Desktop viewport (1440x900)
- Mobile viewport (390x844)

Brief:
- Name: {name}
- What: {what}
- Why: {why}

Design DNA:
{dna_summary}

Your job is to inspect the attached screenshots and report findings. You must
NOT edit files, run commands, or take any action — you have zero tool access.
Report only what you observe:
- missing hero/content
- clipping or overflow
- responsive failures
- overlapping UI
- unreadable text
- severe contrast problems
- broken visual hierarchy
- obvious placeholder leakage
- broken CTA presentation
- major visible accessibility issues
- major mismatch with Design DNA

Do not invent business facts. Do not judge subjective taste beyond the
categories above.

Respond in this exact JSON format:
{{
  "pass": true|false,
  "critical": ["..."],
  "major": ["..."],
  "minor": ["..."],
  "summary": "one paragraph summary"
}}
"""

    def vision_extract_references(
        self,
        reference_files: Dict[str, Path],
    ) -> Dict[str, Any]:
        """Use Hermes VISION to extract per-role characteristics from Phase 12
        design references. Evidence-only — VISION never edits, never copies
        source/assets, and receives ZERO tool access (same zero-tool
        programmatic boundary as ``vision_inspect``/``fast_interpret``).

        ``reference_files`` maps role name (UX/COLOR/LAYOUT/MOTION) to a
        staged image path. Returns ``{"success": bool, "characteristics":
        {role: str}, "error": Optional[str]}``. Fails closed (success=False)
        if any reference cannot be attached as real multimodal image input.
        """
        try:
            from agent.image_routing import build_native_content_parts
        except ImportError as exc:
            return {
                "success": False,
                "characteristics": {},
                "error": f"Failed to import Hermes image routing: {exc}",
            }

        if not reference_files:
            return {"success": True, "characteristics": {}}

        roles = sorted(reference_files.keys())
        prompt_text = self._build_reference_extraction_prompt(roles)
        image_paths = [str(reference_files[role]) for role in roles]
        parts, skipped = build_native_content_parts(prompt_text, image_paths)

        if skipped:
            return {
                "success": False,
                "characteristics": {},
                "error": (
                    "VISION failed to attach required reference image(s): "
                    f"{skipped}. Every reference must attach as multimodal "
                    "image input; this is a blocking condition."
                ),
            }

        result = self._run_fast_programmatic(
            prompt=prompt_text,
            skills=["website-builder-design-dna"],
            content_parts=parts,
            require_vision=True,
            role="VISION",
        )

        if not result.success:
            return {
                "success": False,
                "characteristics": {},
                "error": result.error or "VISION reference extraction failed",
            }

        return self._parse_reference_extraction_response(result.response, roles)

    def _build_reference_extraction_prompt(self, roles: List[str]) -> str:
        role_order = ", ".join(roles)
        return f"""You are VISION, the evidence-only design reference inspector for
Website Builder R1 Phase 12 (multi-reference composition).

Attached images are, in order: {role_order}. Each image contributes ONLY
the named characteristic to the final design:
- UX: interaction patterns, information architecture, navigation feel
- COLOR: palette, contrast, mood
- LAYOUT: grid, spacing, composition, page structure
- MOTION: transitions, animation character, pacing (if depicted)

You must NOT edit files, run commands, or take any action — you have zero
tool access. Describe only the characteristic named for each image as
observed EVIDENCE (a short factual description), never as instructions to
literally copy the image's layout, code, or assets. Do not invent business
facts from the image content.

Respond in this exact JSON format, one entry per attached role in order:
{{
  "characteristics": {{
    "ROLE_NAME": "short evidence-based description"
  }}
}}
"""

    def _parse_reference_extraction_response(
        self, response: str, roles: List[str]
    ) -> Dict[str, Any]:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                from app.core.references import validate_characteristics
                characteristics = validate_characteristics(data.get("characteristics"), roles)
                return {"success": True, "characteristics": characteristics}
        except (json.JSONDecodeError, ValueError):
            pass
        return {
            "success": False,
            "characteristics": {},
            "error": "Failed to parse VISION reference extraction response",
        }

    def _parse_vision_response(self, response: str) -> Dict[str, Any]:
        """Parse VISION JSON response. Fails closed (pass=false) on parse failure."""
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                return {
                    "pass": bool(data.get("pass", False)),
                    "critical": list(data.get("critical") or []),
                    "major": list(data.get("major") or []),
                    "minor": list(data.get("minor") or []),
                    "summary": data.get("summary", ""),
                }
        except (json.JSONDecodeError, ValueError):
            pass

        return {
            "pass": False,
            "critical": [],
            "major": [],
            "minor": [],
            "summary": "",
            "error": "Failed to parse VISION response",
        }

    def _parse_frontend_response(
        self, response: str, workspace: Path
    ) -> Dict[str, Any]:
        """Parse FRONTEND response and load Design DNA from workspace."""
        design_dna = None
        dna_path = workspace / "design-dna.json"
        if dna_path.exists():
            try:
                with dna_path.open("r", encoding="utf-8") as f:
                    design_dna = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        error = None

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                error = data.get("error")
        except (json.JSONDecodeError, ValueError):
            pass

        return {
            "success": error is None,
            "design_dna": design_dna,
            "error": error,
        }
