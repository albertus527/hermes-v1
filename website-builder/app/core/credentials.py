"""R2-B1: role-scoped process environment boundaries for Website Builder.

WHY THIS EXISTS
---------------
R1 architecture review found a security boundary problem: Hermes agent
subprocesses inherited broad process environment state. ``_run_hermes_cli()``
started from ``os.environ.copy()``, so every privileged deployment
credential in the parent process (GitHub, Vercel, Hostinger, Strix) flowed
into a child that has **file and terminal tools**. A single terminal-tool
invocation could therefore read or exfiltrate a deploy credential that the
role was never authorised to use.

THE CONTRACT
------------
This module is the single place that decides which environment variables a
given *role* may receive. Every subprocess boundary in Website Builder
obtains its environment from here — never from a bare ``os.environ.copy()``.

Roles:

``FAST`` / ``FRONTEND`` / ``VISION``
    Minimum runtime + model environment. No deployment credentials. FRONTEND
    additionally has file/terminal tools, so it gets the *most* restricted
    set that still works.

``build``
    Generated-project shells (npm ci / build / typecheck, vite preview,
    agent-browser). The strictest allowlist: benign system variables only.
    This is the set that already existed on ``ProjectRunner._build_project_env``
    and is deliberately preserved verbatim in behaviour.

``git``
    Benign system variables plus ONLY the Git/SSH configuration the adapter
    needs. A Git SSH private key is referenced by *path* in
    ``GIT_SSH_COMMAND``; the key material is never placed in the environment,
    in argv, in a URL, in git config, or in any state payload.

``vercel``
    Benign system variables plus ONLY Vercel's own deployment credentials.
    The Vercel token reaches the adapter as an in-process constructor
    argument (``VercelAdapter(token=...)``) and is used as an HTTP header —
    see the seam note on :func:`vercel_env` for why the token is deliberately
    NOT also exported into the subprocess environment.

``hostinger`` / ``strix``
    Reserved adapter seams. No adapter is implemented in this batch, but the
    credential-name registration exists now so a future adapter inherits an
    audited, isolated injection point instead of inventing a new one. See
    :func:`adapter_env`.

DESIGN RULES
------------
1. **Allowlist, never denylist.** A denylist cannot enumerate every credential
   an operator's profile may define. Anything unlisted is dropped, so a
   credential that nobody anticipated cannot leak.
2. **Never delete from the parent.** The parent process environment is left
   untouched — normal developer/runtime shell behaviour is preserved and the
   application process keeps its own credentials for its own use.
3. **Names only, no values, in this module.** A credential is injected by
   *name*; the value always comes from the caller. Nothing here reads, logs,
   serialises, or caches a secret value.
4. **No credentials in argv or prompts.** Credentials are passed by
   environment variable or in-process argument, never interpolated into a
   command line, a prompt, Design DNA, generated source, or a
   user-visible diagnostic.

5. **The child is not the whole boundary — its tooling is too.** Scoping what a
   generation role can *authenticate with* says nothing about what its
   ``terminal``/``file`` tools can *read*. Those are two different questions
   and both are answered here:

   - **What the child may authenticate with** — :func:`agent_env`, narrowed to
     the resolved role's provider where Hermes' registry knows it.
   - **What the child's tools may see** — delegated to Hermes' own terminal
     scrub (``_HERMES_PROVIDER_ENV_BLOCKLIST``) and *measured* against it by
     :func:`model_credentials_not_shell_protected`, so the residual is a
     registered, canaried fact rather than an assumption.
   - **What the child's tools can read off disk** — :func:`assert_profile_dotenv_clean`
     and :func:`assert_profile_home_clean`, because ``$HERMES_HOME`` is the one
     directory the generation plane is pointed at, and a secret file there is
     reachable no matter how carefully the environment was scoped.

6. **An unresolvable provider is never starved.** Every narrowing path falls
   back to the wider set rather than dropping a credential. A leaked key is a
   bounded, audited problem; a missing model key breaks every build.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Mapping, Optional

# ---------------------------------------------------------------------------
# Base: benign system variables
# ---------------------------------------------------------------------------
#
# Identical in content to the allowlist that already shipped on
# ``ProjectRunner._build_project_env``. Kept here as the single shared base
# so every role derives from ONE list rather than three drifting copies.

_BENIGN_ENV_EXACT = frozenset({
    "PATH", "PATHEXT",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "TEMP", "TMP", "TMPDIR",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "SHELL", "TERM", "COLORTERM",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "NUMBER_OF_PROCESSORS",
    "OS", "PROCESSOR_ARCHITECTURE",
    # nvm / node version managers sometimes need these to resolve the
    # selected toolchain when invoked inside a subprocess.
    "NVM_DIR", "NVM_BIN", "NVM_HOME", "NVM_SYMLINK",
    "VOLTA_HOME", "FNM_DIR", "FNM_MULTISHELL_PATH",
    # Python interpreter RESOLUTION for the `hermes -z` child. These are
    # lookup paths, not credentials: PYTHONPATH tells the child interpreter
    # where the Hermes package tree is importable from, and VIRTUAL_ENV/
    # VIRTUAL_ENV_PROMPT keep a venv-resolved sys.executable consistent.
    # Dropping them makes `python -m hermes_cli.main` fail with
    # "No module named 'hermes_cli'" whenever Hermes is run from a venv or
    # from a working directory that is not the repo root. They carry no
    # secret and are on agent.secret_scope._GLOBAL_ENV_EXACT for the same
    # reason (process-level resolution, not a profile credential).
    "PYTHONPATH", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT",
    "PYTHONHOME", "PYTHONSTARTUP", "PYTHONWARNINGS", "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "PYTHONNOUSERSITE",
})

# Explicitly allowed prefixes for toolchain/runtime resolution only.
# These prefixes do not carry application credentials.
_BENIGN_ENV_PREFIXES = (
    "XDG_",          # XDG_DATA_HOME etc. — cache/config dirs, not secrets
    "PROGRAMFILES",  # Windows Program Files / Program Files (x86)
    "PROGRAMDATA",   # Windows ProgramData
    "LOCALAPPDATA",  # Windows per-user app data
    "APPDATA",       # Windows roaming app data
)

_BENIGN_ENV_PREFIXES_UPPER = tuple(p.upper() for p in _BENIGN_ENV_PREFIXES)

# ---------------------------------------------------------------------------
# Model / provider environment
# ---------------------------------------------------------------------------
#
# The agent roles need the provider credential its resolved role mapping
# points at. Rather than freezing a hand-maintained list of vendor names
# (which silently breaks whenever a provider is added), the list is derived
# from Hermes' own registry: ``OPTIONAL_ENV_VARS`` entries categorised
# ``provider``, plus the canonical base-URL / non-``OPTIONAL_ENV_VARS``
# provider variables Hermes resolves directly.
#
# Everything here is a MODEL credential. No deployment credential
# (GitHub/Vercel/Hostinger/Strix) is a member, which is what makes the
# FRONTEND boundary hold: a terminal-tool invocation in the workspace
# cannot read a deploy token.

_PROVIDER_ENV_FALLBACK = frozenset({
    # Resolved directly by hermes_cli.runtime_provider / credential pools
    # and not always present in OPTIONAL_ENV_VARS.
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "ANTHROPIC_BASE_URL",
    "COHERE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_BASE_URL", "OPENROUTER_API_KEY",
    "CUSTOM_BASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "NOUS_API_KEY", "NOUS_BASE_URL",
    "HF_TOKEN", "HF_BASE_URL",
    "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
    # Local/self-hosted inference credentials.
    "OLLAMA_API_KEY", "LM_API_KEY",
    # Web3 / auxiliary provider keys.
    "WEB3FORMS_ACCESS_KEY",
})


def _provider_env_names() -> frozenset:
    """Model/provider credential variable names, from Hermes' own registry."""
    names = set(_PROVIDER_ENV_FALLBACK)
    try:
        from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

        for name, meta in OPTIONAL_ENV_VARS.items():
            if isinstance(meta, dict) and meta.get("category") == "provider":
                names.add(name)
    except Exception:  # pragma: no cover - Hermes tree unavailable
        pass
    return frozenset(n for n in names if n)


_PROVIDER_ENV = _provider_env_names()


def _current_provider_env_names() -> frozenset:
    """Provider credential names as Hermes declares them RIGHT NOW.

    ``_PROVIDER_ENV`` is the import-time snapshot, which is what
    :func:`agent_env` injects from and which Remaining Risk #4 documents
    (import-time derivation, fail-safe narrower fallback). That stays as it
    is.

    The ACCOUNTING, though, must not depend on import order. Hermes' plugin
    discovery (``hermes_cli.plugins.discover_plugins``) mutates
    ``hermes_cli.config_defaults.OPTIONAL_ENV_VARS`` IN PLACE, adding
    provider-category entries such as ``CLAUDE_CODE_OAUTH_TOKEN`` after this
    module was imported. So a test that walks the frozen snapshot sees a
    different universe depending on whether another test already triggered
    discovery — an order-dependent test that proves nothing.

    Re-deriving here makes the accounting a property of the live registry
    rather than of the session, and it is strictly MORE conservative: it
    evaluates every name Hermes currently declares, including the ones the
    snapshot predates. Falls back to the snapshot if the registry cannot be
    read.
    """
    return _provider_env_names() or _PROVIDER_ENV

# Model-credential names that Hermes does NOT currently guarantee to be
# invisible to a terminal-spawned shell.
#
# Hermes strips `_HERMES_PROVIDER_ENV_BLOCKLIST` from every shell it spawns
# (`tools/environments/local.py::_sanitize_subprocess_env`), and that blocklist
# is derived from `PROVIDER_REGISTRY` (`api_key_env_vars` + `base_url_env_var`)
# plus `OPTIONAL_ENV_VARS` tool/messaging entries — so every *registered*
# provider's key is already protected, and `tools/env_passthrough.py` refuses
# to re-allow any of them (GHSA-rhgp-j443-p4rf).
#
# `OPTIONAL_ENV_VARS` classifies names as `category: provider` somewhat more
# loosely than the blocklist treats them, so nine names in `_PROVIDER_ENV` fall
# outside the scrub. Measured, not assumed — every one is registered here with
# its reason, so the exception is a complete audited statement rather than an
# invisible gap:
#
#   non-secret routing/identification (correctly absent from a secret scrub):
#     AWS_PROFILE, AWS_REGION, OPENAI_PROJECT_ID
#     — Hermes deliberately leaves the general AWS chain inheritable
#       (SECURITY.md 3.2; see local.py:313-320), and these are pointers/ids,
#       not secrets.
#   endpoint URLs (no credential material, though they can name a private host):
#     CUSTOM_BASE_URL, OPENROUTER_BASE_URL, NOUS_BASE_URL, HERMES_QWEN_BASE_URL
#     — a URL is not an API key. (Hermes treats `AUXILIARY_*_BASE_URL` as
#       sensitive because it is paired with a per-task key; these standalone
#       ones are not.)
#   ACTUAL SECRET-VALUED NAMES — the genuine residual:
#     NOUS_API_KEY, WEB3FORMS_ACCESS_KEY, CLAUDE_CODE_OAUTH_TOKEN
#     — see SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED below.
CREDENTIALS_NOT_SHELL_PROTECTED: frozenset = frozenset({
    "AWS_PROFILE", "AWS_REGION", "OPENAI_PROJECT_ID",
    "CUSTOM_BASE_URL", "OPENROUTER_BASE_URL", "NOUS_BASE_URL",
    "HERMES_QWEN_BASE_URL",
    "NOUS_API_KEY", "WEB3FORMS_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
})

# The subset of the above that actually carries secret material — the part a
# FRONTEND `terminal` call could meaningfully exfiltrate. This is what
# :func:`model_credentials_not_shell_protected` returns, and what the per-spawn
# warning reports, so a real gap is never drowned out by the benign names above.
#
# NOUS_API_KEY / WEB3FORMS_ACCESS_KEY are mitigated by the provider narrowing in
# :func:`_provider_env_names_for` rather than by a new mechanism: neither
# belongs to a registered provider's own credential names, so a role whose
# provider Hermes knows never receives them. They reach a child only on the
# fallback path (custom provider name, a `model_aliases` alias, or a partial
# Hermes tree), where the alternative — refusing to spawn — would break provider
# resolution, which is forbidden and strictly worse than a bounded, warned
# residual.
#
# CLAUDE_CODE_OAUTH_TOKEN is different and worth being explicit about: Hermes
# classifies it as a provider credential (`password: True`) but then
# DELIBERATELY excludes it from the terminal scrub (`tools/environments/local.py`
# :425-434) because stripping it broke agent-spawned `claude` CLIs — the token
# belongs to the user's own Claude Code install, not to Hermes (#55878). So it
# is scrubbed by nothing, by upstream choice. It is also absent from
# ``_PROVIDER_ENV`` at import time and only appears once Hermes' plugin
# discovery mutates ``OPTIONAL_ENV_VARS`` in place, which is why a canary that
# walked the import-time snapshot alone would never have seen it. Listed here so
# the spawn warning fires if it ever does reach a generation role.
SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED: frozenset = frozenset({
    "NOUS_API_KEY", "WEB3FORMS_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
})


def _provider_env_names_for(provider: Optional[str]) -> Optional[frozenset]:
    """Model credential names the *named* provider resolves through.

    Consults BOTH of Hermes' provider registries, because they cover
    different sets and using only one silently disables the narrowing for the
    most commonly configured providers:

    * ``hermes_cli.auth.PROVIDER_REGISTRY`` — the built-in ``ProviderConfig``
      entries, shaped ``api_key_env_vars`` + ``base_url_env_var``.
    * ``providers.get_provider_profile`` — the lazy plugin registry
      (``plugins/model-providers/*``: openrouter, anthropic, gmi, deepseek,
      ...), shaped ``env_vars``. ``openrouter`` is NOT in ``PROVIDER_REGISTRY``;
      reading only that one leaves narrowing inert for it.

    Every name is then **intersected with** :data:`_PROVIDER_ENV`, so neither
    registry can widen the allowlist — this only ever narrows.

    Returns ``None`` when the provider resolves in neither registry (a custom
    or self-hosted entry, a ``model_aliases`` alias, or a Hermes tree that is
    not importable). The caller must then fall back to the full
    :data:`_PROVIDER_ENV` set: an unrecognised provider must never be starved
    of its credential, because a missing model key breaks every role rather
    than merely leaking one.

    A provider that resolves but declares no intersectable name also returns
    ``None``. ``auth_type="oauth_device_code"`` providers are the real case
    (``nous``): they hold no API key, so there is nothing to narrow to, and
    falling back is a no-op rather than a regression.
    """
    key = str(provider or "").strip().lower()
    if not key:
        return None

    names: set = set()
    resolved = False

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        config = PROVIDER_REGISTRY.get(key)
        if config is not None:
            resolved = True
            names.update(getattr(config, "api_key_env_vars", ()) or ())
            base_url_env_var = getattr(config, "base_url_env_var", None)
            if base_url_env_var:
                names.add(base_url_env_var)
    except Exception:  # pragma: no cover - Hermes tree unavailable
        pass

    if not resolved:
        try:
            from providers import get_provider_profile

            profile = get_provider_profile(key)
            if profile is not None:
                resolved = True
                names.update(getattr(profile, "env_vars", ()) or ())
        except Exception:  # pragma: no cover - Hermes tree unavailable
            pass

    if not resolved:
        return None
    narrowed = frozenset(n for n in names if n.upper() in _PROVIDER_ENV)
    return narrowed or None


def _shell_protected_model_names() -> Optional[frozenset]:
    """Model credential names Hermes refuses to expose to a spawned shell.

    Uses the SAME predicate the terminal backend and the env-passthrough
    registry use (``tools.env_passthrough._is_hermes_provider_credential``), so
    there is one authority for "which model credentials a shell can never
    see" and no second list to drift.

    Returns ``None`` when the predicate cannot be imported. The caller then
    skips the assertion rather than failing it: an unavailable Hermes tree
    must not be able to break provider resolution, and it also cannot
    introduce a leak, because the credentials themselves come from the same
    tree.
    """
    try:
        from tools.env_passthrough import _is_hermes_provider_credential
    except Exception:  # pragma: no cover - Hermes tree unavailable
        return None
    try:
        return frozenset(
            name for name in _current_provider_env_names()
            if _is_hermes_provider_credential(name)
        )
    except Exception:  # pragma: no cover - registry raises during partial load
        return None


# Hermes runtime knobs that are process/deployment settings, not credentials.
# Mirrors ``agent.secret_scope._GLOBAL_ENV_EXACT`` for the subset that is
# meaningful in a child process.
_HERMES_RUNTIME_ENV_EXACT = frozenset({
    "HERMES_HOME", "HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS",
    "HERMES_MAX_ITERATIONS", "HERMES_MAX_TOKENS", "HERMES_API_TIMEOUT",
    "HERMES_REDACT_SECRETS", "HERMES_NOUS_TIMEOUT_SECONDS",
    "HERMES_INFERENCE_MODEL", "HERMES_INFERENCE_PROVIDER",
    "HERMES_PROFILE", "HERMES_UID", "HERMES_GID",
    "HERMES_CONTAINER", "HERMES_SKIP_CHMOD", "HERMES_HOME_MODE",
    "HERMES_DEV",
})

# ---------------------------------------------------------------------------
# Privileged deployment credentials — NEVER granted to a generation agent
# ---------------------------------------------------------------------------
#
# These are the names R2 will deepen. They are listed once, explicitly, so
# there is a single auditable statement that a generation role may never
# receive them, and so a canary test can assert exactly this set.

GITHUB_CREDENTIAL_NAMES = frozenset({
    "WEBSITE_BUILDER_GITHUB_SSH_KEY",   # deploy-key PATH (not key material)
    "GITHUB_TOKEN", "GH_TOKEN",
    "GITHUB_PAT",
    "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_USERNAME", "GIT_PASSWORD",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "WEBSITE_BUILDER_GITHUB_REPO",      # non-secret remote, adapter-bound
})

VERCEL_CREDENTIAL_NAMES = frozenset({
    "VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_ORG_ID",
    "VERCEL_AUTOMATION_BYPASS_SECRET",
    "VERCEL_OWNERSHIP_NAMESPACE",
    "VERCEL_API_TOKEN", "VERCEL_SCOPE_ID", "VERCEL_PROJECT_ID",
})

HOSTINGER_CREDENTIAL_NAMES = frozenset({
    "HOSTINGER_TOKEN", "HOSTINGER_API_TOKEN", "HOSTINGER_API_KEY",
    "HOSTINGER_PASSWORD", "HOSTINGER_USERNAME", "HOSTINGER_ORDER_ID",
})

STRIX_CREDENTIAL_NAMES = frozenset({
    "STRIX_TOKEN", "STRIX_API_KEY", "STRIX_API_TOKEN", "STRIX_PASSWORD",
})

# Channel credentials Website Builder itself requires at startup. These are
# not adapter-bound — the Telegram/WhatsApp adapters read them in-process —
# but they are just as privileged as a deploy token: the Telegram bot token
# in particular authorises the outbound channel this whole product speaks
# through.
#
# R2-B1 relied on allowlist OMISSION to keep these out of a generation child.
# That is fail-safe today (nothing here matches a benign, provider, or
# HERMES_* allowlist member) but it is UNENFORCED: `assert_no_privileged`
# could not assert them, so a future widening of any allowlist would leak
# the bot token silently and every canary would stay green. Registering the
# names here closes that gap — `assert_no_privileged` and `_apply_extra` both
# derive from the union below.
#
# Exactly the names the application actually consumes: `TELEGRAM_BOT_TOKEN`
# is required by `app.runtime.load_runtime_config`, and the three WhatsApp
# names are the documented Phase 15 set in `config/default.yaml`. No
# speculative vendor names are added.
MESSAGING_CREDENTIAL_NAMES = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
})

# Adapter-bound credentials, grouped by the adapter that owns them. This is
# the isolated injection seam: a new adapter declares its own names here and
# inherits the audited allowlist machinery, rather than re-deriving
# environment construction.
ADAPTER_CREDENTIALS = {
    "git": GITHUB_CREDENTIAL_NAMES,
    "vercel": VERCEL_CREDENTIAL_NAMES,
    "hostinger": HOSTINGER_CREDENTIAL_NAMES,
    "strix": STRIX_CREDENTIAL_NAMES,
}

# The union of every privileged credential a generation role may never
# receive: every deployment adapter's names plus the channel names above.
PRIVILEGED_CREDENTIAL_NAMES = frozenset(
    name for group in ADAPTER_CREDENTIALS.values() for name in group
) | MESSAGING_CREDENTIAL_NAMES

# Directory names under a generation profile home that hold privileged
# secret material. Enforced by `assert_profile_home_clean`.
#
# Why this exists: `HERMES_HOME` is the one directory the generation plane is
# explicitly pointed at — the FRONTEND child is launched with it, and the
# profile path is even named in the `website-builder-environment` skill — and
# the terminal tool can read any absolute path. So a privileged secret stored
# under it is handed to a tool-bearing role no matter how carefully the
# process environment is scoped. The Vercel automation-bypass store is the
# one live instance; the name is registered so a second one cannot be added
# without tripping the same guard.
PRIVILEGED_SECRET_SUBPATHS = frozenset({
    "vercel-bypass",
})

# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _select(source: Mapping[str, str], names: Iterable[str]) -> Dict[str, str]:
    """Copy the named variables present in *source* (case-insensitive)."""
    selected: Dict[str, str] = {}
    upper_map = {k.upper(): k for k in source}
    for name in names:
        actual = upper_map.get(name.upper())
        if actual is not None:
            selected[actual] = source[actual]
    return selected


def _benign(source: Mapping[str, str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if key in _BENIGN_ENV_EXACT or upper in _BENIGN_ENV_EXACT:
            env[key] = value
        elif upper.startswith(_BENIGN_ENV_PREFIXES_UPPER):
            env[key] = value
    return env


def agent_env(
    role: str,
    *,
    provider: Optional[str] = None,
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Minimum runtime + model environment for a generation agent role.

    *role* is ``FAST``, ``FRONTEND`` or ``VISION``. All three get the SAME
    environment: the model credential set and benign system variables, and
    nothing else. There is deliberately no role-specific widening — a
    capability a role does not need must not be reachable, and the cheapest
    way to guarantee that is for every generation role to share one contract.

    *provider* is the role's already-resolved provider name. When it maps to a
    Hermes ``PROVIDER_REGISTRY`` entry, only THAT provider's credential names
    are injected — one key per role instead of the whole 69-name provider set.
    That is the narrowest useful reduction: it cannot break resolution for a
    known provider, and it cannot widen the allowlist for an unknown one. An
    unrecognised provider (custom/self-hosted entry, ``model_aliases`` alias,
    partial Hermes tree) falls back to the full set, because starving a role
    of its model key breaks every build — a far worse outcome than the extra
    breadth, which is bounded and audited. Callers that want to surface that
    breadth read :func:`model_credentials_not_shell_protected`.

    *extra* holds non-secret per-invocation values (``PROJECT_ID``,
    ``WORKSPACE_ROOT``). It is applied LAST so a caller can set a name that
    the allowlist also carries, but it cannot introduce a name that is not
    already in the allowlist, because callers pass these explicitly.

    Raises ``ValueError`` for an unknown role and ``ValueError`` if *extra*
    attempts to smuggle a privileged deployment credential. That guard is
    what stops a future caller from re-opening this boundary by accident.
    """
    normalized = str(role or "").strip().upper()
    if normalized not in {"FAST", "FRONTEND", "VISION"}:
        raise ValueError("Unknown Website Builder model role")

    raw = os.environ if source is None else source
    # Select each group from the UNFILTERED source. Selecting from the benign
    # dict would silently drop every provider key, because none of them is
    # also a benign system variable.
    env = _benign(raw)
    model_names = _provider_env_names_for(provider) or _PROVIDER_ENV
    env.update(_select(raw, model_names))
    env.update(_select(raw, _HERMES_RUNTIME_ENV_EXACT))

    # Defence in depth: even though the allowlists above already exclude
    # every privileged name, strip them explicitly so a future edit to those
    # sets cannot silently reintroduce a leak.
    for name in PRIVILEGED_CREDENTIAL_NAMES:
        for key in [k for k in env if k.upper() == name.upper()]:
            env.pop(key, None)

    if extra:
        _apply_extra(env, extra)
    return env


def build_env(
    project_id: str,
    workspace,
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Environment for a generated-project shell (npm ci/build/typecheck).

    This is the STRICTEST role: benign system variables only, plus the three
    project identity variables. No model credential and no deployment
    credential is present, so a compromised or buggy build dependency
    (postinstall script) has nothing to exfiltrate.
    """
    env = _benign(os.environ if source is None else source)
    env["HERMES_HOME"] = os.path.join(str(workspace), ".hermes")
    env["PROJECT_ID"] = project_id
    env["WORKSPACE_ROOT"] = str(workspace)
    if extra:
        _apply_extra(env, extra)
    return env


def shell_env(
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Strictest environment: benign system variables only.

    For a generated-project-adjacent shell that has no project identity to
    attach — e.g. the ``agent-browser`` capture helper, and the node/npm
    toolchain version probes. It is a strict subset of :func:`build_env`
    (no ``HERMES_HOME``/``PROJECT_ID``/``WORKSPACE_ROOT``), so nothing here
    can be mistaken for a scoped project invocation.
    """
    env = _benign(os.environ if source is None else source)
    if extra:
        _apply_extra(env, extra)
    return env


def adapter_env(
    adapter: str,
    *,
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Isolated credential injection seam for a deployment adapter.

    Benign system variables plus ONLY the credentials the named adapter
    owns. ``git`` gets its Git/SSH configuration, ``vercel`` gets Vercel's
    deployment credentials, and ``hostinger``/``strix`` are reserved seams
    with no adapter implemented in this batch.

    An unknown adapter name is an error, not a passthrough: a typo must fail
    closed rather than silently produce an unfiltered environment.
    """
    key = str(adapter or "").strip().lower()
    if key not in ADAPTER_CREDENTIALS:
        raise ValueError("Unknown deployment adapter: " + str(adapter))

    raw = os.environ if source is None else source
    # Adapter credentials are selected from the unfiltered source; they are
    # disjoint from the benign system set by construction.
    env = _benign(raw)
    env.update(_select(raw, ADAPTER_CREDENTIALS[key]))
    if extra:
        _apply_extra(env, extra, owned=ADAPTER_CREDENTIALS[key])
    return env


def git_env(
    *,
    ssh_key=None,
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Adapter-bound environment for one Git operation.

    The deploy key stays adapter-bound: when *ssh_key* is given, it is
    referenced by PATH inside ``GIT_SSH_COMMAND`` and is never placed in
    argv, a URL, git config, the environment as a value, or any state. The
    caller (the Git adapter) already owns that decision; this function only
    makes sure the surrounding environment is scoped.

    ``GIT_*`` isolation (``GIT_CONFIG_NOSYSTEM`` and friends) remains owned by
    the Git adapter, which sets it after this environment is built, so the
    existing Git behaviour is unchanged.
    """
    env = adapter_env("git", extra=extra, source=source)
    if ssh_key:
        # The key is a PATH. The material is never read, exported, or
        # interpolated into anything agent- or state-visible.
        env["GIT_SSH_COMMAND"] = (
            "ssh -o BatchMode=yes -o IdentitiesOnly=yes -i " + str(ssh_key)
        )
    return env


def vercel_env(
    *,
    extra: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Adapter-bound environment for one Vercel operation.

    Seam note: the Vercel token is intentionally NOT exported here. It
    already reaches ``VercelAdapter`` in-process as a constructor argument
    and is applied as an HTTP ``Authorization`` header, so no Vercel
    subprocess exists that would need it in the environment. Exporting it
    would add a second copy of a deploy token to the process environment for
    no functional gain. When a future Vercel code path does spawn a
    subprocess, it calls this function — that is the audited seam, and the
    non-secret identifiers are already registered above.
    """
    return adapter_env("vercel", extra=extra, source=source)


def assert_no_privileged(env: Mapping[str, str]) -> None:
    """Raise if *env* contains any privileged credential.

    Used by call sites and canary tests as a single, shared assertion of the
    invariant rather than a re-derived list at each site. The set now covers
    every deployment adapter's names AND the messaging/channel names, so the
    assertion cannot be satisfied by allowlist omission alone.
    """
    leaked = sorted(
        name for name in PRIVILEGED_CREDENTIAL_NAMES
        for key in env if key.upper() == name.upper()
    )
    if leaked:
        raise ValueError(
            "Privileged credential(s) present in environment: "
            + ", ".join(leaked)
        )


def model_credentials_not_shell_protected(
    env: Mapping[str, str],
) -> tuple:
    """Secret-valued model credentials in *env* a spawned shell could read.

    A generation role's child runs the ``terminal`` tool, and the terminal
    backend scrubs ``_HERMES_PROVIDER_ENV_BLOCKLIST`` from every shell it
    spawns. This returns the SECRET-VALUED names that would SURVIVE that scrub
    and therefore be printable with ``env`` / ``printenv`` from a FRONTEND
    terminal call — i.e. the exact residual the previous "a determined
    prompt-injection could ask FRONTEND's terminal tool to print them" warning
    described, now measured instead of assumed.

    Scoped to :data:`SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED` so a
    real gap is never buried under benign non-credential names (base URLs,
    region pointers) that are correctly absent from a secret scrub. An
    unregistered name is reported too — see the assertion form below.

    Returns an empty tuple when the Hermes tree is unavailable: nothing can be
    proven, and nothing can be injected either, since both come from it.

    Callers log the returned names; they do not refuse to spawn, because an
    unrecognised provider must never be starved of its credential.
    """
    protected = _shell_protected_model_names()
    if protected is None:
        return ()
    present = {key.upper() for key in env}
    return tuple(sorted(
        name for name in SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED
        if name not in protected and name.upper() in present
    ))


def unregistered_model_credentials(
    env: Mapping[str, str],
) -> tuple:
    """Model credentials in *env* that are neither scrubbed nor registered.

    The bookkeeping form: every name in :data:`_PROVIDER_ENV` must be either
    shell-protected by Hermes or explicitly listed in
    :data:`CREDENTIALS_NOT_SHELL_PROTECTED`. Anything returned here is an
    undeclared gap — typically because Hermes added a provider without
    extending the blocklist, or narrowed it. Canary-pinned so that regression
    cannot land silently.
    """
    protected = _shell_protected_model_names()
    if protected is None:
        return ()
    registered = {n.upper() for n in CREDENTIALS_NOT_SHELL_PROTECTED}
    present = {key.upper() for key in env}
    return tuple(sorted(
        name for name in _current_provider_env_names()
        if name not in protected and name not in registered
        and name.upper() in present
    ))


def assert_model_credentials_shell_invisible(env: Mapping[str, str]) -> None:
    """Raise if *env* carries a model credential a terminal shell could read.

    The hard form of the checks above, pinned by the canary suite. Spawn sites
    use the non-raising forms so provider resolution is never broken; this
    exists so the property is a testable contract rather than a comment.
    """
    uncovered = unregistered_model_credentials(env)
    if uncovered:
        raise ValueError(
            "Model credential(s) in this environment are neither protected "
            "from the agent terminal tool nor registered as a known "
            "exception: " + ", ".join(uncovered)
        )


def _dotenv_key_names(path) -> frozenset:
    """Variable NAMES declared in a dotenv file. Values are never read.

    Deliberately not ``dotenv_values()``: parsing a credential file into a dict
    of values is the one thing this module must never do, and a name-only scan
    keeps the guard incapable of touching secret material.
    """
    from pathlib import Path as _Path

    names = set()
    try:
        text = _Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            names.add(key)
    return frozenset(names)


def assert_profile_dotenv_clean(hermes_home) -> None:
    """Raise if ``$HERMES_HOME/.env`` declares a privileged credential.

    The ``hermes -z`` child re-runs ``load_hermes_dotenv()``, which loads the
    profile's ``.env`` with ``override=True`` and **unfiltered**
    (``hermes_cli/env_loader.py:499-504``). That runs entirely inside the
    child, after the parent's environment has been scoped — so a privileged
    credential written there bypasses ``agent_env`` entirely and lands in a
    process that holds file and terminal tools. The parent's allowlist cannot
    prevent that; only refusing to spawn against such a profile can.

    Names only: this reads key names, never values, so the error message and
    the logs can name the offending variables safely.

    A missing or unreadable ``.env`` is not an error — a profile may legitimately
    hold no dotenv at all.
    """
    from pathlib import Path as _Path

    env_file = _Path(hermes_home) / ".env"
    if not env_file.is_file():
        return
    declared = {n.upper() for n in _dotenv_key_names(env_file)}
    offenders = sorted(
        name for name in PRIVILEGED_CREDENTIAL_NAMES
        if name.upper() in declared
    )
    if offenders:
        raise ValueError(
            "The Hermes profile .env declares privileged credential(s) that a "
            "generation agent would load into its own environment: "
            + ", ".join(offenders)
            + ". Remove them from the profile .env; deployment credentials "
            "belong to the application process environment, not to the "
            "generation profile."
        )


def assert_profile_home_clean(hermes_home) -> None:
    """Raise if the generation profile home holds a privileged secret store.

    ``HERMES_HOME`` is the one directory the generation plane is explicitly
    pointed at: the FRONTEND child is launched with it, and the profile path
    is even written into the ``website-builder-environment`` skill, so a
    model reading it knows exactly where to look. The terminal tool can read
    any absolute path and runs with ``HERMES_YOLO_MODE=1``, which removes the
    human approval gate. Environment scoping cannot help — the credential is
    on disk. Only refusing to spawn against such a profile can.

    Inspects the profile home's DIRECT children only. That is deliberate: it
    is the reachable, discoverable directory (``ls`` of the profile home), it
    is O(entries) rather than a tree walk over a profile that can hold
    sessions and caches, and it is the shape a registered secret store has.
    A missing profile home is not an error.
    """
    from pathlib import Path as _Path

    home = _Path(hermes_home)
    if not home.is_dir():
        return
    try:
        entries = {child.name for child in home.iterdir()}
    except OSError:
        return
    offenders = sorted(
        name for name in PRIVILEGED_SECRET_SUBPATHS if name in entries
    )
    if offenders:
        raise ValueError(
            "The Hermes profile home contains a privileged secret store: "
            + ", ".join(offenders)
            + ". A generation agent holds file and terminal tools and knows "
            "this path, so a credential stored here is readable regardless "
            "of process-environment isolation. Keep deployment secrets in the "
            "application state root instead."
        )


def _apply_extra(
    env: Dict[str, str],
    extra: Mapping[str, str],
    owned: Optional[Iterable[str]] = None,
) -> None:
    """Apply caller-supplied non-secret overrides, refusing credential names."""
    owned_names = {n.upper() for n in (owned or ())}
    for key, value in extra.items():
        upper = key.upper()
        if upper in {n.upper() for n in PRIVILEGED_CREDENTIAL_NAMES} and upper not in owned_names:
            raise ValueError(
                "Refusing to inject privileged deployment credential into a "
                "non-privileged environment: " + key
            )
        env[key] = str(value)
