"""Mutation driver for the D3a.5 dependency-boundary repair.

Same discipline as the other D3a.5 drivers: revert ONE guard at a time on a
THROWAWAY COPY and prove the focused tests go red. The real working tree is never
edited.

This driver covers the NEW class of bug this repair closes -- a design registry
or resource introducing a DIRECT package dependency Hermes did not review:

  Part F -- the bounded npm spec parser
    * parser-echoes-raw          -> a hostile spec becomes a package identity
    * parser-accepts-tag         -> `latest`/`next` become installable
    * parser-accepts-empty-at    -> `gsap@` (npm's `latest`) slips through
    * constraint-always-true     -> an unsatisfiable range is accepted

  Part B -- the reviewed component contract
    * contract-ignored           -> upstream can add a package undetected
    * registry-deps-ignored      -> an unexpected nested component is installed
    * contract-missing-permits   -> a component with no contract is installable

  Part C -- the registry dependency boundary
    * delta-not-verified         -> the CLI's packages are trusted blindly
    * wrong-section-accepted     -> a runtime package lands in devDependencies
    * range-not-normalized       -> a floating range survives to package.json

  Part G -- 21st route false positives
    * routes-become-identities   -> /components/popular becomes a component

  Part H -- the Impeccable degraded scan
    * degraded-treated-as-clean  -> a regex-fallback scan certifies a design

Run:  python tools/mutation_check_d3a5_partbc.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

NPM_SPEC = "app/core/design_npm_spec.py"
REGISTRY = "app/core/design_registry.py"
INSTALL = "app/core/design_install.py"
FETCH = "app/core/design_catalog_fetch.py"
CRITIC = "app/core/design_critic.py"

NPM_SPEC_TESTS = "tests/test_design_npm_spec.py"
REGISTRY_TESTS = "tests/test_design_registry.py"
CONTRACT_TESTS = "tests/test_design_registry_contract.py"
MUTATION_TESTS = "tests/test_design_registry_mutation.py"
INSTALL_TESTS = "tests/test_design_install.py"
PIN_TESTS = "tests/test_design_dependency_pins.py"
FETCH_TESTS = "tests/test_design_catalog_fetch.py"
CRITIC_TESTS = "tests/test_design_critic.py"

MUTATIONS = [
    # ------------------------------------------------------------------
    # Part F -- the bounded npm spec parser
    # ------------------------------------------------------------------
    # A parser that echoes its input would make an arbitrary string a package
    # identity -- the exact class this batch forbids.
    (
        "the spec parser never echoes an unparsed spec",
        NPM_SPEC,
        """    name, constraint, had_separator = _split_name_and_constraint(text)
    if not name or not _PACKAGE_NAME_RE.match(name):
        return None
    if had_separator and not constraint:
        # `gsap@` is npm's spelling for the floating `latest` tag.
        return None
    if constraint and not _CONSTRAINT_RE.match(constraint):
        return None
    return NpmPackageSpec(package_name=name, declared_constraint=constraint)""",
        """    name, constraint, had_separator = _split_name_and_constraint(text)
    return NpmPackageSpec(package_name=text, declared_constraint="")""",
    ),
    # A dist-tag like `latest` must not become an installable version.
    (
        "a dist-tag is not a valid constraint",
        NPM_SPEC,
        """    if constraint and not _CONSTRAINT_RE.match(constraint):
        return None""",
        """    if False:
        return None""",
    ),
    # `gsap@` is npm's spelling for `latest`; it must fail closed.
    (
        "an empty constraint after an explicit @ is refused",
        NPM_SPEC,
        """    if had_separator and not constraint:
        # `gsap@` is npm's spelling for the floating `latest` tag.
        return None""",
        """    if False:
        return None""",
    ),
    # A constraint the pin cannot satisfy must not be accepted.
    (
        "the constraint check can fail",
        NPM_SPEC,
        """    if not isinstance(constraint, str) or not constraint.strip():
        return False
    if not _CONSTRAINT_RE.match(constraint.strip()):
        return False""",
        """    return True
    if not isinstance(constraint, str) or not constraint.strip():
        return False
    if not _CONSTRAINT_RE.match(constraint.strip()):
        return False""",
    ),
    # ------------------------------------------------------------------
    # Part B -- the reviewed component contract
    # ------------------------------------------------------------------
    # Without the contract-set check, upstream can ADD a package and it installs.
    (
        "the declared dependency set must match the reviewed contract",
        REGISTRY,
        """    if set(dependency_ids) != set(contract.expected_dependency_ids):
        logger.warning(
            "Refusing a registry component whose declared dependencies do not "
            "match its reviewed contract."
        )
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_DEPENDENCY_CONTRACT_MISMATCH
        )""",
        """    if False:
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_DEPENDENCY_CONTRACT_MISMATCH
        )""",
    ),
    # An unexpected nested registry dependency must fail closed.
    (
        "unexpected nested registry dependencies are refused",
        REGISTRY,
        """    if set(declared_registry) != set(contract.expected_registry_dependencies):
        logger.warning(
            "Refusing a registry component declaring nested registry "
            "dependencies outside its reviewed contract."
        )
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_REGISTRY_DEPENDENCY_MISMATCH
        )""",
        """    if False:
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_REGISTRY_DEPENDENCY_MISMATCH
        )""",
    ),
    # A component with no contract must NOT be installable.
    (
        "a component with no reviewed contract is not installable",
        REGISTRY,
        """    contract = reviewed_component_contract(source, component_id)
    if contract is None:
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_CONTRACT_MISSING
        )""",
        """    contract = reviewed_component_contract(source, component_id)
    if contract is None:
        return RegistryRequestOutcome(
            ok=True,
            request=RegistryInstallRequest(
                source=source,
                component_id=component_id,
                registry_locator_id=locator,
                required_dependency_ids=dependency_ids,
            ),
            reason=REASON_REQUEST_OK,
            dependency_ids=dependency_ids,
        )""",
    ),
    # ------------------------------------------------------------------
    # Part C -- the registry dependency boundary
    # ------------------------------------------------------------------
    # Trusting the CLI's packages blindly is the whole defect.
    (
        "the registry direct-dependency delta is verified",
        INSTALL,
        """        after = snapshot_direct_dependencies(self.project_root)
        acceptable, offending = registry_dependency_delta_is_acceptable(
            before, after, allowed_packages=allowed_packages
        )
        if not acceptable:""",
        """        after = snapshot_direct_dependencies(self.project_root)
        acceptable, offending = True, ()
        if not acceptable:""",
    ),
    # A reviewed package in the wrong SECTION must be refused.
    (
        "a registry package in a non-runtime section is refused",
        INSTALL,
        """            if name not in allowed:
                offending.add(name)
            elif section != SECTION_DEPENDENCIES:
                # Right package, wrong section: a registry must not move a
                # runtime helper into devDependencies/optionalDependencies.
                offending.add(name)""",
        """            if name not in allowed:
                offending.add(name)""",
    ),
    # A floating range must be normalized to the exact application pin.
    (
        "a registry-introduced range is normalized to an exact pin",
        INSTALL,
        """        already_exact = all(
            after.get(SECTION_DEPENDENCIES, {}).get(package) == pins[package]
            for package in introduced
        )
        if already_exact:
            return _RegistryBoundaryResult(ok=True)""",
        """        already_exact = True
        if already_exact:
            return _RegistryBoundaryResult(ok=True)""",
    ),
    # ------------------------------------------------------------------
    # Part G -- 21st route false positives
    # ------------------------------------------------------------------
    # Reintroducing the route regex fabricates component identities.
    (
        "21st route paths never become component identities",
        FETCH,
        """    for match in _21ST_AUTHORED_COMPONENT_RE.finditer(text):
        identity = match.group(1).strip()""",
        """    import re as _re

    for match in _re.finditer(r"/components/(?:s/)?([a-z0-9]+(?:-[a-z0-9]+)*)\\b", text):
        identity = match.group(1).strip()""",
    ),
    # ------------------------------------------------------------------
    # The failure vocabulary matches the DOCUMENTED contract (401 / 429)
    # ------------------------------------------------------------------
    (
        "a 401 is named as a rejected credential",
        FETCH,
        """    if status == 401:
        return REASON_AUTH_REJECTED""",
        """    if False:
        return REASON_AUTH_REJECTED""",
    ),
    (
        "a 429 is named as rate limiting",
        FETCH,
        """    if status == 429:
        return REASON_RATE_LIMITED""",
        """    if False:
        return REASON_RATE_LIMITED""",
    ),
    # ------------------------------------------------------------------
    # A success flag is bound to its payload at CONSTRUCTION
    # ------------------------------------------------------------------
    # ok means "an installable request was produced" -> it must carry a request.
    (
        "an ok registry request outcome must carry a request",
        REGISTRY,
        """        if self.ok and self.request is None:
            raise ValueError("an ok registry request outcome must carry a request")""",
        """        if False:
            raise ValueError("an ok registry request outcome must carry a request")""",
    ),
    # `installed` must mean an observation happened.
    (
        "an installed outcome must carry verification",
        INSTALL,
        """        if self.state == "installed" and not (
            self.verified_in_manifest or self.verified_components
        ):
            raise ValueError(""",
        """        if False:
            raise ValueError(""",
    ),
    # a successful fetch must carry a payload.
    (
        "a successful fetch must carry a payload",
        FETCH,
        """        if self.reason is None and self.payload is None:
            raise ValueError("a successful fetch must carry a payload")""",
        """        if False:
            raise ValueError("a successful fetch must carry a payload")""",
    ),
    # ------------------------------------------------------------------
    # Reserved route segments are refused at the REGISTRY (installability)
    # ------------------------------------------------------------------
    # A reserved route segment can never have a reviewed contract, so a
    # mistaken table edit cannot make one installable.
    (
        "a reserved route segment cannot have a reviewed contract",
        REGISTRY,
        """        if component_id_is_reserved(self.source, self.component_id):
            raise ValueError(""",
        """        if False:
            raise ValueError(""",
    ),
    # ------------------------------------------------------------------
    # The 21st identity pattern keeps the /@<author>/ ANCHOR. Dropping it for
    # the generic /components/<slug> tail turns routes into identities.
    (
        "the 21st identity pattern keeps the author anchor",
        FETCH,
        """_21ST_AUTHORED_COMPONENT_RE = re.compile(
    r"/@[A-Za-z0-9_.\-]+/components/([a-z0-9]+(?:-[a-z0-9]+)*)"
)""",
        """_21ST_AUTHORED_COMPONENT_RE = re.compile(
    r"/components/([a-z0-9]+(?:-[a-z0-9]+)*)"
)""",
    ),
    # ------------------------------------------------------------------
    # Part G -- 21st discovery is the REAL authenticated REST search
    # ------------------------------------------------------------------
    # No credential -> no request at all. A 401 round-trip is not a useful
    # degradation, and the false-positive route parser must never come back.
    (
        "21st discovery makes no request without a credential",
        FETCH,
        """    if source in CREDENTIAL_REQUIRED_FOR_DISCOVERY and credential is None:
        # No key -> no request. This is the designed bounded state, not an error.
        return CatalogFetchResult(source=source, reason=REASON_CREDENTIAL_REQUIRED)""",
        """    if False:
        # No key -> no request. This is the designed bounded state, not an error.
        return CatalogFetchResult(source=source, reason=REASON_CREDENTIAL_REQUIRED)""",
    ),
    # The 21st URL is the REST SEARCH endpoint, not the identity-free llms.txt.
    (
        "21st discovery uses the REST search endpoint",
        FETCH,
        """    elif source in CATALOG_SEARCH_ENDPOINTS:
        term = query if isinstance(query, str) and query.strip() else DEFAULT_DISCOVERY_QUERY
        term = term.strip()[:MAX_QUERY_CHARS]
        encoded = urllib.parse.quote(term, safe="")
        url = (
            f"{CATALOG_SEARCH_ENDPOINTS[source]}"
            f"?q={encoded}&scope={DISCOVERY_SCOPE}&limit={DISCOVERY_LIMIT}"
        )""",
        """    elif source in CATALOG_SEARCH_ENDPOINTS:
        url = CATALOG_SEARCH_ENDPOINTS[source]""",
    ),
    # The search term is percent-encoded, so it cannot widen the request.
    (
        "the discovery query term is percent-encoded",
        FETCH,
        """        encoded = urllib.parse.quote(term, safe="")""",
        """        encoded = term""",
    ),
    # ------------------------------------------------------------------
    # Part H -- the Impeccable degraded scan
    # ------------------------------------------------------------------
    # A degraded (regex-fallback) scan must not be treated as authoritative.
    (
        "a degraded engine scan is not authoritative",
        CRITIC,
        """    stderr = getattr(completed, "stderr", "") or ""
    if DEGRADED_MARKER in stderr:""",
        """    stderr = getattr(completed, "stderr", "") or ""
    if False:""",
    ),
    # ------------------------------------------------------------------
    # Part B (layer 2) -- the request CONSTRUCTOR binds to the contract
    # ------------------------------------------------------------------
    # A directly-constructed request must not widen the dependency set beyond
    # the reviewed contract.
    (
        "a directly-constructed request is bound to the reviewed contract",
        REGISTRY,
        """            if set(self.required_dependency_ids) != set(
                contract.expected_dependency_ids
            ):
                raise ValueError(
                    "required dependency ids do not match the component's reviewed "
                    "contract"
                )""",
        """            if False:
                raise ValueError(
                    "required dependency ids do not match the component's reviewed "
                    "contract"
                )""",
    ),
    # ------------------------------------------------------------------
    # Part C (layer 2) -- the accepted set is derived from the CONTRACT
    # ------------------------------------------------------------------
    # The installer must not trust a field on the passed object; it must resolve
    # the contract itself, so a duck-typed stand-in cannot widen the boundary.
    (
        "the installer derives the accepted set from the contract",
        INSTALL,
        """        contract = reviewed_component_contract(source, component_id)
        if contract is None:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_COMPONENT_NOT_ALLOWED,
            )
        allowed_packages = tuple(
            sorted(
                DEPENDENCY_PACKAGES[dependency_id]
                for dependency_id in contract.expected_dependency_ids
                if dependency_id in DEPENDENCY_PACKAGES
            )
        )""",
        """        contract = reviewed_component_contract(source, component_id)
        if contract is None:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_COMPONENT_NOT_ALLOWED,
            )
        allowed_packages = tuple(
            sorted(
                DEPENDENCY_PACKAGES[dependency_id]
                for dependency_id in getattr(request, "required_dependency_ids", ())
                if dependency_id in DEPENDENCY_PACKAGES
            )
        )""",
    ),
    # ------------------------------------------------------------------
    # Part D (built-in shadcn) -- the emitted SOURCE import closure
    # ------------------------------------------------------------------
    # Live: `add dialog` emits a file importing `lucide-react` (never installed)
    # and `@/components/ui/button` (never created). A guard that only inspects
    # package.json would report `installed` for a component that cannot build.
    #
    # 1. The required-import postcondition must actually run.
    (
        "a builtin's emitted-source import is installed and verified",
        INSTALL,
        """        imports_ok, imports_receipt = self._ensure_required_registry_imports(
            effective, manager
        )
        if not imports_ok:""",
        """        imports_ok, imports_receipt = True, None
        if not imports_ok:""",
    ),
    # 2. A required import with no reviewed exact pin must fail closed.
    (
        "a required import with no reviewed pin fails closed",
        INSTALL,
        """        pins = reviewed_registry_package_pins()
        if any(package not in pins for package in required):
            logger.error(
                "A component's emitted source requires a package with no "
                "application-owned exact pin."
            )
            return (False, None)""",
        """        pins = reviewed_registry_package_pins()
        if False:
            logger.error(
                "A component's emitted source requires a package with no "
                "application-owned exact pin."
            )
            return (False, None)""",
    ),
    # 3. The nested closure must be expanded, not just the requested component.
    (
        "the reviewed nested closure is materialized",
        INSTALL,
        """        effective = expand_reviewed_component_closure(allowed)""",
        """        effective = tuple(allowed)""",
    ),
    # 4. The nested closure must come from the app table, not be empty forever.
    (
        "a builtin that imports a sibling declares it in the closure table",
        INSTALL,
        """REVIEWED_BUILTIN_COMPONENT_NESTED: Dict[str, Tuple[str, ...]] = {
    "dialog": ("button",),
}""",
        """REVIEWED_BUILTIN_COMPONENT_NESTED: Dict[str, Tuple[str, ...]] = {}""",
    ),
    # 5. lucide-react must be a reviewed exact pin, not an unpinned package.
    (
        "the icon package the emitted sources import is exact-pinned",
        INSTALL,
        """    "lucide-react": "1.52.0",""",
        """    "lucide-react": "^1.52.0",""",
    ),
    # ------------------------------------------------------------------
    # Emitted-SOURCE import boundary -- the INSTALLATION is untrusted
    # ------------------------------------------------------------------
    # A registry may materialize a file importing a package it never declared,
    # so the manifest delta is clean. The emitted source itself must be checked.
    #
    # 6. The source-import boundary must actually run in install_components.
    (
        "the emitted-source import boundary runs after materialization",
        INSTALL,
        """        source_ok = self._enforce_registry_source_import_boundary(
            effective, component_dir
        )
        if not source_ok:""",
        """        source_ok = True
        if not source_ok:""",
    ),
    # 7. The bare-package parser must not treat the project alias as a package.
    (
        "the project alias is never treated as a package import",
        INSTALL,
        """    if specifier.startswith(("./", "../", "/", "@/")) or specifier in (".", ".."):
        return None""",
        """    if specifier.startswith(("./", "../", "/")) or specifier in (".", ".."):
        return None""",
    ),
    # 8. The unreviewed check must actually filter against the allow-set.
    (
        "only reviewed packages are accepted from an emitted source",
        INSTALL,
        """    allowed = set(allowed_packages)
    return tuple(p for p in declared_imports(source) if p not in allowed)""",
        """    allowed = set(allowed_packages)
    return ()""",
    ),
    # 9. The external path must apply the source-import boundary too.
    (
        "the external component source import boundary runs",
        INSTALL,
        """        if not self._enforce_registry_source_import_boundary(
            (request.component_id,), component_dir, extra_packages=allowed_packages
        ):""",
        """        if False:""",
    ),
    # ------------------------------------------------------------------
    # Removal mutation -- an install must never DELETE a project dependency
    # ------------------------------------------------------------------
    # `dependency_delta` only reports what `after` CONTAINS, so a removal is
    # invisible to it. A registry install adds reviewed packages; it never
    # removes a pre-existing project dependency.
    #
    # 10. The delta guard must include removals.
    (
        "a removed pre-existing dependency is refused",
        INSTALL,
        """    # A removal is invisible to ``dependency_delta``; catch it here so a silent
    # deletion of a project dependency fails the install.
    for packages in removed_direct_dependencies(before, after).values():
        offending.update(packages)""",
        """    pass""",
    ),
    # 11. The whole-operation removal check must actually run (builtin path).
    (
        "the whole-operation removal check runs after normalization",
        INSTALL,
        """        if self._removal_after_install(before):
            logger.warning(
                "Refusing a registry install that removed a pre-existing "
                "project dependency."
            )""",
        """        if False:
            logger.warning(
                "Refusing a registry install that removed a pre-existing "
                "project dependency."
            )""",
    ),
    # 12r. The import boundary must see require() and dynamic import().
    (
        "require() and dynamic import() are seen by the import boundary",
        INSTALL,
        """    for pattern in (
        _IMPORT_FROM_RE,
        _SIDE_EFFECT_IMPORT_RE,
        _REQUIRE_RE,
        _DYNAMIC_IMPORT_RE,
    ):""",
        """    for pattern in (
        _IMPORT_FROM_RE,
        _SIDE_EFFECT_IMPORT_RE,
    ):""",
    ),
    # 12q. A file written outside the component dir must be refused.
    (
        "a file outside the component dir is refused",
        INSTALL,
        """        if unreviewed_project_files(
            project_files_before,
            snapshot_project_files(self.project_root),
            allowed_components=effective,
            component_dir=component_dir,
            project_root=self.project_root,
        ):
            logger.warning(
                "Refusing a registry install that wrote a file outside the "
                "reviewed artifact set."
            )""",
        """        if False:
            logger.warning(
                "Refusing a registry install that wrote a file outside the "
                "reviewed artifact set."
            )""",
    ),
    # 12p. An unexpected package.json section mutation must be refused.
    (
        "an unexpected package.json section mutation is refused",
        INSTALL,
        """        if changed_manifest_sections(
            sections_before, snapshot_manifest_sections(self.project_root)
        ):
            logger.warning(
                "Refusing a registry install that changed a package.json section "
                "outside the reviewed dependency surface."
            )""",
        """        if False:
            logger.warning(
                "Refusing a registry install that changed a package.json section "
                "outside the reviewed dependency surface."
            )""",
    ),
    # 12o. A git/file/http npm spec must never become a package identity.
    (
        "a git/file/http npm spec is refused",
        NPM_SPEC,
        """    if not name or not _PACKAGE_NAME_RE.match(name):
        return None""",
        """    if not name:
        return None""",
    ),
    # 12n. A package-source config (e.g. a root .npmrc registry= line) must not
    #      be written or changed by an install.
    (
        "a package-source config change is refused",
        INSTALL,
        """        if changed_package_source_configs(
            source_config_before, package_source_config_snapshot(self.project_root)
        ):
            logger.warning(
                "Refusing a registry install that wrote or changed a "
                "package-source config file."
            )""",
        """        if False:
            logger.warning(
                "Refusing a registry install that wrote or changed a "
                "package-source config file."
            )""",
    ),
    # 12m. The locator must be re-resolved to the canonical one before argv, so
    #      a duck-typed request cannot smuggle an arbitrary URL into the CLI.
    (
        "an arbitrary registry URL never reaches argv",
        INSTALL,
        """        canonical_locator = resolve_registry_locator(source, component_id)
        if canonical_locator is None or locator != canonical_locator:""",
        """        canonical_locator = resolve_registry_locator(source, component_id)
        if False:""",
    ),
    # 12l. The FILE delta must be checked: an extra materialized file is refused.
    (
        "an extra materialized file is refused",
        INSTALL,
        """        files_after = snapshot_component_files(component_dir)
        unexpected_files = unreviewed_materialized_files(
            files_before, files_after, allowed_components=effective
        )
        if unexpected_files:""",
        """        files_after = snapshot_component_files(component_dir)
        unexpected_files = unreviewed_materialized_files(
            files_before, files_after, allowed_components=effective
        )
        if False:""",
    ),
    # 12k. The component tables stay SEPARATE: a builtin name is never an
    #      approved EXTERNAL identity (the contract-keyed-to-builtin-source case
    #      is subsumed by the "contract without an approved identity" check).
    (
        "a builtin name is never an approved external identity",
        REGISTRY,
        """        for source, components in self.approved_components.items():
            if source == SOURCE_SHADCN_BUILTIN:
                if components:
                    raise ValueError(
                        "the builtin source must have no approved external identities"
                    )
                continue
            overlap = builtin_names & set(components)
            if overlap:
                raise ValueError(
                    f"a builtin name is an approved external identity: {sorted(overlap)!r}"
                )""",
        """        for source, components in self.approved_components.items():
            if source == SOURCE_SHADCN_BUILTIN:
                if components:
                    raise ValueError(
                        "the builtin source must have no approved external identities"
                    )
                continue
            overlap = set()
            if overlap:
                raise ValueError(
                    f"a builtin name is an approved external identity: {sorted(overlap)!r}"
                )""",
    ),
    # 12j. A builtin carrying declared dependencies must be a bounded REFUSAL,
    #      not an uncaught ValueError (which would let a builtin be routed
    #      through the external-contract path).
    (
        "a builtin carrying declared dependencies is refused, not crashed",
        REGISTRY,
        """        if dependency_ids:
            logger.warning(
                "Refusing a shadcn builtin carrying declared dependencies; builtin "
                "dependencies are reviewed per component, not declared."
            )
            return RegistryRequestOutcome(
                ok=False, request=None, reason=REASON_BUILTIN_CARRIES_DEPENDENCIES
            )""",
        """        if False:
            logger.warning(
                "Refusing a shadcn builtin carrying declared dependencies; builtin "
                "dependencies are reviewed per component, not declared."
            )
            return RegistryRequestOutcome(
                ok=False, request=None, reason=REASON_BUILTIN_CARRIES_DEPENDENCIES
            )""",
    ),
    # 12h. The trusted boundary must be CONSULTED by build_registry_request.
    (
        "build_registry_request consults the trusted boundary",
        REGISTRY,
        """    try:
        trusted_registry_boundary()
    except ValueError:
        logger.error(
            "The trusted registry boundary is incoherent; refusing to build a "
            "registry request."
        )
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_BOUNDARY_INCOHERENT
        )""",
        """    try:
        pass
    except ValueError:
        logger.error(
            "The trusted registry boundary is incoherent; refusing to build a "
            "registry request."
        )
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_BOUNDARY_INCOHERENT
        )""",
    ),
    # 12f. The trusted boundary TYPE must validate on construction (D3a idiom).
    (
        "the trusted boundary type validates on construction",
        REGISTRY,
        """        if not introduced <= set(self.introduced_pins):
            raise ValueError(
                "a package the builtins introduce has no exact application-owned pin"
            )""",
        """        if False:
            raise ValueError(
                "a package the builtins introduce has no exact application-owned pin"
            )""",
    ),
    # 12i. install_components must consult the trusted boundary (builtin path).
    (
        "install_components consults the trusted boundary",
        INSTALL,
        """        try:
            trusted_registry_boundary()
        except ValueError:
            logger.error(
                "The trusted registry boundary is incoherent; refusing the "
                "builtin registry install."
            )""",
        """        try:
            pass
        except ValueError:
            logger.error(
                "The trusted registry boundary is incoherent; refusing the "
                "builtin registry install."
            )""",
    ),
    # 12g. A non-builtin source must have a host.
    (
        "a non-builtin registry source must have a host",
        REGISTRY,
        """        for source in self.sources:
            if source != SOURCE_SHADCN_BUILTIN and source not in self.hosts:
                raise ValueError(f"a non-builtin source has no host: {source!r}")""",
        """        for source in ():
            if source != SOURCE_SHADCN_BUILTIN and source not in self.hosts:
                raise ValueError(f"a non-builtin source has no host: {source!r}")""",
    ),
    # 12e. The trusted tables are the only source of truth: an untrusted host is
    #      never added to the closed host table.
    (
        "the trusted host table is closed",
        REGISTRY,
        """REGISTRY_HOSTS: Dict[str, str] = {
    SOURCE_TWENTY_FIRST: "21st.dev",
    SOURCE_REACT_BITS: "reactbits.dev",
}""",
        """REGISTRY_HOSTS: Dict[str, str] = {
    SOURCE_TWENTY_FIRST: "21st.dev",
    SOURCE_REACT_BITS: "reactbits.dev",
    "evil": "evil.example",
}""",
    ),
    # 12d. The builtin registry packages are a SEPARATE set from the D2-selectable
    #      dependencies. A registry helper must not be smuggled into the D2 set
    #      (which would make it selectable).
    (
        "the builtin registry packages stay separate from the D2 dependencies",
        INSTALL,
        """DEPENDENCY_PACKAGES: Dict[str, str] = {
    "gsap": "gsap",""",
        """DEPENDENCY_PACKAGES: Dict[str, str] = {
    "cn": "cn",
    "gsap": "gsap",""",
    ),
    # 12c. The reviewed contract's packages must be PRESENT, not just not-exceeded.
    (
        "a missing reviewed contract package is refused",
        INSTALL,
        """        if not self._contract_packages_present(allowed_packages):""",
        """        if False:""",
    ),
    # 12b. A builtin request must not carry dependency ids at all.
    (
        "a builtin request cannot carry dependency ids",
        REGISTRY,
        """            if self.required_dependency_ids and all(
                dependency_id in DEPENDENCY_PACKAGES
                for dependency_id in self.required_dependency_ids
            ):
                raise ValueError(
                    "a shadcn builtin request must not carry required dependency ids"
                )""",
        """            if False:
                raise ValueError(
                    "a shadcn builtin request must not carry required dependency ids"
                )""",
    ),
    # 12. The pinned CLI's DIRECT-package allowlist must be the reviewed set,
    #     not silently widened by an extra entry.
    (
        "the registry-introduced package set is exactly the reviewed set",
        INSTALL,
        """REGISTRY_INTRODUCED_PACKAGE_PINS: Dict[str, str] = {
    "cn": "0.4.0",
    "radix-ui": "1.7.0",""",
        """REGISTRY_INTRODUCED_PACKAGE_PINS: Dict[str, str] = {
    "cn": "0.4.0",
    "radix-ui": "1.7.0",
    "unreviewed-extra-pkg": "9.9.9",""",
    ),
]


def tests_for(relative: str) -> Tuple[str, ...]:
    if relative == NPM_SPEC:
        return (NPM_SPEC_TESTS,)
    if relative == REGISTRY:
        return (CONTRACT_TESTS, REGISTRY_TESTS, MUTATION_TESTS)
    if relative == INSTALL:
        return (MUTATION_TESTS, INSTALL_TESTS, PIN_TESTS, CONTRACT_TESTS)
    if relative == FETCH:
        return (FETCH_TESTS,)
    if relative == CRITIC:
        return (CRITIC_TESTS,)
    return (NPM_SPEC_TESTS,)


def run_tests(cwd, test_files):
    if isinstance(test_files, str):
        test_files = (test_files,)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *test_files,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else result.stderr.strip()[-140:]


def main():
    for label, path in (
        ("Part F", NPM_SPEC_TESTS),
        ("Part B", CONTRACT_TESTS),
        ("Part C", MUTATION_TESTS),
        ("Parts E/K", PIN_TESTS),
        ("Part G", FETCH_TESTS),
        ("Part H", CRITIC_TESTS),
    ):
        code, tail = run_tests(ROOT, path)
        print(f"{label} baseline: exit={code} {tail}")
        if code != 0:
            return 1

    print()
    unproven = []
    for label, relative, present, replacement in MUTATIONS:
        target = ROOT / relative
        original = target.read_text(encoding="utf-8")

        if present not in original:
            print(f"[ANCHOR-MISS] {label}")
            unproven.append(f"{label}: anchor not found in {relative}")
            continue

        mutated = original.replace(present, replacement, 1)
        if mutated == original:
            print(f"[ANCHOR-MISS] {label} (replacement was a no-op)")
            unproven.append(f"{label}: mutation changed nothing")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            # The copy MUST be named `website-builder`: test_design_install.py
            # resolves the shipped manifest as `parents[2]/website-builder/config`,
            # so a differently-named tree makes those tests error at setup
            # (environmental), which would masquerade as a mutation kill.
            work = Path(tmp) / "website-builder"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            destination = work / relative
            if not destination.resolve().is_relative_to(work.resolve()):
                print(f"[ABORT]      {label} -- target escapes the temp tree")
                return 1
            destination.write_text(mutated, encoding="utf-8")

            code, tail = run_tests(work, tests_for(relative))
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "no tests ran" in tail or " errors in " in tail:
                print(f"[INVALID]    {label} -- {tail}")
                unproven.append(f"{label}: mutation invalidated collection ({tail})")
            else:
                print(f"[KILLED]     {label} -- {tail}")

    print()
    if unproven:
        print(f"{len(unproven)} guard(s) unproven:")
        for item in unproven:
            print("  -", item)
        return 1

    print(f"all {len(MUTATIONS)} guards killed by the focused tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
