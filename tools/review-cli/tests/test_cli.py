import ast
from contextlib import redirect_stderr, redirect_stdout
from email import policy
from email.parser import BytesParser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ccheck_review as cli


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, value, status=200, content_type="application/json"):
        data = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data) + (20 if self.server.truncate and content_type == "application/pdf" else 0)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            self.respond({"detail": "secret detail must not leak"}, 401)
            return False
        return True

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.path, dict(self.headers), body))
        if self.path == "/api/auth/login":
            data = json.loads(body)
            if data != {"username": "tester", "password": "test-password"}:
                return self.respond({"detail": data}, 401)
            return self.respond({"access_token": "test-token", "token_type": "bearer"})
        if not self.authorized():
            return
        if self.path == "/api/reviews/archive":
            if self.server.submit_error:
                return self.respond({"detail": "sensitive source data"}, self.server.submit_error)
            return self.respond(self.server.task, 201)
        self.respond({}, 404)

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers), None))
        if not self.authorized():
            return
        if self.path == "/api/models":
            return self.respond(self.server.models)
        if self.path == "/api/admin/resources/gpus" and self.server.old_gpu_api:
            return self.respond({}, 404)
        if self.path in ("/api/admin/resources", "/api/admin/resources/gpus"):
            if self.server.gpu_error:
                return self.respond({}, self.server.gpu_error)
            return self.respond({"captured_at": "2026-10-26T10:00:00Z", "gpus": [
                {"index": 0, "name": "RTX 5880", "memory_used_mb": 24576, "memory_total_mb": 49152,
                 "utilization_percent": 75, "memory_percent": 50}
            ]})
        if self.path.startswith("/api/reviews?"):
            return self.respond({"items": [self.server.task], "total": 1})
        if self.path == "/api/reviews/task-1":
            if self.server.sequence:
                self.server.task.update(self.server.sequence.pop(0))
            return self.respond(self.server.task)
        if self.path == "/api/reviews/redirect":
            self.send_response(302)
            self.send_header("Location", "/leaked")
            self.end_headers()
            return
        if self.path == "/api/reviews/forbidden":
            return self.respond({}, 403)
        if self.path.startswith("/api/reports/report-1"):
            if self.server.report_error:
                return self.respond({}, self.server.report_error)
            if self.path.endswith("/pdf"):
                return self.respond(self.server.pdf, content_type="application/pdf")
            if self.path.endswith("/markdown"):
                return self.respond("# Report\n\n代码\n".encode(), content_type="text/markdown; charset=utf-8")
            if self.path.endswith("/text"):
                return self.respond(b"Report text\n", content_type="text/plain")
            return self.respond({"id": "report-1", "task_id": "task-1", "result_json": {}})
        self.respond({}, 404)


class CLITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.sources = self.root / "sources"
        self.sources.mkdir()
        (self.sources / "a_c.txt").write_bytes(b"int a;\r\n")
        (self.sources / "sub").mkdir()
        (self.sources / "sub" / "b_h.txt").write_bytes(b"// \xd6\xd0\xce\xc4\n")
        (self.sources / "ignore.txt").write_bytes(b"not source")
        self.server.requests = []
        self.server.task = dict(id="task-1", display_name="Wireless", status="queued", progress=0,
                                file_count=2, finding_count=0, queued_ahead_count=1, report_id=None)
        self.server.sequence = []
        self.server.models = [dict(id="model-1", display_name="Qwen", is_enabled=True, is_default=True)]
        self.server.submit_error = 0
        self.server.report_error = 0
        self.server.pdf = b"%PDF-1.4\nmock report\n%%EOF"
        self.server.truncate = False
        self.server.gpu_error = 0
        self.server.old_gpu_api = False
        self.env = patch.dict(os.environ, {"CCHECK_REVIEW_TOKEN": "test-token", "CCHECK_REVIEW_SERVER": self.url,
                                         "CCHECK_REVIEW_CONFIG": str(self.config)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def submit(self, *extra):
        return self.invoke("submit", "--name", "Wireless", "--dir", str(self.sources), *extra)

    def ready_report(self):
        self.server.task.update(status="completed", progress=100, report_id="report-1")

    def test_archive_contract_and_bytes(self):
        code, stdout, stderr = self.submit("--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["id"], "task-1")
        path, headers, body = self.server.requests[-1]
        self.assertEqual(path, "/api/reviews/archive")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + body)
        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                  for part in message.iter_parts()}
        self.assertEqual(fields["model_node_id"], b"model-1")
        self.assertEqual(fields["display_name"], b"Wireless")
        self.assertEqual(json.loads(fields["check_types"]), list(cli.CHECKS))
        with zipfile.ZipFile(io.BytesIO(fields["file"])) as archive:
            self.assertEqual(archive.namelist(), ["a_c.txt", "sub/b_h.txt"])
            self.assertEqual(archive.read("sub/b_h.txt"), b"// \xd6\xd0\xce\xc4\n")
        self.assertEqual(len(self.server.requests), 2)

    def test_explicit_model_and_checks(self):
        self.server.models.append(dict(id="model-2", display_name="Other", is_enabled=True, is_default=False))
        code, _, error = self.submit("--model", "model-2", "--checks", "logic,buffer_overflow,logic")
        self.assertEqual(code, 0, error)
        self.assertIn(b'model-2', self.server.requests[-1][2])
        self.assertIn(b'["logic", "buffer_overflow"]', self.server.requests[-1][2])

    def test_no_default_fails_without_post(self):
        self.server.models[0]["is_default"] = False
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual(len(self.server.requests), 1)

    def test_duplicate_txt_name_rejected_locally(self):
        (self.sources / "a.c").write_text("int a;")
        self.assertEqual(self.submit()[0], 2)
        self.assertEqual(self.server.requests, [])

    def test_empty_directory_and_invalid_name(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(self.invoke("submit", "--name", "x", "--dir", str(empty))[0], 2)
        self.assertEqual(self.submit("--name", " " )[0], 2)
        self.assertEqual(self.submit("--name", "x" * 129)[0], 2)
        self.assertEqual(self.server.requests, [])

    def test_normalization(self):
        self.assertEqual(cli.source_path("dir/A_C.TXT"), "dir/A.c")
        self.assertEqual(cli.source_path("dir/a.H"), "dir/a.H")
        self.assertIsNone(cli.source_path("dir/readme.txt"))
        self.assertIsNone(cli.source_path("_c.txt"))

    def test_symlink_source_rejected(self):
        try:
            (self.sources / "link.c").symlink_to(self.sources / "a_c.txt")
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertEqual(self.submit()[0], 2)

    def test_symlink_rejection_branch(self):
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path.name == "a_c.txt" or original(path)):
            self.assertEqual(self.submit()[0], 2)
        self.assertEqual(self.server.requests, [])

    def test_wait_progress_json_and_final(self):
        self.server.sequence = [dict(status="running", progress=50), dict(status="completed", progress=100)]
        code, stdout, stderr = self.submit("--wait", "--poll-interval", "0.001", "--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "completed")
        events = [json.loads(line) for line in stderr.splitlines()]
        self.assertEqual([e["progress"] for e in events], [0, 50, 100])

    def test_gpu_snapshot_and_bounded_watch(self):
        code, stdout, stderr = self.invoke("gpu", "--watch", "--samples", "2", "--poll-interval", "0.001", "--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["gpus"][0]["memory_used_mb"], 24576)
        self.assertEqual(len(stderr.splitlines()), 2)
        self.assertNotIn("\x1b", stderr)

    def test_wait_gpu_permission_does_not_fail_task_or_repeat_403(self):
        self.server.gpu_error = 403
        self.server.sequence = [dict(status="running", progress=60), dict(status="completed", progress=100)]
        code, stdout, stderr = self.submit("--wait", "--gpu", "--poll-interval", "0.001", "--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "completed")
        self.assertEqual(len([r for r in self.server.requests if r[0] == "/api/admin/resources/gpus"]), 1)
        self.assertIn("Admin account required", stderr)

    def test_gpu_error_not_zero_usage(self):
        self.server.gpu_error = 503
        code, stdout, _ = self.invoke("gpu")
        self.assertEqual(code, 1)
        self.assertIn("unavailable", stdout)
        self.assertNotIn("0.0%", stdout)
        self.assertIn("N/A", cli.bar(None))

    def test_gpu_legacy_api_fallback_only_probes_once(self):
        self.server.old_gpu_api = True
        code, _, _ = self.invoke("gpu", "--watch", "--samples", "2", "--poll-interval", "0.001", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len([r for r in self.server.requests if r[0] == "/api/admin/resources/gpus"]), 1)
        self.assertEqual(len([r for r in self.server.requests if r[0] == "/api/admin/resources"]), 2)

    def test_gpu_bars_distinguish_memory_and_compute(self):
        code, stdout, _ = self.invoke("gpu")
        self.assertEqual(code, 0)
        self.assertIn("50.0%", stdout)
        self.assertIn("75.0%", stdout)
        self.assertIn("24.0/48.0 GiB", stdout)
        self.assertIn("NOT task-exclusive", stdout)

    def test_file_counts_not_estimated_from_percentage(self):
        self.server.task.update(progress=95)
        _, stdout, _ = self.invoke("status", "task-1")
        self.assertIn("remaining=N/A", stdout)
        self.server.task.update(scan_file_total=10, scan_file_completed=6, scan_file_remaining=4)
        _, stdout, _ = self.invoke("status", "task-1")
        self.assertIn("scan_files=6/10 remaining=4", stdout)

    def test_live_display_redraw_and_terminal_width(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        stream = TTY()
        with patch.dict(os.environ, {"TERM": "xterm"}), patch.object(cli.shutil, "get_terminal_size", return_value=os.terminal_size((30, 20))):
            display = cli.LiveDisplay(stream)
            display.draw(["task name " + "a" * 50, "Review " + cli.bar(50)])
            display.draw(["completed"])
        self.assertIn("\x1b[2A", stream.getvalue())
        self.assertIn("\x1b[2K", stream.getvalue())
        self.assertNotIn("a" * 30, stream.getvalue())
        self.assertEqual(cli.clip_columns("中文abcd", 5), "中文a")

    def test_redirected_and_dumb_output_has_no_ansi(self):
        self.server.task.update(status="completed", progress=100)
        _, _, stderr = self.invoke("status", "task-1", "--wait")
        self.assertIn("[########################]", stderr)
        self.assertNotIn("\x1b", stderr)
        stream = io.StringIO()
        with patch.dict(os.environ, {"TERM": "dumb"}):
            cli.LiveDisplay(stream).draw(["a\x1b[2J", "b"])
        self.assertNotIn("\x1b", stream.getvalue())

    def test_gpu_samples_validation(self):
        self.assertEqual(self.invoke("gpu", "--samples", "2")[0], 2)
        self.assertEqual(self.invoke("gpu", "--watch", "--samples", "0")[0], 2)

    def test_wait_timeout_keeps_task_id(self):
        code, _, stderr = self.submit("--wait", "--wait-timeout", "0.001", "--json")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(stderr.splitlines()[-1])["task_id"], "task-1")
        self.assertEqual(self.server.task["status"], "queued")

    def test_failed_status_exit_code(self):
        self.server.task.update(status="failed", error_message="worker failed")
        code, stdout, _ = self.invoke("status", "task-1", "--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(stdout)["error_message"], "worker failed")

    def test_invalid_options_are_json_errors(self):
        for args in (("submit",), ("status", "task-1", "--poll-interval", "nan"),
                     ("submit", "--name", "x", "--dir", str(self.sources), "--checks", "bad")):
            with self.subTest(args=args):
                code, stdout, stderr = self.invoke("--json", *args)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["exit_code"], 2)

    def test_options_before_command(self):
        code, stdout, _ = self.invoke("--json", "--server", self.url + "/api/", "status", "task-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["id"], "task-1")

    def test_help_is_offline_without_valid_config(self):
        self.config.write_text("invalid JSON", encoding="utf-8")
        for args in (("help",), ("--server", "invalid", "help"), ("help", "help")):
            code, stdout, stderr = self.invoke(*args)
            self.assertEqual(code, 0, stderr)
            self.assertIn("usage:", stdout)
            self.assertEqual(stderr, "")
        self.assertEqual(self.server.requests, [])

    def test_help_for_every_subcommand_matches_dash_help(self):
        for command in ("login", "logout", "submit", "status", "list", "report", "models", "checks", "gpu", "help"):
            with self.subTest(command=command):
                code, stdout, stderr = self.invoke("help", command)
                self.assertEqual(code, 0, stderr)
                expected = io.StringIO()
                with redirect_stdout(expected), self.assertRaises(SystemExit) as raised:
                    cli.main([command, "--help"])
                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(stdout, expected.getvalue())
        self.assertEqual(self.server.requests, [])

    def test_help_unknown_topic_is_usage_error(self):
        code, stdout, stderr = self.invoke("help", "missing", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["exit_code"], 2)
        self.assertEqual(self.server.requests, [])

    def test_login_config_and_logout(self):
        with patch.dict(os.environ, {"CCHECK_REVIEW_TOKEN": ""}), patch("sys.stdin", io.StringIO("test-password\n")):
            code, stdout, stderr = self.invoke("login", "--username", "tester", "--password-stdin", "--json")
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("test-token", stdout + stderr)
            self.assertNotIn("test-password", self.config.read_text())
            self.assertEqual(self.invoke("status", "task-1")[0], 0)
            self.assertEqual(self.invoke("logout")[0], 0)
            self.assertEqual(self.invoke("status", "task-1")[0], 3)

    def test_token_not_reused_for_other_server(self):
        cli.save_config(self.config, {"profiles": {"http://localhost:1/api": {"token": "test-token"}}})
        with patch.dict(os.environ, {"CCHECK_REVIEW_TOKEN": ""}):
            self.assertEqual(self.invoke("status", "task-1")[0], 3)
        self.assertEqual(self.server.requests, [])

    def test_auth_failure_does_not_echo_server_details(self):
        with patch.dict(os.environ, {"CCHECK_REVIEW_TOKEN": "bad"}):
            code, _, stderr = self.invoke("status", "task-1", "--json")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stderr)["http_status"], 401)
        self.assertNotIn("secret detail", stderr)

    def test_forbidden_and_missing(self):
        self.assertEqual(self.invoke("status", "forbidden")[0], 3)
        self.assertEqual(self.invoke("status", "missing")[0], 1)

    def test_redirect_not_followed(self):
        code, _, error = self.invoke("status", "redirect")
        self.assertEqual(code, 1)
        self.assertIn("Redirect refused", error)
        self.assertEqual(len(self.server.requests), 1)

    def test_remote_http_requires_opt_in(self):
        code, _, _ = self.invoke("status", "task-1", "--server", "http://192.0.2.1:8800")
        self.assertEqual(code, 2)

    def test_server_validation(self):
        self.assertEqual(cli.server_url("https://EXAMPLE.com:443/api/"), "https://example.com/api")
        for url in ("http://u:p@host", "file:///tmp/file", "http://host/workspace", "http://host/?token=x"):
            with self.assertRaises(cli.CLIError):
                cli.server_url(url)

    def test_submit_not_retried(self):
        self.server.submit_error = 503
        code, _, error = self.submit("--json")
        self.assertEqual(code, 1)
        self.assertIn("NOT retried", json.loads(error)["error"])
        self.assertNotIn("sensitive source", error)
        self.assertEqual(len([r for r in self.server.requests if r[0].endswith("archive")]), 1)

    def test_list_name_query(self):
        code, stdout, _ = self.invoke("list", "--name", "a b&c", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["total"], 1)
        self.assertIn("keyword=a+b%26c", self.server.requests[-1][0])

    def test_pdf_download_uses_report_id(self):
        self.ready_report()
        output = self.root / "report.pdf"
        code, stdout, stderr = self.invoke("report", "task-1", "--output", str(output), "--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output.read_bytes(), self.server.pdf)
        self.assertEqual(json.loads(stdout)["report_id"], "report-1")
        self.assertEqual(self.server.requests[-1][0], "/api/reports/report-1/pdf")

    def test_report_formats(self):
        self.ready_report()
        for extension, fmt in (("md", "markdown"), ("txt", "text"), ("json", "json")):
            with self.subTest(extension=extension):
                output = self.root / ("report." + extension)
                code, stdout, stderr = self.invoke("report", "task-1", "--output", str(output), "--json")
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["format"], fmt)
                self.assertGreater(output.stat().st_size, 0)
        code, stdout, _ = self.invoke("report", "task-1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["id"], "report-1")

    def test_report_not_ready(self):
        self.assertEqual(self.invoke("report", "task-1", "--json")[0], 1)

    def test_no_overwrite_and_failed_download_preserves_existing(self):
        self.ready_report()
        output = self.root / "report.pdf"
        output.write_bytes(b"original")
        self.assertEqual(self.invoke("report", "task-1", "--output", str(output))[0], 2)
        for error in (503, 0):
            self.server.report_error = error
            self.server.pdf = b"not a PDF"
            self.assertEqual(self.invoke("report", "task-1", "--output", str(output), "--force")[0], 1)
            self.assertEqual(output.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".ccheck-*")), [])

    def test_truncated_report_does_not_replace_existing(self):
        self.ready_report()
        output = self.root / "report.pdf"
        output.write_bytes(b"original")
        self.server.truncate = True
        code, _, stderr = self.invoke("report", "task-1", "--output", str(output), "--force", "--json")
        self.assertEqual(code, 1, stderr)
        self.assertEqual(output.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".ccheck-*")), [])

    def test_config_parse_error_is_actionable_json(self):
        self.config.write_text("{", encoding="utf-8")
        code, _, stderr = self.invoke("status", "task-1", "--json")
        self.assertEqual(code, 2)
        self.assertIn("configuration", json.loads(stderr)["error"])

    def test_report_requires_valid_output_format(self):
        self.assertEqual(self.invoke("report", "task-1", "--output", str(self.root / "file.bin"))[0], 2)
        self.assertEqual(self.invoke("report", "task-1", "--format", "pdf")[0], 2)
        self.assertEqual(self.server.requests, [])

    def test_cancel_does_not_cancel_server_task(self):
        with patch.object(cli, "wait_for_task", side_effect=KeyboardInterrupt):
            code, _, stderr = self.submit("--wait", "--json")
        self.assertEqual(code, 130)
        self.assertIn("continues", json.loads(stderr)["error"])
        self.assertEqual(len(self.server.requests), 2)

    def test_check_keys_match_backend(self):
        tree = ast.parse((ROOT.parents[1] / "backend/app/services/check_types.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CHECK_TYPE_LABELS" for t in node.targets):
                self.assertEqual(tuple(ast.literal_eval(node.value)), cli.CHECKS)
                break
        else:
            self.fail("Backend check category contract changed")

    def test_zipapp_build_and_run(self):
        paths = [self.root / "client1.pyz", self.root / "client2.pyz"]
        for path in paths:
            subprocess.run([sys.executable, str(ROOT / "build_zipapp.py"), "--output", str(path)], check=True, capture_output=True)
        self.assertEqual(hashlib.sha256(paths[0].read_bytes()).digest(), hashlib.sha256(paths[1].read_bytes()).digest())
        result = subprocess.run([sys.executable, str(paths[0]), "checks", "--json"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["checks"], list(cli.CHECKS))
        result = subprocess.run([sys.executable, str(paths[0]), "help", "submit"], check=True, capture_output=True, text=True)
        self.assertIn("--name", result.stdout)
        self.assertIn("--dir", result.stdout)


if __name__ == "__main__":
    unittest.main()
