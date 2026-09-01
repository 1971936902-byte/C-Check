from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_FILES = [
    Path(r"C:\Users\19719\Desktop\E5_Wireless\ServiceProtocol.c"),
    Path(r"C:\Users\19719\Desktop\E5_Wireless\WirelessModule_EC600U.c"),
]
DEFAULT_CHECK_TYPES = [
    "memory_safety",
    "buffer_overflow",
    "pointer_safety",
    "resource_leak",
    "integer_safety",
    "logic",
]


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def login(client: httpx.Client, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    response.raise_for_status()
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def default_model_id(client: httpx.Client) -> str:
    response = client.get("/api/models")
    response.raise_for_status()
    models = response.json()
    if not models:
        raise SystemExit("no enabled model nodes returned by /api/models")
    default = next((model for model in models if model.get("is_default")), models[0])
    return default["id"]


def submit_file(client: httpx.Client, model_id: str, path: Path, run_label: str) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"source file does not exist: {path}")
    with path.open("rb") as fh:
        response = client.post(
            "/api/reviews/file",
            data={
                "model_node_id": model_id,
                "check_types": json.dumps(DEFAULT_CHECK_TYPES),
                "display_name": f"{run_label}-{path.name}",
            },
            files={"file": (path.name, fh, "text/x-csrc")},
            timeout=60,
        )
    response.raise_for_status()
    return response.json()


def wait_task(client: httpx.Client, task_id: str, poll_seconds: int) -> dict[str, Any]:
    while True:
        response = client.get(f"/api/reviews/{task_id}")
        response.raise_for_status()
        task = response.json()
        status = task["status"]
        print(
            f"{task_id} {task.get('display_name')} status={status} "
            f"progress={task.get('progress')} findings={task.get('finding_count')}",
            flush=True,
        )
        if status in {"completed", "failed"}:
            return task
        time.sleep(poll_seconds)


def fetch_report(client: httpx.Client, report_id: str) -> dict[str, Any]:
    response = client.get(f"/api/reports/{report_id}")
    response.raise_for_status()
    return response.json()


def summarize(task: dict[str, Any], report: dict[str, Any] | None) -> dict[str, Any]:
    result_json = (report or {}).get("result_json") or {}
    findings = result_json.get("findings") or []
    groups = result_json.get("finding_groups") or []
    model_log = task.get("model_log") or ""
    return {
        "task_id": task.get("id"),
        "display_name": task.get("display_name"),
        "status": task.get("status"),
        "duration_ms": task.get("duration_ms"),
        "finding_count": task.get("finding_count"),
        "group_count": len(groups),
        "high_count": (report or {}).get("high_count"),
        "medium_count": (report or {}).get("medium_count"),
        "low_count": (report or {}).get("low_count"),
        "suggestion_count": (report or {}).get("suggestion_count"),
        "category_counts": (report or {}).get("category_counts"),
        "format_failed": "Model formatting failed" in model_log,
        "server_protocol_len_hit": any(
            "ServiceProtocol.c" in str(item.get("file_path"))
            and (
                item.get("line") in {659, 700, 706, 785}
                or "uMessageLen" in str(item.get("description"))
                or "ServerProtocolRecv" in str(item.get("description"))
            )
            for item in findings
        ),
        "strstr_false_positive_hint": any(
            "strstr" in f"{item.get('title')} {item.get('description')}".lower()
            and item.get("severity") in {"high", "medium"}
            for item in findings
        ),
        "low_value_pointer_hint": any(
            item.get("severity") in {"high", "medium"}
            and item.get("category") == "pointer_safety"
            and any(
                token in f"{item.get('title')} {item.get('description')}"
                for token in ("未初始化", "未检查返回值", "空指针", "NULL")
            )
            for item in findings
        ),
        "candidate_pipeline": next(
            (line for line in model_log.splitlines() if "[CandidatePipeline]" in line),
            "",
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run C-Check single-file parameter experiments.")
    parser.add_argument("--label", required=True, help="experiment label, for example G1-B-ngram-conservative")
    parser.add_argument("--output-dir", default="outputs/qwen32b-parameter-experiments")
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--files", nargs="*", type=Path, default=DEFAULT_FILES)
    args = parser.parse_args()

    base_url = required_env("CCHECK_BASE_URL").rstrip("/")
    username = required_env("CCHECK_USERNAME")
    password = required_env("CCHECK_PASSWORD")
    model_id = os.environ.get("CCHECK_MODEL_ID")

    output_dir = Path(args.output_dir) / args.label
    output_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=30) as client:
        login(client, username, password)
        model_id = model_id or default_model_id(client)
        for path in args.files:
            created = submit_file(client, model_id, path, args.label)
            task = wait_task(client, created["id"], args.poll_seconds)
            report = fetch_report(client, task["report_id"]) if task.get("report_id") else None
            (output_dir / f"{task['id']}.task.json").write_text(
                json.dumps(task, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if report:
                (output_dir / f"{task['id']}.report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            summaries.append(summarize(task, report))

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
