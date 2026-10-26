"""Exercise the standalone CLI against real routes, without dispatching a review."""
import importlib.util
import io
import json
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.db.models import ModelNode, Report, ReviewTask, TaskStatus, User
from app.core.security import hash_password


def test_review_cli_real_api_contract(tmp_path, monkeypatch, db_session_factory):
    from app.main import app

    module_path = Path(__file__).resolve().parents[2] / "tools/review-cli/ccheck_review.py"
    spec = importlib.util.spec_from_file_location("review_cli_contract", module_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("JWT_SECRET", "test-review-cli-jwt-secret-at-least-32-characters")
    monkeypatch.setenv("CCHECK_REVIEW_CONFIG", str(tmp_path / "client.json"))
    monkeypatch.setenv("CCHECK_REVIEW_SERVER", "http://testserver")
    monkeypatch.delenv("CCHECK_REVIEW_TOKEN", raising=False)
    dispatched = []
    monkeypatch.setattr("app.services.submissions.dispatch_review", dispatched.append)
    with db_session_factory() as db:
        user = User(username="cli-user", password_hash=hash_password("cli-test-password"))
        model = ModelNode(display_name="Qwen", model_identifier="test-model", base_url="http://unused",
                          is_enabled=True, is_default=True)
        db.add_all([user, model])
        db.commit()
        model_id = model.id

    with TestClient(app) as http:
        class Opener:
            def open(self, request, timeout=None):
                parsed = urlsplit(request.full_url)
                content = request.data
                if hasattr(content, "read"):
                    content = content.read()
                response = http.request(request.get_method(), parsed.path + ("?" + parsed.query if parsed.query else ""),
                                        headers=dict(request.header_items()), content=content)
                headers = Message()
                for name, value in response.headers.items():
                    headers[name] = value
                body = io.BytesIO(response.content)
                body.headers = headers
                if response.status_code >= 400:
                    raise HTTPError(request.full_url, response.status_code, "error", headers, body)
                return body

        monkeypatch.setattr(cli.urllib.request, "build_opener", lambda *args: Opener())

        def invoke(*args):
            output = io.StringIO()
            with redirect_stdout(output):
                assert cli.main([*args, "--json"]) == 0
            return json.loads(output.getvalue())

        monkeypatch.setattr("sys.stdin", io.StringIO("cli-test-password\n"))
        login = invoke("login", "--username", "cli-user", "--password-stdin", "--allow-insecure-http")
        assert login["logged_in"] is True
        source = tmp_path / "source"
        source.mkdir()
        (source / "hello_c.txt").write_bytes(b"int main(void) { return 0; }\n")
        result = invoke("submit", "--name", "CLI contract", "--dir", str(source))
        assert result["model_node_id"] == model_id
        assert result["check_types"] == list(cli.CHECKS)
        assert result["file_count"] == 1
        assert result["scan_file_total"] is None
        assert result["scan_file_remaining"] is None
        assert result["files"][0]["relative_path"] == "hello.c"
        task_id = result["id"]
        assert dispatched == [task_id]
        assert invoke("status", task_id)["status"] == "queued"
        assert invoke("list", "--name", "CLI contract")["total"] == 1

        with db_session_factory() as db:
            task = db.get(ReviewTask, task_id)
            task.status = TaskStatus.COMPLETED
            task.progress = 100
            task.scan_file_total = 1
            task.scan_file_completed = 1
            db.add(Report(task=task, summary="CLI integration report", score=100, result_json={"findings": []}))
            db.commit()
        assert invoke("status", task_id)["scan_file_remaining"] == 0
        report = invoke("report", task_id)
        assert report["summary"] == "CLI integration report"
        assert report["task_id"] == task_id
        target = tmp_path / "report.md"
        saved = invoke("report", task_id, "--output", str(target))
        assert saved["format"] == "markdown"
        assert "CLI integration report" in target.read_text(encoding="utf-8")
