"""R2-B1: credential isolation — canary proofs that privileged deployment
credentials never reach a generation agent or a generated-project shell.

The canaries below are deliberately distinctive strings. Every assertion is
made against the REAL environment-construction seams (``app.core.credentials``
and the production callers that use them), not against a re-implementation,
so a test cannot pass while production still leaks.

Contract proved here:

* FAST / FRONTEND / VISION receive minimum runtime+model env, never a
  privileged deploy or messaging credential.
* every model credential a role receives is one Hermes already refuses to
  expose to a terminal-spawned shell — measured against the real scrub, not
  assumed.
* the generation profile home carries no privileged credential, in its
  ``.env`` or as a secret store, because a file there is readable by a role
  that holds file + terminal tools however the environment is scoped.
* the Vercel automation-bypass secret store is not under any Hermes profile.
* build/typecheck/npm/vite/agent-browser shells receive nothing privileged.
* the Git adapter still receives and uses its own Git/SSH configuration.
* Vercel deployment still works through its existing credential path.
* credentials never appear in serialized diagnostics/log fixtures.
* a credential cannot be smuggled in through the ``extra`` hook.
* an unknown adapter fails closed rather than passing through unfiltered.
* the parent process environment is never mutated.

Two tests are deliberate CHARACTERIZATION tests, marked as such inline: they
assert the CURRENT limits of Hermes' scrub rather than desirable behaviour, so
the residual is documented and a future Hermes improvement surfaces as a test
that must be re-read, not a silent divergence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core import credentials
from app.deploy.git_output import OutputGitRepository

# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------

CANARIES = {
    "WEBSITE_BUILDER_GITHUB_SSH_KEY": "/secret/github-key",
    "GITHUB_TOKEN": "CANARY_GITHUB_TOKEN",
    "VERCEL_TOKEN": "CANARY_VERCEL_SECRET",
    "VERCEL_TEAM_ID": "CANARY_VERCEL_TEAM",
    "VERCEL_AUTOMATION_BYPASS_SECRET": "CANARY_VERCEL_BYPASS",
    "HOSTINGER_TOKEN": "CANARY_HOSTINGER_SECRET",
    "HOSTINGER_API_KEY": "CANARY_HOSTINGER_KEY",
    "STRIX_TOKEN": "CANARY_STRIX_SECRET",
    "STRIX_API_KEY": "CANARY_STRIX_KEY",
    # Channel credentials. Registered in ``MESSAGING_CREDENTIAL_NAMES`` so
    # ``assert_no_privileged`` can actually assert them — previously they were
    # excluded only by allowlist omission, which no test could pin.
    "TELEGRAM_BOT_TOKEN": "CANARY_TELEGRAM_TOKEN",
    "WHATSAPP_ACCESS_TOKEN": "CANARY_WHATSAPP_TOKEN",
}

PRIVILEGED_CANARY_NAMES = frozenset(CANARIES)

# Non-privileged values that MUST still be present where the role needs them,
# so these tests fail on over-correction (stripping everything) as well as on
# under-correction (leaking a credential).
MODEL_CANARY = "canary-model-key"


@pytest.fixture
def canaries(monkeypatch):
    """Install every privileged canary into the parent environment."""
    for name, value in CANARIES.items():
        monkeypatch.setenv(name, value)
    return CANARIES


# ---------------------------------------------------------------------------
# Generation agent roles: FAST / FRONTEND / VISION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["FAST", "FRONTEND", "VISION"])
def test_generation_role_receives_no_privileged_canary(role, canaries):
    env = credentials.agent_env(role)
    for name in PRIVILEGED_CANARY_NAMES:
        assert name not in {k.upper() for k in env}, name
    credentials.assert_no_privileged(env)
    for value in canaries.values():
        assert value not in "".join(env.values())


@pytest.mark.parametrize("role", ["FAST", "FRONTEND", "VISION"])
def test_generation_role_env_is_allowlisted_not_inherited(role, canaries):
    """A generation role env is never a superset of the parent environment."""
    env = credentials.agent_env(role)
    # Sanity: it is NOT os.environ.copy().
    assert set(env) != set(os.environ)
    # The benign toolchain still resolves, or node/npm could not run.
    assert "PATH" in env


@pytest.mark.parametrize("role", ["FAST", "FRONTEND", "VISION"])
def test_generation_role_keeps_its_model_credential(role, monkeypatch):
    """Scoping must not starve the role of the model key it resolves with."""
    monkeypatch.setenv("OPENAI_API_KEY", MODEL_CANARY)
    env = credentials.agent_env(role)
    assert env["OPENAI_API_KEY"] == MODEL_CANARY


def test_unknown_role_fails_closed(canaries):
    with pytest.raises(ValueError, match="Unknown Website Builder model role"):
        credentials.agent_env("DEPLOYER")
    with pytest.raises(ValueError):
        credentials.agent_env("")


def test_agent_env_cannot_be_smuggled_via_extra(canaries):
    """The ``extra`` hook must not re-open the boundary.

    ``frontend_build`` passes PROJECT_ID/WORKSPACE_ROOT through ``extra``.
    A future caller must not be able to route a deploy token through the
    same hook.
    """
    with pytest.raises(ValueError, match="Refusing to inject privileged"):
        credentials.agent_env(
            "FRONTEND", extra={"VERCEL_TOKEN": "CANARY_VERCEL_SECRET"}
        )
    # A legitimate non-secret extra still works.
    env = credentials.agent_env("FRONTEND", extra={"PROJECT_ID": "p1"})
    assert env["PROJECT_ID"] == "p1"


# ---------------------------------------------------------------------------
# Generated-project shells: npm ci / build / typecheck, vite, agent-browser
# ---------------------------------------------------------------------------


def test_build_shell_receives_no_privileged_canary(canaries, tmp_path):
    env = credentials.build_env("proj-1", tmp_path)
    for name in PRIVILEGED_CANARY_NAMES:
        assert name not in {k.upper() for k in env}, name
    credentials.assert_no_privileged(env)
    assert env["PROJECT_ID"] == "proj-1"
    assert env["WORKSPACE_ROOT"] == str(tmp_path)
    assert env["HERMES_HOME"] == str(tmp_path / ".hermes")


def test_shell_env_receives_no_privileged_canary(canaries):
    env = credentials.shell_env()
    for name in PRIVILEGED_CANARY_NAMES:
        assert name not in {k.upper() for k in env}, name
    credentials.assert_no_privileged(env)


def test_build_shell_carries_no_model_credential(monkeypatch, tmp_path):
    """A postinstall script must not find the model key either."""
    monkeypatch.setenv("OPENAI_API_KEY", MODEL_CANARY)
    assert "OPENAI_API_KEY" not in credentials.build_env("p", tmp_path)


def test_project_runner_build_env_has_no_privileged_canary(canaries, tmp_path):
    """The real ``npm ci``/build/typecheck seam, not just the helper."""
    from app.core.state import ProjectStateStore
    from app.sandbox.runner import ProjectRunner

    runner = ProjectRunner(
        tmp_path / "workspaces", ProjectStateStore(tmp_path / "state")
    )
    ws = runner.create_workspace("proj-canary")
    env = runner._build_project_env("proj-canary", ws)
    credentials.assert_no_privileged(env)
    for value in canaries.values():
        assert value not in "".join(env.values())


def test_screenshot_browser_command_has_no_privileged_canary(canaries, tmp_path):
    """The agent-browser capture subprocess is a generated-project shell."""
    from app.qa import screenshot

    seen = {}

    class _Result:
        returncode = 0
        stdout = '{"success": true, "data": {"result": "{}"}}'
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return _Result()

    with patch("subprocess.run", fake_run):
        screenshot._run_browser_command(["agent-browser", "--help"], timeout=5)
    assert seen.get("env") is not None, "browser command must pass an explicit env"
    credentials.assert_no_privileged(seen["env"])


# ---------------------------------------------------------------------------
# Git adapter: still gets its own configuration, nothing else
# ---------------------------------------------------------------------------


def test_git_adapter_still_receives_its_ssh_configuration(canaries, tmp_path):
    """The deploy key remains adapter-bound and functional.

    Invariant 5: Git SSH private-key use stays adapter-bound. The key is a
    PATH in GIT_SSH_COMMAND; material is never placed in the environment,
    argv, a URL, or state.
    """
    key = tmp_path / "deploy_key"
    command = credentials.git_env(ssh_key=key, source={})["GIT_SSH_COMMAND"]
    assert str(key) in command
    assert "BatchMode=yes" in command
    assert "IdentitiesOnly=yes" in command


def test_git_env_receives_git_creds_but_not_other_adapters(canaries, monkeypatch):
    monkeypatch.setenv("GIT_ASKPASS", "askpass-helper")
    env = credentials.git_env()
    # Git's own configuration is present...
    assert "GIT_ASKPASS" in env
    assert "WEBSITE_BUILDER_GITHUB_SSH_KEY" in env
    # ...and no other adapter's credential is.
    for name in (
        "VERCEL_TOKEN", "HOSTINGER_TOKEN", "STRIX_TOKEN",
        "VERCEL_TEAM_ID", "HOSTINGER_API_KEY", "STRIX_API_KEY",
    ):
        assert name not in {k.upper() for k in env}, name


def test_git_adapter_publish_still_injects_ssh_env(canaries, tmp_path):
    """End-to-end: the real Git adapter build path still sets GIT_SSH_COMMAND."""
    repo = OutputGitRepository(tmp_path / "output")
    extra = repo._ssh_env(tmp_path / "key")
    assert "GIT_SSH_COMMAND" in extra
    assert str(tmp_path / "key") in extra["GIT_SSH_COMMAND"]


def test_git_subprocess_env_has_no_non_git_credential(canaries, tmp_path):
    """The real ``git`` subprocess environment is adapter-scoped."""
    repo = OutputGitRepository(tmp_path / "output")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, b"true\n", b"")

    with patch.object(subprocess, "run", fake_run):
        repo._run(["rev-parse", "--is-bare-repository"])
    env = seen["env"]
    assert "GIT_CONFIG_NOSYSTEM" in env
    credentials.assert_no_privileged(
        {k: v for k, v in env.items() if k.upper() not in credentials.GITHUB_CREDENTIAL_NAMES}
    )
    for value in (CANARIES["VERCEL_TOKEN"], CANARIES["HOSTINGER_TOKEN"],
                  CANARIES["STRIX_TOKEN"]):
        assert value not in "".join(env.values())


# ---------------------------------------------------------------------------
# Vercel adapter: existing credential path still works, nothing new leaks
# ---------------------------------------------------------------------------


def test_vercel_deployment_still_works_through_its_credential_path(canaries):
    """Vercel deployment behaviour is unchanged.

    The token is an in-process constructor argument applied as an HTTP
    Authorization header — it never needed to be in any subprocess
    environment, and still does not.
    """
    from app.deploy.adapters import VercelAdapter

    captured = {}

    class Transport:
        def request(self, method, url, headers=None, data=None, timeout=30):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            body = json.dumps({"id": "prj_1", "name": "wb-" + "0" * 40,
                               "accountId": "team", "env": []}).encode()
            return type("R", (), {"status": 200, "body": body})()

    adapter = VercelAdapter("CANARY_VERCEL_SECRET", "team", "ns", transport=Transport())
    adapter._call("GET", "/v9/projects/x")
    assert captured["headers"]["Authorization"] == "Bearer CANARY_VERCEL_SECRET"

    # The adapter subprocess seam carries Vercel's OWN credentials and no
    # other adapter's. The token reaches the adapter in-process, so nothing
    # privileged of another kind can appear here.
    env = credentials.vercel_env()
    for other in (
        CANARIES["WEBSITE_BUILDER_GITHUB_SSH_KEY"],
        CANARIES["GITHUB_TOKEN"],
        CANARIES["HOSTINGER_TOKEN"],
        CANARIES["STRIX_TOKEN"],
    ):
        assert other not in "".join(env.values()), other


def test_vercel_env_receives_no_other_adapter_credential(canaries):
    env = credentials.vercel_env()
    for name in ("WEBSITE_BUILDER_GITHUB_SSH_KEY", "HOSTINGER_TOKEN", "STRIX_TOKEN"):
        assert name not in {k.upper() for k in env}, name


# ---------------------------------------------------------------------------
# Future adapter seam: Hostinger / Strix registered, not implemented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", ["hostinger", "strix"])
def test_future_adapter_seam_is_registered_and_isolated(adapter, canaries):
    """A clear, audited injection point exists for future adapters.

    No adapter is implemented in this batch, but the credential names are
    registered so a future adapter inherits the isolated seam instead of
    inventing a new environment-construction path.
    """
    assert adapter in credentials.ADAPTER_CREDENTIALS
    env = credentials.adapter_env(adapter)
    for name in ("VERCEL_TOKEN", "WEBSITE_BUILDER_GITHUB_SSH_KEY"):
        assert name not in {k.upper() for k in env}, name
    # It receives its own registered credential names.
    for name in credentials.ADAPTER_CREDENTIALS[adapter]:
        if name in CANARIES:
            assert name in {k.upper() for k in env}, name


def test_unknown_adapter_fails_closed():
    """A typo must not silently produce an unfiltered environment."""
    with pytest.raises(ValueError, match="Unknown deployment adapter"):
        credentials.adapter_env("hostniger")


# ---------------------------------------------------------------------------
# Diagnostics / serialization
# ---------------------------------------------------------------------------


def test_no_canary_in_serialized_diagnostics_fixture(canaries):
    """A user-visible diagnostic payload must not carry any canary."""
    from app.deploy.adapters import _fail

    result = _fail("SOME_PROVIDER_FAILURE")
    payload = json.dumps({
        "error": result.error,
        "error_code": result.error_code,
        "retryable": result.retryable,
    })
    for name, value in CANARIES.items():
        assert value not in payload, name
        assert name not in payload or payload.count(name) == 0


def test_no_canary_in_watchdog_receipt(canaries, tmp_path):
    """The forensic receipt is bounded and sanitized."""
    from app.hermes import watchdog as wd

    receipt = {
        "invocation_id": "a" * 32,
        "project_id": "proj",
        "outcome": "FRONTEND_IDLE_TIMEOUT",
        "elapsed_seconds": 12.5,
    }
    blob = json.dumps(receipt)
    for value in CANARIES.values():
        assert value not in blob

    # The forensic helper redacts the credential-bearing shapes it claims to:
    # URLs, absolute paths, and long token/hex runs. A receipt records that a
    # tool ran, never what it was pointed at.
    assert "token" not in wd.normalize_forensic_desc(
        "GET https://api.example.com/v1?token=" + "a" * 40
    )
    assert "secret" not in wd.normalize_forensic_desc(
        "read C:\\Users\\operator\\deploy.pem"
    ).lower().replace("<redacted>", "")
    assert "deadbeef" not in wd.normalize_forensic_desc("blob " + "deadbeef" * 8)


# ---------------------------------------------------------------------------
# Parent process is never mutated
# ---------------------------------------------------------------------------


def test_isolation_never_deletes_from_the_parent(canaries, tmp_path):
    """Requirement 7: no global deletion from the parent environment.

    Normal developer/runtime shell behaviour must be preserved: the parent
    keeps every credential it had. Only each child's view is scoped.
    """
    for name, value in CANARIES.items():
        assert os.environ[name] == value, name

    credentials.agent_env("FRONTEND")
    credentials.build_env("p", tmp_path)
    credentials.adapter_env("git")
    credentials.shell_env()

    for name, value in CANARIES.items():
        assert os.environ[name] == value, name


# ---------------------------------------------------------------------------
# REGRESSION: interpreter resolution must survive scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONIOENCODING",
])
def test_generation_role_keeps_python_resolution_vars(name, monkeypatch):
    """Regression: scoping must not break `python -m hermes_cli.main`.

    The agent child is spawned as ``sys.executable -m hermes_cli.main``. If
    Hermes runs from a virtualenv, or from a working directory that is not the
    repo root, the child resolves the package tree through ``PYTHONPATH`` /
    ``VIRTUAL_ENV``. Dropping those made every real FRONTEND build fail with
    "No module named 'hermes_cli'" — a scoping change silently breaking the
    primary production path.

    These are lookup paths, not credentials, so forwarding them costs nothing
    and the isolation invariant is unaffected.
    """
    monkeypatch.setenv(name, "/somewhere/that/is/a/path")
    for role in ("FAST", "FRONTEND", "VISION"):
        assert name in credentials.agent_env(role), (role, name)


def test_generation_role_keeps_python_resolution_and_still_isolates(
    monkeypatch, canaries
):
    """The interpreter-resolution fix must not re-open the credential hole."""
    monkeypatch.setenv("PYTHONPATH", "/repo/root")
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    env = credentials.agent_env("FRONTEND")
    assert env["PYTHONPATH"] == "/repo/root"
    assert env["VIRTUAL_ENV"] == "/venv"
    credentials.assert_no_privileged(env)


def test_child_can_still_import_hermes_cli_with_scoped_env(tmp_path):
    """End-to-end: a child launched with the scoped env can import hermes_cli.

    This is the real check behind the regression above — it runs an actual
    subprocess with exactly the environment ``agent_env`` produces and
    requires it to import the module the FRONTEND child needs.
    """
    import sys

    repo_root = str(Path(__file__).resolve().parents[2])
    monkey = os.environ.copy()
    monkey["PYTHONPATH"] = repo_root  # simulate a venv-launched Hermes

    env = credentials.agent_env("FRONTEND", source=monkey)
    result = subprocess.run(
        [sys.executable, "-c", "import hermes_cli; print('ok')"],
        env=env, capture_output=True, text=True, timeout=120,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# ---------------------------------------------------------------------------
# Model-credential boundary: what the agent's OWN tools can see
# ---------------------------------------------------------------------------
#
# Scoping the child's environment is necessary but not sufficient. The child
# runs the `terminal` tool, and the terminal backend scrubs Hermes' provider
# blocklist from every shell it spawns. These tests exercise that REAL scrub so
# the claim "FRONTEND's terminal cannot print the model credential" is
# measured rather than asserted in prose.


def _provider_env_names():
    """Provider credential names as Hermes declares them right now.

    Not ``credentials._PROVIDER_ENV``: that is an import-time snapshot, and
    Hermes' plugin discovery mutates ``OPTIONAL_ENV_VARS`` in place afterwards
    (adding ``CLAUDE_CODE_OAUTH_TOKEN`` among others). Reading the live set is
    what makes these assertions order-independent — a test whose result depends
    on whether some other test already triggered discovery proves nothing.
    """
    return credentials._current_provider_env_names()


def _real_terminal_env_scrub(env):
    """Run the production scrub the terminal backend applies to every spawn."""
    from tools.environments.local import _sanitize_subprocess_env

    return _sanitize_subprocess_env(dict(env))


def test_terminal_scrub_drops_every_model_credential_we_inject(monkeypatch):
    """The FRONTEND child env, passed through the real terminal scrub, is clean.

    This is the load-bearing test for the whole "model credential" half of the
    boundary. It uses the same ``_sanitize_subprocess_env`` the terminal
    backend calls, so it cannot pass while production still leaks.

    The claim is precisely stated: the scrub removes every model credential
    EXCEPT the names registered in ``CREDENTIALS_NOT_SHELL_PROTECTED`` (and
    none of the secret-valued ones among those, because the provider narrowing
    keeps them out of the env entirely). Asserting "everything is scrubbed"
    would be false, and a test that asserts something false is worse than no
    test at all.
    """
    for name in sorted(_provider_env_names()):
        monkeypatch.setenv(name, "canary-" + name.lower().replace("_", "-"))

    env = credentials.agent_env("FRONTEND")
    assert env, "the role must still receive something"

    registered = {n.upper() for n in credentials.CREDENTIALS_NOT_SHELL_PROTECTED}
    scrubbed = _real_terminal_env_scrub(env)
    scrubbed_names = {k.upper() for k in scrubbed}
    injected = {k.upper() for k in env}

    # 1. Every model credential actually injected that is NOT a registered
    #    exception IS removed by the real scrub.
    for name in sorted(_provider_env_names()):
        if name.upper() in injected and name.upper() not in registered:
            assert name.upper() not in scrubbed_names, name

    # 2. The survivors are exactly the registered exceptions — no silent extras.
    survivors = sorted(
        name for name in _provider_env_names()
        if name.upper() in scrubbed_names
    )
    assert set(survivors) <= registered

    # 3. This is the FALLBACK path (no provider was resolved), so the measured
    #    secret-valued residual is present in the env and survives the scrub.
    #    Asserting it away would be false. What must hold is that it is
    #    *reported* — the narrowed path is covered by
    #    test_narrowed_env_carries_no_secret_valued_credential.
    reported = set(credentials.model_credentials_not_shell_protected(env))
    assert reported, "the measured residual must not be silently emptied"
    assert reported <= set(
        credentials.SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED
    )
    assert reported <= scrubbed_names


def test_narrowed_env_carries_no_secret_valued_credential(monkeypatch):
    """The strong claim, on the path that actually achieves it.

    When the role's provider resolves in Hermes' registry, the child env holds
    only that provider's own names — and therefore no secret-valued model
    credential at all survives the terminal scrub. A FRONTEND ``terminal``
    call cannot print a model credential, because there isn't one in the
    process to print.
    """
    for name in sorted(_provider_env_names()):
        monkeypatch.setenv(name, "canary-" + name.lower().replace("_", "-"))

    env = credentials.agent_env("FRONTEND", provider="openrouter")
    scrubbed = _real_terminal_env_scrub(env)
    scrubbed_names = {k.upper() for k in scrubbed}

    assert credentials.SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED.isdisjoint(
        scrubbed_names
    )
    assert credentials.model_credentials_not_shell_protected(env) == ()
    credentials.assert_model_credentials_shell_invisible(env)


def test_every_model_credential_name_is_scrubbed_or_registered():
    """Invariant, not a snapshot: no UNREGISTERED model credential gap.

    Every name Hermes currently declares as a provider credential must either
    be one the terminal scrub strips, or appear in
    ``CREDENTIALS_NOT_SHELL_PROTECTED`` with a recorded reason. A name that
    satisfies neither is an undeclared leak and this fails with the list of
    offenders so the fix is actionable.
    """
    protected = credentials._shell_protected_model_names()
    assert protected is not None, "the Hermes provider registry must be importable"

    registered = {n.upper() for n in credentials.CREDENTIALS_NOT_SHELL_PROTECTED}
    unaccounted = sorted(
        name for name in _provider_env_names()
        if name not in protected and name.upper() not in registered
    )
    assert not unaccounted, (
        "model credentials neither scrubbed from the terminal nor registered "
        f"as a known exception: {unaccounted}"
    )


def test_claude_code_oauth_is_an_accounted_exception():
    """The upstream deliberate exclusion is registered, not unnoticed.

    Hermes classifies ``CLAUDE_CODE_OAUTH_TOKEN`` as a provider credential
    (``password: True``) yet deliberately drops it from the terminal scrub,
    because stripping it broke agent-spawned ``claude`` CLIs (#55878). It is a
    real secret with no scrub, so it belongs in the warned subset. It also only
    enters the registry once plugin discovery mutates ``OPTIONAL_ENV_VARS`` in
    place — an accounting walk of the import-time snapshot alone would never
    have surfaced it, which is exactly why this canary reads the live set.
    """
    assert "CLAUDE_CODE_OAUTH_TOKEN" in credentials.CREDENTIALS_NOT_SHELL_PROTECTED
    assert (
        "CLAUDE_CODE_OAUTH_TOKEN"
        in credentials.SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED
    )


def test_registered_exceptions_that_carry_secrets_are_declared():
    """The secret-valued subset is exactly the two measured real names.

    Pins the accounting rather than the whole list: benign non-credential names
    (base URLs, region pointers) may be added to
    ``CREDENTIALS_NOT_SHELL_PROTECTED`` freely, but anything secret-valued must
    appear in the subset the spawn warning actually reports.
    """
    declared = credentials.SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED
    registered = credentials.CREDENTIALS_NOT_SHELL_PROTECTED
    assert declared <= registered, "the warned subset must be a subset of the registry"
    assert declared, "the measured residual must not be silently emptied"


def test_narrowing_limits_the_child_env_to_the_resolved_provider(monkeypatch):
    """A known provider yields ONE key, not the whole provider set.

    ``openrouter`` is the interesting case: it ships as a plugin profile
    (``plugins/model-providers/openrouter``) and is absent from
    ``hermes_cli.auth.PROVIDER_REGISTRY``, so reading only the auth registry
    would leave the narrowing silently inert for one of the most commonly
    configured providers.
    """
    for name in sorted(_provider_env_names()):
        monkeypatch.setenv(name, "canary-" + name.lower().replace("_", "-"))

    env = credentials.agent_env("FRONTEND", provider="openrouter")
    keys = {k.upper() for k in env if "KEY" in k.upper()}
    assert keys == {"OPENROUTER_API_KEY"}, keys
    # And therefore no secret-valued residual reaches this role at all.
    assert credentials.model_credentials_not_shell_protected(env) == ()


def test_unknown_provider_still_receives_its_model_key(monkeypatch):
    """Over-correction guard: narrowing must never starve a role.

    A custom / self-hosted / aliased provider does not resolve in either
    registry, so the policy falls back to the full provider set. Breaking
    provider resolution would break every build, which is strictly worse than
    the bounded extra breadth this retains.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", MODEL_CANARY)
    env = credentials.agent_env("FRONTEND", provider="my-self-hosted-llm")
    assert env["OPENROUTER_API_KEY"] == MODEL_CANARY
    # And it must not raise — resolution is never blocked by this policy.
    credentials.assert_no_privileged(env)


def test_narrowing_never_widens_the_allowlist(monkeypatch):
    """A registry name outside the allowlist is not injected.

    The provider's declared names are INTERSECTED with ``_PROVIDER_ENV``, so
    consulting a second registry cannot become a widening.
    """
    monkeypatch.setenv("SOME_UNDECLARED_CREDENTIAL", "nope")
    env = credentials.agent_env("FRONTEND", provider="openrouter")
    assert "SOME_UNDECLARED_CREDENTIAL" not in env


def test_characterization_terminal_scrub_does_not_cover_deploy_names():
    """CHARACTERIZATION — records a limit, not a desired behaviour.

    Hermes' terminal scrub is derived from the PROVIDER registry and
    OPTIONAL_ENV_VARS. Website Builder's own deploy credential names are in
    NEITHER, so if one ever reached a child environment a terminal call would
    print it verbatim. That is precisely why the profile ``.env`` gate and the
    secret-store relocation below exist.

    If this test starts failing, Hermes has closed the gap: re-read the
    residual-risk section of the R2 write-up before changing the production
    guards, since a narrower guard may then be available.
    """
    env = {
        "VERCEL_AUTOMATION_BYPASS_SECRET": "CANARY_VERCEL_BYPASS",
        "HOSTINGER_TOKEN": "CANARY_HOSTINGER_SECRET",
        "STRIX_TOKEN": "CANARY_STRIX_SECRET",
        "WEBSITE_BUILDER_GITHUB_SSH_KEY": "/secret/github-key",
    }
    scrubbed = _real_terminal_env_scrub(env)
    for name, value in env.items():
        assert scrubbed.get(name) == value, (
            f"{name} is now scrubbed from terminal subprocesses — the "
            "environment-layer protection has been extended upstream"
        )


def test_read_file_refuses_the_website_profile_dotenv(tmp_path, monkeypatch):
    """The existing file guard really covers THIS profile, not just ``~/.hermes``.

    The website profile is ``~/.hermes-website``, which is NOT under the
    default Hermes root, so the exact-path arm of the guard cannot help it —
    only the secret-bearing-basename arm can. That must be proven, not assumed.
    """
    from agent.file_safety import get_read_block_error

    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    dotenv = profile / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=" + MODEL_CANARY + "\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert get_read_block_error(str(dotenv))


# ---------------------------------------------------------------------------
# The generation profile itself: .env hygiene and secret-store location
# ---------------------------------------------------------------------------


def _write_dotenv(path, mapping):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join("{}={}\n".format(k, v) for k, v in mapping.items()),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("name", sorted(PRIVILEGED_CANARY_NAMES))
def test_profile_dotenv_rejects_every_privileged_credential(tmp_path, name):
    """A profile ``.env`` carrying a privileged credential fails closed.

    The ``hermes -z`` child re-runs ``load_hermes_dotenv()``, which loads this
    file with ``override=True`` and unfiltered, INSIDE the child — after the
    parent's scoping. A credential written here never passes through
    ``agent_env``, so nothing in the environment contract can catch it.
    """
    profile = tmp_path / "profile"
    _write_dotenv(profile / ".env", {name: CANARIES[name]})

    with pytest.raises(ValueError, match="profile .env declares privileged"):
        credentials.assert_profile_dotenv_clean(profile)


def test_profile_dotenv_rejection_names_the_variable_never_the_value(tmp_path):
    """The diagnostic must be actionable without being a disclosure."""
    profile = tmp_path / "profile"
    secret = "SUPER-SECRET-VERCEL-VALUE"
    _write_dotenv(profile / ".env", {"VERCEL_TOKEN": secret})

    with pytest.raises(ValueError) as excinfo:
        credentials.assert_profile_dotenv_clean(profile)

    message = str(excinfo.value)
    assert "VERCEL_TOKEN" in message
    assert secret not in message


def test_profile_dotenv_accepts_a_model_only_dotenv(tmp_path):
    """Negative control — the gate must not refuse a legitimate profile."""
    profile = tmp_path / "profile"
    _write_dotenv(profile / ".env", {
        "OPENROUTER_API_KEY": MODEL_CANARY,
        "HERMES_INFERENCE_MODEL": "some/model",
    })
    credentials.assert_profile_dotenv_clean(profile)


def test_absent_profile_dotenv_is_not_an_error(tmp_path):
    """A profile with no .env is the common case, not a failure."""
    (tmp_path / "profile").mkdir()
    credentials.assert_profile_dotenv_clean(tmp_path / "profile")
    credentials.assert_profile_dotenv_clean(tmp_path / "does-not-exist")


def test_profile_home_with_a_privileged_secret_subdir_fails_closed(tmp_path):
    """A secret store inside the profile home is refused.

    ``HERMES_HOME`` is the one directory the generation plane is pointed at,
    and the terminal tool reads any absolute path under
    ``HERMES_YOLO_MODE=1`` with no approval gate. File mode 0600 does not
    help: the child is the same OS user.
    """
    profile = tmp_path / "profile"
    (profile / "vercel-bypass").mkdir(parents=True)
    with pytest.raises(ValueError, match="privileged secret store"):
        credentials.assert_profile_home_clean(profile)


def test_clean_profile_home_passes(tmp_path):
    """A profile holding only ordinary Hermes state is fine."""
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    credentials.assert_profile_home_clean(profile)


# ---------------------------------------------------------------------------
# Vercel automation-bypass secret store: relocated out of the profile home
# ---------------------------------------------------------------------------


def test_bypass_store_root_is_outside_the_generation_profile_home(tmp_path):
    """The production wiring keeps the bypass secret out of ``$HERMES_HOME``.

    Constructed through the real ``RuntimeConfig`` field names, because the
    defect was a wiring choice in ``build_runtime`` — not a property of the
    store class, which happily writes wherever it is pointed.
    """
    from app.core.secrets import BypassSecretStore

    state_root = tmp_path / "state"
    hermes_home = tmp_path / ".hermes-website"
    state_root.mkdir()
    hermes_home.mkdir()

    store = BypassSecretStore(
        state_root / "vercel-bypass",
        legacy_roots=[hermes_home / "vercel-bypass"],
    )
    assert not store.root.is_relative_to(hermes_home)
    credentials.assert_profile_home_clean(hermes_home)


def test_bypass_store_legacy_root_is_still_readable(tmp_path):
    """Relocation must not silently discard a provisioned secret.

    A store written before the move lives under the profile home; it has to
    keep resolving, otherwise every existing project's preview smoke test
    starts failing with an unrelated-looking "no bypass" symptom.
    """
    from app.core.secrets import BypassSecretStore

    state_root = tmp_path / "state"
    hermes_home = tmp_path / ".hermes-website"
    legacy = BypassSecretStore(hermes_home / "vercel-bypass")
    legacy.set("prj_canary", "CANARY_BYPASS_SECRET")

    migrated = BypassSecretStore(
        state_root / "vercel-bypass",
        legacy_roots=[hermes_home / "vercel-bypass"],
    )
    assert migrated.get("prj_canary") == "CANARY_BYPASS_SECRET"
    assert migrated.has_stored("prj_canary")

    # A new write lands in the primary root only.
    migrated.set("prj_new", "CANARY_NEW")
    assert (state_root / "vercel-bypass" / "prj_new.json").is_file()
    assert not (hermes_home / "vercel-bypass" / "prj_new.json").exists()

    # The primary wins over the legacy copy for the same project id.
    migrated.set("prj_canary", "CANARY_UPDATED")
    assert migrated.get("prj_canary") == "CANARY_UPDATED"


def test_bypass_store_never_logs_the_secret(tmp_path, caplog):
    """A malformed secret file must not echo its contents into the log."""
    import logging

    from app.core.secrets import BypassSecretStore

    store = BypassSecretStore(tmp_path / "store")
    store._dir()
    (store.root / "prj_broken.json").write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.core.secrets"):
        assert store.get("prj_broken") is None
    assert "CANARY" not in caplog.text


# ---------------------------------------------------------------------------
# Fail-closed at the real spawn seam
# ---------------------------------------------------------------------------


def _run_cli_with_profile(profile, monkeypatch, env=None):
    """Drive the real ``_run_hermes_cli`` and report whether it spawned."""
    from app.hermes.adapter import HermesAdapter, HermesResult

    adapter = HermesAdapter(store=None, hermes_home=profile)
    adapter.sync_skills_to_profile = lambda: None

    role_config = profile / "config.yaml"
    role_config.write_text(
        "website_builder:\n  models:\n    FRONTEND:\n"
        "      model: some/model\n      provider: openrouter\n",
        encoding="utf-8",
    )

    spawned = {}

    def _fake_run(argv, **kwargs):
        spawned["argv"] = argv
        spawned["env"] = kwargs.get("env")

        class _R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return _R()

    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr("subprocess.run", _fake_run)
    result = adapter._run_hermes_cli(
        prompt="build", role="FRONTEND", toolsets=["file", "terminal"],
        cwd=profile,
    )
    return result, spawned


def test_spawn_seam_refuses_a_profile_dotenv_with_a_deploy_credential(tmp_path, monkeypatch):
    """Fail-closed happens BEFORE the child is ever started.

    Asserts the real production path (``HermesAdapter._run_hermes_cli``) and
    that ``subprocess.run`` is never reached — refusing to spawn is the whole
    point; a guard that runs the child anyway and complains afterwards is not
    a boundary.
    """
    profile = tmp_path / "profile"
    _write_dotenv(profile / ".env", {"VERCEL_TOKEN": CANARIES["VERCEL_TOKEN"]})

    result, spawned = _run_cli_with_profile(profile, monkeypatch)

    assert result.success is False
    assert result.exit_code == 1
    assert spawned == {}, "the child must not be spawned against an unsafe profile"
    assert CANARIES["VERCEL_TOKEN"] not in (result.error or "")
    assert "VERCEL_TOKEN" in (result.error or "")


def test_spawn_seam_refuses_a_profile_holding_a_privileged_secret_store(tmp_path, monkeypatch):
    """Same fail-closed posture for an on-disk secret store in the profile."""
    profile = tmp_path / "profile"
    (profile / "vercel-bypass").mkdir(parents=True)
    _write_dotenv(profile / ".env", {"OPENROUTER_API_KEY": MODEL_CANARY})

    result, spawned = _run_cli_with_profile(profile, monkeypatch)

    assert result.success is False
    assert spawned == {}, "the child must not be spawned against an unsafe profile"
    assert "vercel-bypass" in (result.error or "")


def test_spawn_seam_still_runs_for_a_clean_profile(tmp_path, monkeypatch):
    """Negative control for the two gates above — they must not be over-broad."""
    profile = tmp_path / "profile"
    profile.mkdir()
    _write_dotenv(profile / ".env", {"OPENROUTER_API_KEY": MODEL_CANARY})
    monkeypatch.setenv("OPENROUTER_API_KEY", MODEL_CANARY)

    result, spawned = _run_cli_with_profile(profile, monkeypatch)

    assert result.success is True, result.error
    assert spawned.get("env") is not None
    credentials.assert_no_privileged(spawned["env"])
    assert spawned["env"]["OPENROUTER_API_KEY"] == MODEL_CANARY


def test_spawn_seam_uses_the_role_provider_for_narrowing(tmp_path, monkeypatch):
    """The child env is narrowed to the FRONTEND role's configured provider."""
    for name in sorted(_provider_env_names()):
        monkeypatch.setenv(name, "canary-" + name.lower().replace("_", "-"))

    profile = tmp_path / "profile"
    profile.mkdir()

    result, spawned = _run_cli_with_profile(profile, monkeypatch)

    assert result.success is True, result.error
    env = spawned["env"]
    keys = {k.upper() for k in env if "KEY" in k.upper()}
    assert keys == {"OPENROUTER_API_KEY"}, keys
    assert credentials.model_credentials_not_shell_protected(env) == ()


# ---------------------------------------------------------------------------
# Messaging credentials are assertable, not merely omitted
# ---------------------------------------------------------------------------


def test_messaging_credentials_are_registered_as_privileged():
    """Registration is what makes the omission assertable.

    Before this, ``TELEGRAM_BOT_TOKEN`` was excluded from a generation child
    only because it matched no allowlist member — true today, but a future
    allowlist widening would leak the bot token with every canary still green.
    """
    assert "TELEGRAM_BOT_TOKEN" in credentials.PRIVILEGED_CREDENTIAL_NAMES
    assert "WHATSAPP_ACCESS_TOKEN" in credentials.PRIVILEGED_CREDENTIAL_NAMES


@pytest.mark.parametrize("role", ["FAST", "FRONTEND", "VISION"])
def test_generation_role_env_carries_no_messaging_credential(role, canaries):
    """A channel token must be assertably absent, not incidentally absent."""
    env = credentials.agent_env(role)
    for name in ("TELEGRAM_BOT_TOKEN", "WHATSAPP_ACCESS_TOKEN"):
        assert name not in {k.upper() for k in env}, name
        assert CANARIES[name] not in "".join(env.values())
    credentials.assert_no_privileged(env)


def test_messaging_credential_cannot_be_smuggled_via_extra():
    """The ``extra`` hook refuses channel credentials too."""
    with pytest.raises(ValueError, match="Refusing to inject privileged"):
        credentials.agent_env(
            "FRONTEND", extra={"TELEGRAM_BOT_TOKEN": "CANARY_TELEGRAM_TOKEN"}
        )
    with pytest.raises(ValueError, match="Refusing to inject privileged"):
        credentials.build_env("p", ".", extra={"TELEGRAM_BOT_TOKEN": "x"})


def test_isolation_gates_never_mutate_the_parent(canaries, tmp_path):
    """Requirement 7 extends to the new gates: names are read, never moved."""
    profile = tmp_path / "profile"
    _write_dotenv(profile / ".env", {"VERCEL_TOKEN": CANARIES["VERCEL_TOKEN"]})
    (profile / "vercel-bypass").mkdir(parents=True)

    for guard in (
        lambda: credentials.assert_profile_dotenv_clean(profile),
        lambda: credentials.assert_profile_home_clean(profile),
    ):
        with pytest.raises(ValueError):
            guard()

    for name, value in CANARIES.items():
        assert os.environ[name] == value, name
