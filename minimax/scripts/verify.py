"""End-to-end verification for the SpecLens SDD context demo."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CLI = ROOT / "context_module" / "cli.py"
PROJECT = ROOT / "demo_project"
SNAPSHOTS = ROOT / "snapshots"
SPEC = PROJECT / "specs" / "books_api.md"
SPEC_V1 = PROJECT / "specs" / "books_api.v1.md"
SPEC_V2 = PROJECT / "specs" / "books_api.v2.md"
TARGET_TESTS = {
    "test_filter_by_price_range",
    "test_price_boundaries_are_inclusive",
    "test_invalid_price_range_returns_422",
    "test_negative_price_returns_422",
}


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def baseline_copy() -> Path:
    temp = Path(tempfile.mkdtemp(prefix="sdd-verify-baseline-"))
    target = temp / "demo_project"
    shutil.copytree(PROJECT, target, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"))
    for source in (SNAPSHOTS / "before").rglob("*"):
        if source.is_file():
            destination = target / source.relative_to(SNAPSHOTS / "before")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def main() -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / "evidence" / f"verify-{stamp}"
    evidence.mkdir(parents=True)
    checks: dict[str, bool] = {}

    baseline = baseline_copy()
    before = run([PYTHON, "-m", "pytest", "-q"], baseline)
    write(evidence / "baseline-tests.log", before.stdout + before.stderr)
    failed_lines = [line for line in before.stdout.splitlines() if line.startswith("FAILED")]
    failed_names = {line.split("::")[-1].split(" ")[0].strip() for line in failed_lines}
    checks["baseline_target_tests_fail"] = before.returncode != 0 and TARGET_TESTS.issubset(failed_names)
    checks["baseline_regressions_pass"] = not (failed_names - TARGET_TESTS)

    after = run([PYTHON, "-m", "pytest", "-q"], PROJECT)
    write(evidence / "final-tests.log", after.stdout + after.stderr)
    checks["final_tests_pass"] = after.returncode == 0

    ingest = run([PYTHON, str(CLI), "ingest"], ROOT)
    write(evidence / "ingest.log", ingest.stdout + ingest.stderr)
    checks["ingest_runs"] = ingest.returncode == 0

    context_path = evidence / "context-before.json"
    context_run = run(
        [PYTHON, str(CLI), "context", "--format", "json", "--out", str(context_path), "--label", "verify-before"],
        ROOT,
    )
    write(evidence / "context.log", context_run.stdout + context_run.stderr)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    spec_ids = {item["id"] for item in context["specs"]}
    code_paths = {item["path"] for item in context["code"]}
    test_ids = {item["nodeid"].split("::")[-1] for item in context["tests"]}
    checks["context_has_spec"] = "SPEC-BOOKS-FILTER-3" in spec_ids
    checks["context_has_code"] = {"app/api/books.py", "app/repository.py"}.issubset(code_paths)
    checks["context_has_target_test"] = "test_filter_by_price_range" in test_ids
    checks["context_has_regression_test"] = "test_list_returns_all_books" in test_ids
    checks["context_sources_exist"] = all(
        (PROJECT / source["path"]).is_file() for source in context["selection"]["sources"]
    )
    write(evidence / "context-before.json", json.dumps(context, ensure_ascii=False, indent=2))

    original = SPEC.read_text(encoding="utf-8")
    try:
        shutil.copy2(SPEC_V2, SPEC)
        run([PYTHON, str(CLI), "ingest"], ROOT)
        context_after_path = evidence / "context-after-spec-update.json"
        run(
            [PYTHON, str(CLI), "context", "--format", "json", "--out", str(context_after_path), "--label", "verify-after"],
            ROOT,
        )
        context_after = json.loads(context_after_path.read_text(encoding="utf-8"))
        after_ids = {item["id"] for item in context_after["specs"]}
        exact = "min_price must be less than or equal to max_price"
        checks["spec_update_visible"] = "SPEC-BOOKS-FILTER-4" in after_ids and any(
            exact in item["content"] for item in context_after["specs"] if item["id"] == "SPEC-BOOKS-FILTER-3"
        )
    finally:
        SPEC.write_text(original, encoding="utf-8")
        run([PYTHON, str(CLI), "ingest"], ROOT)

    stats = run([PYTHON, str(CLI), "db-stats"], ROOT)
    write(evidence / "seekdb-stats.log", stats.stdout + stats.stderr)
    try:
        counts = json.loads(stats.stdout)
        checks["seekdb_has_chunks"] = all(int(counts.get(kind, 0)) > 0 for kind in ("spec", "code", "test"))
    except json.JSONDecodeError:
        checks["seekdb_has_chunks"] = False

    replay = run([PYTHON, str(CLI), "run", "--mode", "replay"], ROOT)
    write(evidence / "replay.log", replay.stdout + replay.stderr)
    checks["replay_patch_effective"] = '"before_exit_code": 1' in replay.stdout and '"after_exit_code": 0' in replay.stdout
    patch_text = (ROOT / "patches" / "0001-books-price-filter.patch").read_text(encoding="utf-8")
    checks["patch_tests_untouched"] = "tests/" not in patch_text and "tests\\" not in patch_text

    status = run([PYTHON, str(CLI), "status"], ROOT)
    write(evidence / "status.log", status.stdout + status.stderr)
    try:
        state = json.loads(status.stdout).get("state") or {}
        checks["powercontext_state_reused"] = (
            state.get("task_id") == "books-price-filter"
            and state.get("stage") == "verified"
            and state.get("verification", {}).get("after_tests_passed") is True
        )
    except json.JSONDecodeError:
        checks["powercontext_state_reused"] = False

    write(evidence / "summary.json", json.dumps({"checks": checks, "timestamp": stamp}, ensure_ascii=False, indent=2))
    print(json.dumps({"checks": checks, "evidence": str(evidence)}, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
