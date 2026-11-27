"""Desktop probe diagnostics and last confirmed login projection."""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from time import monotonic

from cli_manager import backend

LAST_AUTH = "desktop_last_auth"
TRACE = "desktop_probe_trace"


def confirmed_auth(row):
    if not row or row.get("auth_busy") or row.get("deleting"):
        return None
    status = row.get("status") or {}
    checked = row.get("probed_at")
    if row.get("probe_error") or not checked or status.get("schema") != backend.PROBE_SCHEMA:
        return None
    if not status.get("logged_in"):
        login = "expired" if row.get("bound_account") else "signed_out"
    elif status.get("rebind_required"):
        login = "rebind_required"
    elif row.get("bound_account") and row["bound_account"] == status.get("account"):
        login = "signed_in"
    else:
        login = "unbound"
    return {"login": login, "checked_at": checked, "generation": row["generation"]}


def last_auth(row):
    if actual := confirmed_auth(row):
        return actual
    cached = row.get(LAST_AUTH)
    if isinstance(cached, dict) and cached.get("generation") == row["generation"]:
        return cached
    return None


def preserve(previous, row):
    historical = confirmed_auth(row)
    if historical is None and previous and (
            previous["generation"], previous.get("bound_account")) == (
            row["generation"], row.get("bound_account")):
        historical = last_auth(previous)
    if historical is None:
        row.pop(LAST_AUTH, None)
    else:
        row[LAST_AUTH] = historical
    if not row.get("probe_error") or not previous or (
            previous["generation"], previous.get("probed_at")) != (row["generation"], row.get("probed_at")):
        row.pop(TRACE, None)


def reason(error, stderr=b"", exit_code=None):
    if isinstance(error, subprocess.TimeoutExpired):
        return "subprocess_timeout"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "executable_missing"
    text = stderr.lower()
    if any(fragment in text for fragment in (b"operation not permitted", b"permission denied", b"sandbox denied",
                                              b"read-only file system")):
        return "permission_denied"
    if any(fragment in text for fragment in (b"could not resolve", b"network is unreachable",
                                              b"failed to lookup address", b"connection refused")):
        return "network_unavailable"
    if exit_code is not None and exit_code != 0:
        return "process_exited"
    if isinstance(error, RuntimeError) and "没有响应" in str(error):
        return "rpc_timeout"
    if isinstance(error, (ValueError, KeyError)):
        return "invalid_response"
    return "provider_error"


def stderr_tail(app):
    try:
        with (app.run_dir / "stderr.log").open("rb") as stream:
            stream.seek(max(0, (app.run_dir / "stderr.log").stat().st_size - 16_384))
            return stream.read(16_384)
    except OSError:
        return b""


class Observation:
    def __init__(self):
        self.started = monotonic()
        self.failure = None

    def record(self, stage, error, stderr=b"", exit_code=None):
        self.failure = {"stage": stage, "reason": reason(error, stderr, exit_code)}

    def result(self, checked_at):
        details = self.failure or {"stage": "probe", "reason": "provider_error"}
        return {**details, "checked_at": checked_at, "duration_ms": round((monotonic() - self.started) * 1000)}


@contextmanager
def observed_probe():
    """Use the frozen probe verbatim; only retain allowlisted failure diagnostics."""
    observation = Observation()
    saved_invoke, saved_class, saved_probe = backend.invoke, backend.app_class, backend.probe

    def invoke(instance, args, env, cwd, *, timeout=15):
        stage = {("--version",): "version", ("auth", "status", "--json"): "auth_status",
                 ("features", "list"): "features"}.get(tuple(args), "quota")
        try:
            result = saved_invoke(instance, args, env, cwd, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            observation.record(stage, error)
            raise
        if result.returncode:
            observation.record(stage, None, result.stderr.encode("utf-8", errors="replace")[-16_384:],
                               result.returncode)
        if stage == "auth_status":
            try:
                auth = json.loads(result.stdout)
                if not isinstance(auth, dict) or type(auth.get("loggedIn")) is not bool:
                    raise ValueError("invalid auth status")
            except ValueError as error:
                observation.record(stage, error)
                raise
        return result

    def app_class():
        cls = saved_class()

        class ObservedAppServer(cls):
            def request(self, method, params, timeout=120):
                try:
                    value = super().request(method, params, timeout)
                    if method == "account/read":
                        if not isinstance(value, dict) or "account" not in value:
                            raise ValueError("missing account field")
                        account = value["account"]
                        if account is not None and (not isinstance(account, dict) or not isinstance(account.get("type"), str)):
                            raise ValueError("invalid account field")
                        if isinstance(account, dict) and account["type"] == "chatgpt" and not backend.codex_identity(value)[0]:
                            raise ValueError("incomplete ChatGPT account")
                    return value
                except (RuntimeError, ValueError, OSError) as error:
                    stage = {"initialize": "initialize", "account/read": "account", "model/list": "models",
                             "account/rateLimits/read": "quota"}.get(method, "rpc")
                    observation.record(stage, error, stderr_tail(self), self.proc.poll())
                    raise

        return ObservedAppServer

    def probe(*args, **kwargs):
        try:
            return saved_probe(*args, **kwargs)
        except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
            if observation.failure is None:
                observation.record("probe", error)
            raise

    backend.invoke, backend.app_class, backend.probe = invoke, app_class, probe
    try:
        yield observation
    finally:
        backend.invoke, backend.app_class, backend.probe = saved_invoke, saved_class, saved_probe
