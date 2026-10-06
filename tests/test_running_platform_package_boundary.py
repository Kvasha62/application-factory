"""The Running Platform layer consumes the identity contract, never its verifier.

ADR-0020 §4 assigns the Platform Identity Surface semantically to the Running
Platform, and §5 splits the work between the two layers: the Running Platform
*provides* actual identity-bearing facts, Deployment & Operations *verifies*
them and evaluates correspondence. The type vocabulary both sides need is
shipped inside the D&O package as a deliberate shared contract
(``docs/DOCUMENTATION_BASELINE.md`` §7), so what the runtime layer may take from
there is exactly that vocabulary — and nothing that would make it a second
evaluator of evidence.

Three rules are checked, and nothing else:

* ``running_platform`` may import only the two approved contract modules
  (``deployment_operations.platform_identity`` and
  ``deployment_operations.platform_identity_source``);
* from those modules it may take data types only — never a verification or
  projection function and never the ``PlatformIdentityProvider`` seam, because
  judging evidence is D&O's responsibility;
* ``deployment_operations.runtime_worker`` is never imported: that module is the
  member runtime process of the shipped default adapter, while supervision and
  lifetime of a Running Platform belong to its own owner.

The scan reads syntax instead of importing, so it also sees an import hidden
inside a function or behind ``importlib``.
"""

from __future__ import annotations

import ast
import pathlib

import running_platform

PACKAGE_ROOT = pathlib.Path(running_platform.__file__).resolve().parent
SRC_ROOT = PACKAGE_ROOT.parent

#: The shared identity contract — the only thing this layer may take from D&O.
APPROVED_CONTRACT_MODULES = frozenset(
    {
        "deployment_operations.platform_identity",
        "deployment_operations.platform_identity_source",
    }
)

#: Evaluating identity evidence is Deployment & Operations' job (ADR-0020 §5).
FORBIDDEN_NAMES = frozenset(
    {
        "PlatformIdentityProvider",
        "compute_actual_digest",
        "establish_identity_correspondence",
        "project_actual_identity",
        "validate_actual_evidence",
        "validate_actual_surface",
    }
)

#: The member runtime process of the shipped default adapter — never ours.
RUNTIME_WORKER = "deployment_operations.runtime_worker"

_PACKAGE = "deployment_operations"


def _is_package_module(name: str) -> bool:
    return name == _PACKAGE or name.startswith(f"{_PACKAGE}.")


def _references(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """Every ``deployment_operations`` module and name one module reaches for."""
    modules: set[str] = set()
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.name for alias in node.names if _is_package_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_package_module(node.module):
                modules.add(node.module)
                names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            # An import hidden behind importlib is still an import.
            called = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if called in {"import_module", "__import__"}:
                for argument in node.args:
                    value = getattr(argument, "value", None)
                    if isinstance(value, str) and _is_package_module(value):
                        modules.add(value)
    return modules, names


def _layer_modules() -> list[pathlib.Path]:
    return sorted(PACKAGE_ROOT.glob("*.py"))


def test_the_layer_reaches_only_for_the_shared_identity_contract() -> None:
    referenced: set[str] = set()
    for path in _layer_modules():
        modules, _names = _references(path)
        for module in modules:
            assert module in APPROVED_CONTRACT_MODULES, (
                f"{path.name} reaches for {module}; this layer may take only the "
                "identity contract vocabulary from Deployment & Operations"
            )
        referenced |= modules
    assert referenced, "the scan saw no shared-contract import, so it proves nothing"


def test_the_layer_takes_data_types_and_never_a_verifier() -> None:
    for path in _layer_modules():
        _modules, names = _references(path)
        taken = sorted(names & FORBIDDEN_NAMES)
        assert not taken, (
            f"{path.name} imports {taken} from Deployment & Operations; judging "
            "identity evidence is D&O's responsibility (ADR-0020 §5)"
        )


def test_the_layer_never_imports_the_member_runtime_process() -> None:
    for path in _layer_modules():
        modules, _names = _references(path)
        assert RUNTIME_WORKER not in modules, (
            f"{path.name} imports {RUNTIME_WORKER}; supervision and lifetime of a "
            "Running Platform belong to its own owner, not to a D&O module"
        )


def test_the_scan_would_notice_a_forbidden_import() -> None:
    """Control: the scanner finds exactly what the rules above forbid.

    Both modules scanned here belong to Deployment & Operations, where these
    imports are correct — which is what makes them the honest place to prove the
    scan is not vacuous.
    """
    _modules, verifier_names = _references(
        SRC_ROOT / "deployment_operations" / "deployment.py"
    )
    assert verifier_names & FORBIDDEN_NAMES, "the scan no longer sees a verifier import"

    adapter_modules, _names = _references(
        SRC_ROOT / "deployment_operations" / "runtime.py"
    )
    assert (
        RUNTIME_WORKER in adapter_modules
    ), "the scan no longer sees the runtime worker"
