"""M7: a static guard that the RISK_APPROVED write stays lease-fenced.

The portfolio lease fences the approval write so a gateway whose lease
expired mid-authorization cannot persist an approval (DBAE-146-02 runs its
freshness precondition in the same transaction). A runtime test only proves
the current call passes ``lease=``; this guard fails as soon as a change
drops it, or adds a second RISK_APPROVED write path without one.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "tradepulse" / "execution" / "gateway.py"


def _is_risk_approved(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "RISK_APPROVED"
            and isinstance(node.value, ast.Name) and node.value.id == "TradeIntentStatus")


def _builds_risk_approved(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and any(
        keyword.arg == "status" and _is_risk_approved(keyword.value) for keyword in node.keywords)


def _approved_names(tree: ast.AST) -> set[str]:
    """Variables assigned an intent built with ``status=RISK_APPROVED``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _builds_risk_approved(node.value):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _intent_writes(tree: ast.AST) -> list[ast.Call]:
    """``*.trade_intents.update(...)`` and ``*.trade_intents.create(...)`` calls."""
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"update", "create"}
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "trade_intents"]


def unfenced_approval_writes(source: str) -> tuple[int, list[int]]:
    """(approval writes found, line numbers of those without a real ``lease=``)."""
    tree = ast.parse(source)
    approved = _approved_names(tree)
    found, unfenced = 0, []
    for call in _intent_writes(tree):
        record = call.args[1] if call.func.attr == "update" and len(call.args) > 1 else (
            call.args[0] if call.func.attr == "create" and call.args else None)
        writes_approval = (record is not None and (
            (isinstance(record, ast.Name) and record.id in approved) or _builds_risk_approved(record)))
        if not writes_approval:
            continue
        found += 1
        lease = next((keyword.value for keyword in call.keywords if keyword.arg == "lease"), None)
        if lease is None or (isinstance(lease, ast.Constant) and lease.value is None):
            unfenced.append(call.lineno)
    return found, unfenced


def test_risk_approved_write_passes_lease() -> None:
    found, unfenced = unfenced_approval_writes(GATEWAY.read_text())
    assert found >= 1, "no RISK_APPROVED intent write found; the guard no longer sees the approval path"
    assert unfenced == [], f"RISK_APPROVED write without lease= at gateway.py lines {unfenced}"


def test_guard_detects_a_dropped_lease() -> None:
    source = GATEWAY.read_text()
    assert "lease=portfolio_lease, " in source
    found, unfenced = unfenced_approval_writes(source.replace("lease=portfolio_lease, ", "", 1))
    assert found >= 1 and len(unfenced) == 1


def test_guard_detects_a_none_lease() -> None:
    source = GATEWAY.read_text().replace("lease=portfolio_lease, ", "lease=None, ", 1)
    assert unfenced_approval_writes(source)[1]


def test_risk_approved_is_built_only_in_the_gateway() -> None:
    """A second module building a RISK_APPROVED intent would be an approval
    path this guard does not scan."""
    builders = [str(path.relative_to(REPO_ROOT)) for path in sorted((REPO_ROOT / "tradepulse").rglob("*.py"))
                if any(_builds_risk_approved(node) for node in ast.walk(ast.parse(path.read_text())))]
    assert builders == ["tradepulse/execution/gateway.py"]
