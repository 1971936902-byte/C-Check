"""Dependency-free C-Check client. No model or deployment dependencies required."""

import argparse
import getpass
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
import zipfile
from contextlib import contextmanager

VERSION = "0.2.1"
CHECKS = (
    "memory_safety", "buffer_overflow", "pointer_safety",
    "resource_leak", "integer_safety", "logic",
)


class CLIError(Exception):
    def __init__(self, message, code=1, **context):
        super().__init__(message)
        self.code = code
        self.context = context


def json_text(value):
    return json.dumps(value, ensure_ascii=False)


def safe_text(value):
    # Server-controlled names/errors must not emit terminal escape sequences.
    return "".join(c if c.isprintable() else " " for c in str(value))


def safe_multiline(value):
    return "\n".join(safe_text(line) for line in str(value).splitlines())


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def check_list(value):
    items = list(dict.fromkeys(item.strip() for item in value.split(",")))
    if not items or any(item not in CHECKS for item in items):
        raise argparse.ArgumentTypeError("use comma-separated keys: " + ",".join(CHECKS))
    return items


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError(message, 2)


def parser():
    result = Parser(description="Submit and inspect C-Check reviews using the existing API.")
    result.add_argument("--version", action="version", version=VERSION)
    # Suppressed defaults let common options appear before OR after the command.
    def common(p):
        p.add_argument("--server", default=argparse.SUPPRESS, help="service URL, optionally ending in /api")
        p.add_argument("--config", default=argparse.SUPPRESS, help="local JSON configuration file")
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--allow-insecure-http", action="store_true", default=argparse.SUPPRESS,
                       help="explicitly allow unencrypted HTTP to a non-loopback server")
        p.add_argument("--timeout", type=positive, default=argparse.SUPPRESS, help="HTTP timeout, seconds (default 60)")
    common(result)
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    login = commands.add_parser("login", help="log in and save a server-scoped access token")
    login.add_argument("--username", required=True)
    login.add_argument("--password-stdin", action="store_true")
    commands.add_parser("logout", help="remove the locally saved token for this server")
    commands.add_parser("models", help="list visible model node IDs and the default model")
    commands.add_parser("checks", help="list the six supported check category keys")
    submit = commands.add_parser("submit", help="recursively upload C/H sources from a local directory")
    submit.add_argument("--name", required=True)
    submit.add_argument("--dir", required=True, type=Path)
    submit.add_argument("--model", help="model node ID from the models command")
    submit.add_argument("--checks", type=check_list, default=list(CHECKS))
    status = commands.add_parser("status", help="show task status, optionally wait for completion")
    status.add_argument("task_id")
    for command in (submit, status):
        command.add_argument("--wait", action="store_true")
        command.add_argument("--gpu", action="store_true", help="also show server GPU telemetry (admin account required)")
        command.add_argument("--poll-interval", type=positive, default=5)
        command.add_argument("--wait-timeout", type=positive, help="maximum wait seconds; omitted means unlimited")
    gpu = commands.add_parser("gpu", help="show server GPU memory and utilization (admin account required)")
    gpu.add_argument("--watch", action="store_true", help="continuously refresh telemetry")
    gpu.add_argument("--poll-interval", type=positive, default=5)
    gpu.add_argument("--samples", type=int, help="stop watching after this many snapshots")
    listing = commands.add_parser("list", help="list recent tasks (also useful after an uncertain submission)")
    listing.add_argument("--name", help="task name substring")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    report = commands.add_parser("report", help="download the existing report for a task ID")
    report.add_argument("task_id")
    report.add_argument("--output", type=Path)
    report.add_argument("--format", choices=("pdf", "markdown", "text", "json"))
    report.add_argument("--force", action="store_true", help="replace an existing output file")
    help_command = commands.add_parser("help", help="show general help or help for a command")
    help_command.add_argument("topic", nargs="?", choices=commands.choices, help="command to describe")
    help_command.set_defaults(help_root=result, help_commands=commands.choices)
    for command in commands.choices.values():
        common(command)
    return result


def config_path(args):
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return Path(getattr(args, "config", os.environ.get("CCHECK_REVIEW_CONFIG", str(root / "ccheck-review/config.json")))).expanduser()


def load_config(path):
    if not path.exists():
        return {}
    try:
        if path.is_symlink():
            raise ValueError("symbolic link")
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("permissions must be 0600 (chmod 600)")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("profiles", {}), dict):
            raise ValueError("expected configuration object with profiles object")
        return data
    except (ValueError, OSError) as exc:
        raise CLIError(f"Cannot read configuration: {exc}", 2) from exc


def save_config(path, config):
    if path.is_symlink():
        raise CLIError("Refusing to overwrite a symlink configuration", 2)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with atomic_output(path, force=True) as output:
        output.write((json_text(config) + "\n").encode("utf-8"))


@contextmanager
def atomic_output(path, force=False):
    path = path.absolute()
    if path.exists() and not force:
        raise CLIError("Output exists; use --force to replace it", 2)
    fd, name = tempfile.mkstemp(prefix=".ccheck-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            yield output
            output.flush()
            os.fsync(output.fileno())
        if force:
            os.replace(temporary, path)
        else:
            # Atomic no-clobber publication, including concurrent writers.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def server_url(raw):
    try:
        parsed = urllib.parse.urlsplit(raw)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError()
        port = parsed.port
        host = parsed.hostname.lower()
        authority = f"[{host}]" if ":" in host else host
        if port and port != {"http": 80, "https": 443}[parsed.scheme]:
            authority += f":{port}"
        path = parsed.path.rstrip("/")
        if path and path != "/api":
            raise ValueError()
        return f"{parsed.scheme}://{authority}/api"
    except (ValueError, TypeError) as exc:
        raise CLIError("Server must be an http(s) origin, optionally ending in /api; no credentials/query/path", 2) from exc


def require_secure_transport(server, allowed):
    parsed = urllib.parse.urlsplit(server)
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if parsed.scheme == "http" and not local and not allowed:
        raise CLIError("Remote HTTP exposes credentials and source code. Use HTTPS/SSH forwarding, or explicitly --allow-insecure-http", 2)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, server, token=None, timeout=60):
        self.server = server
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, path, *, data=None, headers=None, authenticated=True):
        headers = dict(headers or {})
        if authenticated:
            if not self.token:
                raise CLIError("Not logged in for this server; run login or set CCHECK_REVIEW_TOKEN", 3)
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.server + path, data=data, headers=headers)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # Validation responses can echo input/passwords; never dump response bodies.
            hints = {401: "Authentication expired or invalid; log in again", 403: "Access denied",
                     404: "Task/report/model or API not found", 413: "Upload exceeds server limit",
                     422: "Invalid input; check categories, filenames and server upload limits",
                     503: "Service unavailable, maintenance active, or report rendering unavailable"}
            message = hints.get(exc.code, "HTTP request failed")
            if 300 <= exc.code < 400:
                message = "Redirect refused; configure the final server URL"
            exc.close()
            raise CLIError(f"{message} (HTTP {exc.code})", 3 if exc.code in (401, 403) else 1,
                           http_status=exc.code) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise CLIError("Connection failed or timed out; check server address/network", 1) from exc

    def json(self, path, *, payload=None, authenticated=True):
        data = None if payload is None else json_text(payload).encode("utf-8")
        headers = {} if data is None else {"Content-Type": "application/json"}
        with self.open(path, data=data, headers=headers, authenticated=authenticated) as response:
            return read_json(response)


def read_json(response):
    if response.headers.get_content_type() != "application/json":
        raise CLIError("Expected JSON from API; check server URL/reverse proxy")
    try:
        return json.load(response)
    except (ValueError, UnicodeError) as exc:
        raise CLIError("API returned invalid JSON") from exc


def source_path(relative):
    lower = relative.lower()
    for suffix, extension in (("_c.txt", ".c"), ("_h.txt", ".h")):
        if lower.endswith(suffix) and len(PurePosixPath(relative).name) > len(suffix):
            return relative[:-len(suffix)] + extension
    return relative if lower.endswith((".c", ".h")) else None


def source_files(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise CLIError("--dir must be an existing directory, not a symlink", 2)
    root = directory.resolve()
    seen = set()
    files = []
    def fail(error):
        raise error
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink())
        for name in sorted(names):
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            normalized = source_path(relative)
            if normalized is None:
                continue
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise CLIError("Source file is a symlink or special file: " + safe_text(relative), 2)
            if "\\" in relative or ":" in relative or any(ord(c) < 32 for c in relative):
                raise CLIError("Unsupported characters in source path: " + safe_text(relative), 2)
            if normalized in seen:
                raise CLIError("Duplicate source path after TXT normalization: " + safe_text(normalized), 2)
            seen.add(normalized)
            files.append((path, relative))
    if not files or not any(path.stat().st_size for path, _ in files):
        raise CLIError("Directory contains no non-empty C/H or _c.txt/_h.txt source files", 2)
    return files


@contextmanager
def multipart_archive(files, fields):
    # Use disk-backed temporary files: source trees need not fit into client RAM.
    with tempfile.TemporaryFile() as archive, tempfile.TemporaryFile() as body:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path, relative in files:
                bundle.write(path, relative)
        boundary = "ccheck-" + uuid.uuid4().hex
        for key, value in fields.items():
            body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode("utf-8"))
        body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sources.zip"\r\n'
                    'Content-Type: application/zip\r\n\r\n').encode("ascii"))
        archive.seek(0)
        shutil.copyfileobj(archive, body)
        body.write(f"\r\n--{boundary}--\r\n".encode("ascii"))
        length = body.tell()
        body.seek(0)
        yield body, {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(length)}


def identifier(value):
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise CLIError("Invalid task/report ID", 2)
    return value


def task_summary(task):
    result = (f"{task['id']}  {task.get('display_name', '')}  {task['status']}  "
              f"{task.get('progress', 0)}%  files={task.get('file_count', 0)}  findings={task.get('finding_count', 0)}")
    if task.get("queued_ahead_count") is not None:
        result += f"  queued_ahead={task['queued_ahead_count']}"
    if task.get("error_message"):
        result += f"  error={task['error_message']}"
    result += "  " + file_summary(task)
    return safe_text(result)


def file_summary(task):
    total, completed = task.get("scan_file_total"), task.get("scan_file_completed")
    if (type(total) is int and type(completed) is int and 0 <= completed <= total):
        return f"scan_files={completed}/{total} remaining={total - completed}"
    return "scan_files=N/A remaining=N/A (not reported by server yet)"


def percentage(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return max(0, min(100, value))
    return None


def bar(value, width=24):
    value = percentage(value)
    if value is None:
        return "[" + "?" * width + "] N/A"
    count = int(value * width / 100)
    return "[" + "#" * count + "-" * (width - count) + f"] {value:5.1f}%"


def clip_columns(text, columns):
    result, used = [], 0
    for char in safe_text(text):
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + size > columns:
            break
        result.append(char)
        used += size
    return "".join(result)


class LiveDisplay:
    """Refresh only our own terminal lines; redirected output stays escape-free."""
    def __init__(self, stream):
        self.stream = stream
        self.live = stream.isatty() and os.environ.get("TERM") != "dumb"
        self.previous_lines = 0

    def draw(self, lines):
        if not self.live:
            print(" | ".join(safe_text(line) for line in lines), file=self.stream, flush=True)
            return
        size = shutil.get_terminal_size((80, 24))
        columns = max(1, size.columns - 1)
        rows = max(2, size.lines - 1)
        if len(lines) > rows:
            lines = lines[:rows - 1] + [f"... {len(lines) - rows + 1} lines hidden; use --json for all GPUs"]
        if self.previous_lines:
            self.stream.write(f"\x1b[{self.previous_lines}A")
        for line in lines:
            self.stream.write("\r\x1b[2K" + clip_columns(line, columns) + "\n")
        extra = self.previous_lines - len(lines)
        if extra > 0:
            self.stream.write("\r\x1b[2K\n" * extra + f"\x1b[{extra}A")
        self.previous_lines = len(lines)
        self.stream.flush()


def gpu_lines(snapshot):
    if snapshot.get("error"):
        return ["GPU: unavailable - " + snapshot["error"]]
    lines = ["Server GPUs (shared, NOT task-exclusive); sampled " + str(snapshot.get("captured_at", "N/A"))]
    devices = snapshot.get("gpus") or []
    if not devices:
        return lines + ["GPU: unavailable (no devices reported)"]
    for device in devices:
        used, total = device.get("memory_used_mb"), device.get("memory_total_mb")
        valid_memory = (isinstance(used, (int, float)) and isinstance(total, (int, float))
                        and math.isfinite(used) and math.isfinite(total) and total > 0 and 0 <= used <= total)
        memory = 100 * used / total if valid_memory else device.get("memory_percent")
        amount = f"{used / 1024:.1f}/{total / 1024:.1f} GiB" if valid_memory else "N/A GiB"
        lines.extend([
            f"GPU {device.get('index', '?')} {device.get('name', '')}",
            f"  VRAM {bar(memory)} {amount}",
            f"  GPU  {bar(device.get('utilization_percent'))}",
        ])
    return lines


def task_lines(task):
    ahead = task.get("queued_ahead_count")
    lines = [f"{task['id']}  {task.get('display_name', '')}",
             f"Review {bar(task.get('progress'))} {task['status']}",
             file_summary(task) + f"  uploaded={task.get('file_count', 0)}",
             f"findings={task.get('finding_count', 0)}  queued_ahead={ahead if ahead is not None else 'N/A'}"]
    if task.get("scan_file_remaining") == 0 and task["status"] == "running":
        lines.append("File scanning finished; post-processing/report generation may still be running.")
    if task.get("error_message"):
        lines.append("error=" + task["error_message"])
    return lines


class GpuMonitor:
    def __init__(self, client):
        self.client = client
        self.permanent_error = None
        self.endpoint = "/admin/resources/gpus"

    def sample(self):
        if self.permanent_error:
            return self.permanent_error
        old_timeout = self.client.timeout
        try:
            # Optional telemetry must not hold task status hostage to a slow metrics endpoint.
            self.client.timeout = min(old_timeout, 5)
            try:
                snapshot = self.client.json(self.endpoint)
            except CLIError as exc:
                if exc.context.get("http_status") != 404 or self.endpoint != "/admin/resources/gpus":
                    raise
                self.endpoint = "/admin/resources"
                snapshot = self.client.json(self.endpoint)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("gpus"), list):
                raise CLIError("Invalid GPU telemetry response")
            if not all(isinstance(device, dict) for device in snapshot["gpus"]):
                raise CLIError("Invalid GPU device telemetry")
            return {"captured_at": snapshot.get("captured_at"), "gpus": snapshot.get("gpus", [])}
        except (CLIError, OSError, http.client.HTTPException) as exc:
            result = {"error": str(exc), "gpus": [], "captured_at": None, "exit_code": getattr(exc, "code", 1)}
            if isinstance(exc, CLIError) and exc.context.get("http_status") in (401, 403, 404):
                if exc.context["http_status"] == 403:
                    result["error"] = "Admin account required for GPU resources; review monitoring continues"
                self.permanent_error = result
            return result
        finally:
            self.client.timeout = old_timeout


def watch_gpus(client, args):
    if args.samples is not None and (not args.watch or args.samples < 1):
        raise CLIError("--samples requires --watch and a positive integer", 2)
    monitor = GpuMonitor(client)
    display = LiveDisplay(sys.stderr)
    samples = 0
    while True:
        snapshot = monitor.sample()
        samples += 1
        if not args.watch:
            return snapshot, snapshot.get("exit_code", 1) if snapshot.get("error") else 0
        if args.json:
            print(json_text({"event": "gpu", **snapshot}), file=sys.stderr, flush=True)
        else:
            display.draw(gpu_lines(snapshot))
        if monitor.permanent_error or (args.samples is not None and samples >= args.samples):
            return snapshot, snapshot.get("exit_code", 1) if snapshot.get("error") else 0
        time.sleep(args.poll_interval)


def wait_for_task(client, task, args):
    start = time.monotonic()
    display = LiveDisplay(sys.stderr)
    monitor = GpuMonitor(client) if args.gpu else None
    while True:
        telemetry = monitor.sample() if monitor else None
        if args.json:
            print(json_text({"event": "progress", **{k: task.get(k) for k in (
                "id", "display_name", "status", "progress", "queued_ahead_count",
                "scan_file_total", "scan_file_completed", "scan_file_remaining")},
                **({"gpu": telemetry} if monitor else {})}), file=sys.stderr, flush=True)
        else:
            display.draw(task_lines(task) + (gpu_lines(telemetry) if monitor else []))
        if task["status"] in ("completed", "failed"):
            return task
        if task["status"] not in ("queued", "running"):
            raise CLIError("Unknown task status; stopped waiting", task_id=task["id"])
        remaining = None if args.wait_timeout is None else args.wait_timeout - (time.monotonic() - start)
        if remaining is not None and remaining <= 0:
            raise CLIError("Wait timed out; task continues on server", 5, task_id=task["id"])
        time.sleep(args.poll_interval if remaining is None else min(args.poll_interval, remaining))
        if remaining is not None:
            remaining = args.wait_timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise CLIError("Wait timed out; task continues on server", 5, task_id=task["id"])
        original_timeout = client.timeout
        try:
            if remaining is not None:
                client.timeout = min(original_timeout, remaining)
            task = client.json("/reviews/" + identifier(task["id"]))
        except CLIError as exc:
            exc.context.setdefault("task_id", task["id"])
            raise
        finally:
            client.timeout = original_timeout


def download_report(client, args):
    formats = {".pdf": "pdf", ".md": "markdown", ".txt": "text", ".json": "json"}
    fmt = args.format or (formats.get(args.output.suffix.lower()) if args.output else "json" if args.json else "markdown")
    if fmt is None:
        raise CLIError("Unknown output extension; specify --format", 2)
    if fmt == "pdf" and args.output is None:
        raise CLIError("PDF requires --output", 2)
    if args.output and args.output.exists() and not args.force:
        raise CLIError("Output exists; use --force to replace it", 2)
    task = client.json("/reviews/" + identifier(args.task_id))
    report_id = task.get("report_id")
    if not report_id:
        raise CLIError("Report not available yet", 4 if task.get("status") == "failed" else 1,
                       task_id=args.task_id, status=task.get("status"))
    path = "/reports/" + identifier(report_id)
    if fmt != "json":
        path += "/" + fmt
    with client.open(path) as response:
        expected = {"pdf": ("application/pdf",), "markdown": ("text/markdown", "text/plain"),
                    "text": ("text/plain",), "json": ("application/json",)}
        if response.headers.get_content_type() not in expected[fmt]:
            raise CLIError("Unexpected report content type; output not written")
        first = response.read(5)
        if fmt == "pdf" and first != b"%PDF-":
            raise CLIError("Response is not a PDF; output not written")
        if args.output:
            with atomic_output(args.output, args.force) as output:
                output.write(first)
                shutil.copyfileobj(response, output)
                expected_length = response.headers.get("Content-Length")
                if expected_length is not None and output.tell() != int(expected_length):
                    raise CLIError("Incomplete report download; output not written")
            return {"task_id": args.task_id, "report_id": report_id, "format": fmt,
                    "output": str(args.output.absolute()), "bytes": args.output.stat().st_size}
        content = (first + response.read()).decode("utf-8")
        return json.loads(content) if fmt == "json" else {"task_id": args.task_id, "format": fmt, "content": content}


def run(args):
    if args.command == "checks":
        return {"checks": list(CHECKS)}, 0
    path = config_path(args)
    config = load_config(path)
    server = server_url(getattr(args, "server", None) or os.environ.get("CCHECK_REVIEW_SERVER")
                        or config.get("server") or "http://127.0.0.1:8800")
    profiles = config.get("profiles", {})
    profile = profiles.get(server, {})
    if not isinstance(profile, dict):
        raise CLIError("Invalid server profile in configuration", 2)
    if args.command == "logout":
        profiles.pop(server, None)
        config["profiles"] = profiles
        save_config(path, config)
        return {"server": server, "logged_out": True}, 0
    insecure = getattr(args, "allow_insecure_http", False) or profile.get("allow_insecure_http") is True
    require_secure_transport(server, insecure)
    token = os.environ.get("CCHECK_REVIEW_TOKEN") or profile.get("token")
    client = Client(server, token, getattr(args, "timeout", 60))
    if args.command == "login":
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
        if not password:
            raise CLIError("Password cannot be empty", 2)
        result = client.json("/auth/login", payload={"username": args.username, "password": password}, authenticated=False)
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise CLIError("Login response did not include an access token")
        profiles[server] = {"token": token, "allow_insecure_http": insecure}
        config.update(server=server, profiles=profiles)
        save_config(path, config)
        return {"server": server, "username": args.username, "logged_in": True}, 0
    if args.command == "models":
        return client.json("/models"), 0
    if args.command == "gpu":
        return watch_gpus(client, args)
    if args.command == "list":
        if not 1 <= args.limit <= 100 or args.offset < 0:
            raise CLIError("--limit must be 1..100 and --offset must be >= 0", 2)
        query = {"limit": args.limit, "offset": args.offset}
        if args.name:
            query["keyword"] = args.name
        return client.json("/reviews?" + urllib.parse.urlencode(query)), 0
    if args.command == "report":
        return download_report(client, args), 0
    if args.command == "submit":
        if not args.name.strip() or len(args.name) > 128 or any(ord(c) < 32 for c in args.name):
            raise CLIError("--name must contain 1..128 characters, without control characters", 2)
        files = source_files(args.dir)
        models = client.json("/models")
        candidates = [m for m in models if m.get("is_enabled") and
                      (m.get("id") == args.model if args.model else m.get("is_default"))]
        if len(candidates) != 1:
            raise CLIError("No unique accessible default/matching model; use models and --model NODE_ID", 2)
        fields = {"model_node_id": candidates[0]["id"], "display_name": args.name.strip(),
                  "check_types": json_text(args.checks)}
        with multipart_archive(files, fields) as (body, headers):
            try:
                with client.open("/reviews/archive", data=body, headers=headers) as response:
                    task = read_json(response)
            except (CLIError, OSError, http.client.HTTPException) as exc:
                # A server may have committed the task before the response was lost.
                raise CLIError("Submit was not confirmed; it was NOT retried. Check 'list --name' before resubmitting. "
                               + str(exc), getattr(exc, "code", 1), task_name=args.name) from exc
    else:
        task = client.json("/reviews/" + identifier(args.task_id))
    if args.wait:
        task = wait_for_task(client, task, args)
    elif args.gpu:
        task["gpu"] = GpuMonitor(client).sample()
    return task, 4 if task.get("status") == "failed" else 0


def emit(result, args):
    if args.json:
        print(json_text(result))
    elif args.command in ("submit", "status"):
        if sys.stdout.isatty() and not args.wait:
            LiveDisplay(sys.stdout).draw(task_lines(result) + (gpu_lines(result["gpu"]) if "gpu" in result else []))
        else:
            print(task_summary(result))
            if "gpu" in result:
                print("\n".join(safe_text(line) for line in gpu_lines(result["gpu"])))
    elif args.command == "gpu":
        LiveDisplay(sys.stdout).draw(gpu_lines(result))
    elif args.command == "report" and "content" in result:
        print(safe_multiline(result["content"]) if sys.stdout.isatty() else result["content"], end="\n")
    elif args.command == "list":
        for task in result.get("items", []):
            print(task_summary(task))
        print(f"total={result.get('total', 0)}")
    elif args.command == "models":
        for model in result:
            print(safe_text(f"{model['id']}  {model['display_name']}  default={model.get('is_default', False)}"))
    elif args.command == "checks":
        print("\n".join(result["checks"]))
    else:
        print(safe_text(json_text(result)))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    use_json = "--json" in argv
    try:
        args = parser().parse_args(argv)
        if args.command == "help":
            target = args.help_commands[args.topic] if args.topic else args.help_root
            target.print_help()
            return 0
        args.json = getattr(args, "json", False)
        result, code = run(args)
        emit(result, args)
        return code
    except KeyboardInterrupt:
        error = CLIError("Interrupted; any submitted task continues on the server", 130)
    except CLIError as exc:
        error = exc
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
        # Never include raw API data or secret-bearing request objects in diagnostics.
        error = CLIError(f"Local I/O or unexpected API/configuration data ({type(exc).__name__})")
    if use_json:
        print(json_text({"error": str(error), "exit_code": error.code, **error.context}), file=sys.stderr)
    else:
        print("Error: " + safe_text(str(error)), file=sys.stderr)
    return error.code


if __name__ == "__main__":
    raise SystemExit(main())
