"""Apply a recorded patch or a MiniMax-generated edit to the demo project."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def prepare_baseline(project: Path, snapshots: Path) -> Path:
    """Copy the final project to a temporary directory and restore the before snapshot."""

    temp = Path(tempfile.mkdtemp(prefix="sdd-baseline-"))
    shutil.copytree(
        project,
        temp / project.name,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", "*.pyc"),
    )
    target = temp / project.name
    before = snapshots / "before"
    for source in before.rglob("*"):
        if source.is_file():
            relative = source.relative_to(before)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def run_tests(project: Path, log_path: Path) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    log_path.write_text(output, encoding="utf-8")
    return result.returncode, output


def apply_patch(project: Path, patch: Path) -> None:
    result = subprocess.run(["patch", "-p1", "-i", str(patch)], cwd=project, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"patch failed: {result.stdout}\n{result.stderr}")


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = _THINK.sub("", text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def apply_edits(project: Path, edits: list[dict[str, str]]) -> None:
    for edit in edits:
        relative = Path(str(edit["path"]))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "app":
            raise ValueError(f"MiniMax tried to edit outside app/: {relative}")
        path = project / relative
        if not path.is_file():
            raise ValueError(f"edit target does not exist: {relative}")
        source = path.read_text(encoding="utf-8")
        old = str(edit["old"])
        new = str(edit["new"])
        if source.count(old) != 1:
            raise ValueError(f"edit anchor must appear exactly once in {relative}")
        path.write_text(source.replace(old, new), encoding="utf-8")


def diff_project(project: Path, baseline_project: Path) -> str:
    parts: list[str] = []
    for final_file in sorted((project / "app").rglob("*.py")):
        relative = final_file.relative_to(project)
        baseline = baseline_project / relative
        old = baseline.read_text(encoding="utf-8").splitlines(keepends=True) if baseline.exists() else []
        new = final_file.read_text(encoding="utf-8").splitlines(keepends=True)
        if old == new:
            continue
        parts.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{relative.as_posix()}",
                tofile=f"b/{relative.as_posix()}",
            )
        )
    return "".join(parts)


def write_record(record_dir: Path, name: str, content: str) -> None:
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / name).write_text(content, encoding="utf-8")


def replay(project: Path, snapshots: Path, patch: Path, record_dir: Path, state: Any = None) -> dict[str, Any]:
    baseline = prepare_baseline(project, snapshots)
    original = Path(tempfile.mkdtemp(prefix="sdd-original-")) / project.name
    shutil.copytree(baseline, original)
    record_dir.mkdir(parents=True, exist_ok=True)
    before_code, before_output = run_tests(baseline, record_dir / "tests-before.log")
    apply_patch(baseline, patch)
    after_code, after_output = run_tests(baseline, record_dir / "tests-after.log")
    generated = diff_project(baseline, original)
    write_record(record_dir, "patch.diff", generated)
    result = {
        "mode": "replay",
        "baseline": str(baseline),
        "before_exit_code": before_code,
        "after_exit_code": after_code,
        "before_output": before_output,
        "after_output": after_output,
        "patch": str(patch),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if state is not None:
        state.write_state(
            {
                "task_id": "books-price-filter",
                "stage": "verified" if after_code == 0 else "failed",
                "selected": {"patch": str(patch)},
                "verification": {"before_target_failed": before_code != 0, "after_tests_passed": after_code == 0},
                "done": ["replayed patch", "ran target and regression tests"],
                "todo": [],
            }
        )
    return result


def _request(
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    conversation: list[dict[str, str]],
) -> tuple[str, dict[str, object], dict[str, str]]:
    if protocol == "anthropic":
        url = base_url.rstrip("/") + "/v1/messages"
        payload: dict[str, object] = {
            "model": model,
            "max_tokens": 16384,
            "output_config": {"effort": "medium"},
            "system": system,
            "messages": [
                {"role": message["role"], "content": [{"type": "text", "text": message["content"]}]}
                for message in conversation
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *conversation],
            "max_completion_tokens": 8000,
        }
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    return url, payload, headers


def _response_text(protocol: str, payload: dict[str, Any]) -> str:
    if protocol == "anthropic":
        blocks = payload.get("content", [])
        return "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
    return str(payload["choices"][0]["message"].get("content") or "")


def _sanitized_payload(protocol: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe = json.loads(json.dumps(payload))
    if protocol == "anthropic":
        blocks = safe.get("content", [])
        safe["content"] = [
            {"type": "text", "text": _THINK.sub("", str(block.get("text", "")))}
            for block in blocks
            if block.get("type") == "text"
        ]
        return safe
    for choice in safe.get("choices", []):
        message = choice.get("message", {})
        message.pop("reasoning_content", None)
        if isinstance(message.get("content"), str):
            message["content"] = _THINK.sub("", message["content"])
    return safe


def live(
    project: Path,
    snapshots: Path,
    context: dict[str, Any],
    record_dir: Path,
    *,
    api_key: str,
    base_url: str,
    model: str,
    protocol: str = "anthropic",
    state: Any = None,
) -> dict[str, Any]:
    protocol = protocol.strip().lower()
    if protocol not in {"anthropic", "openai"}:
        raise ValueError(f"unsupported MiniMax protocol: {protocol}")
    baseline = prepare_baseline(project, snapshots)
    original = Path(tempfile.mkdtemp(prefix="sdd-original-")) / project.name
    shutil.copytree(baseline, original)
    record_dir.mkdir(parents=True, exist_ok=True)
    before_code, before_output = run_tests(baseline, record_dir / "tests-before.log")
    system = (
        "You are a careful software engineer. The supplied code is the current pre-change state; "
        "the task is not implemented yet. Use only the supplied specification, code and tests. "
        "Edit only files under app/. Never edit tests. Preserve existing behaviour. "
        'Return at least one edit, as strict JSON: {"edits":[{"path":"app/...","old":"exact existing text","new":"replacement"}]}. '
        "Keep each old anchor unique in the file. Do not return an empty edits list."
    )
    body = {
        "task": context["task"],
        "specs": context["specs"],
        "code": context["code"],
        "tests": context["tests"],
    }
    conversation: list[dict[str, str]] = [{"role": "user", "content": json.dumps(body, ensure_ascii=False)}]
    write_record(
        record_dir,
        "request.json",
        json.dumps(
            {"protocol": protocol, "base_url": base_url, "model": model, "system": system, "conversation": conversation},
            ensure_ascii=False,
            indent=2,
        ),
    )
    outputs: list[dict[str, Any]] = []
    for attempt in range(1, 4):
        url, payload, headers = _request(protocol, base_url, api_key, model, system, conversation)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode("utf-8", errors="replace")) from error
        content = _response_text(protocol, response_payload)
        write_record(
            record_dir,
            f"response-{attempt}.json",
            json.dumps(_sanitized_payload(protocol, response_payload), ensure_ascii=False, indent=2),
        )
        outputs.append({"attempt": attempt, "content": content, "usage": response_payload.get("usage")})
        if not content.strip():
            conversation.append(
                {
                    "role": "user",
                    "content": "You returned no text. Return the required strict JSON object with at least one edit.",
                }
            )
            continue
        try:
            edits = _extract_json(content).get("edits", [])
            if not edits:
                raise ValueError("the edits list is empty; implement the task")
            apply_edits(baseline, edits)
        except Exception as error:  # feed the parser/apply error back to the model
            conversation.append({"role": "assistant", "content": content})
            conversation.append(
                {"role": "user", "content": f"Your edits could not be applied: {error}. Return corrected JSON."}
            )
            continue
        after_code, after_output = run_tests(baseline, record_dir / f"tests-after-{attempt}.log")
        if after_code == 0:
            generated = diff_project(baseline, original)
            write_record(record_dir, "patch.diff", generated)
            result = {
                "mode": "live",
                "protocol": protocol,
                "baseline": str(baseline),
                "model": model,
                "attempts": outputs,
                "before_exit_code": before_code,
                "after_exit_code": after_code,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            if state is not None:
                state.write_state(
                    {
                        "task_id": "books-price-filter",
                        "stage": "verified",
                        "selected": {"specs": [spec["id"] for spec in context["specs"]]},
                        "verification": {"before_target_failed": before_code != 0, "after_tests_passed": True},
                        "done": ["MiniMax generated the patch", "target and regression tests passed"],
                        "todo": [],
                    }
                )
            return result
        conversation.append({"role": "assistant", "content": content})
        conversation.append(
            {
                "role": "user",
                "content": f"Tests still fail. Output:\n{after_output[-4000:]}\nReturn corrected JSON edits.",
            }
        )
    raise RuntimeError("MiniMax did not produce a passing patch in 3 attempts")
