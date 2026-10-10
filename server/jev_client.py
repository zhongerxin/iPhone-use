"""Bounded, persistent TypeSafe Jev transport for structured decisions.

The endpoint is deliberately fixed: phone state and credentials are sent only to
the official TypeSafe HTTPS API. A model request never executes a phone action.
"""
import http.client
import json
import math
import os
from pathlib import Path
import socket
import stat
import threading
import time

from wda_client import WDAError


HOST = "api.typesafe.ai"
PATH = "/v1/systemone"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
MAX_CREDENTIAL_BYTES = 16 * 1024


def _error(code, message, **details):
    return WDAError(code, message, details={"action_executed": False, **details})


def _finite_probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_choice(answer, criteria):
    """Validate the official Choice contract against exactly the offered keys."""
    if not isinstance(answer, dict) or not isinstance(criteria, dict) or not criteria:
        raise _error("jev_invalid_response", "Jev returned an invalid choice; no phone action executed.")
    probabilities = answer.get("probabilities")
    choice = answer.get("choice")
    valid = (
        answer.get("type") == "choice"
        and isinstance(choice, str) and choice in criteria
        and isinstance(probabilities, dict) and set(probabilities) == set(criteria)
        and _finite_probability(answer.get("confidence"))
        and all(_finite_probability(value) for value in probabilities.values())
    )
    if valid:
        valid = (abs(sum(probabilities.values()) - 1) < 0.02
                 and probabilities[choice] >= max(probabilities.values()) - 1e-6)
    if not valid:
        raise _error("jev_invalid_response", "Jev returned an invalid choice; no phone action executed.")
    return {"type": "choice", "choice": choice, "probabilities": dict(probabilities),
            "confidence": answer["confidence"]}


def selected_answers(result, questions):
    """Use only the target head selected by operation; discard speculative heads."""
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise _error("jev_invalid_response", "Jev returned no valid answers; no phone action executed.")
    answers = result["answers"]
    if "operation" not in questions:
        return {name: validate_choice(answers.get(name), question["criteria"])
                for name, question in questions.items()}
    operation = validate_choice(answers.get("operation"), questions["operation"]["criteria"])
    selected = {"operation": operation}
    target_name = operation["choice"].lower() + "_target"
    if target_name in questions:
        selected[target_name] = validate_choice(answers.get(target_name), questions[target_name]["criteria"])
    return selected


def _valid_key(value):
    return (isinstance(value, str) and bool(value.strip()) and value.strip().isascii()
            and all(33 <= ord(char) <= 126 for char in value.strip()))


def load_api_key(state_dir=None, api_key=None):
    """Environment wins; otherwise read a private local jev.json configuration."""
    environment = os.environ.get("TYPESAFE_API_KEY")
    if environment is not None and environment.strip():
        value = environment
    elif api_key is not None:
        value = api_key
    else:
        if state_dir is None:
            from wda_setup import state_directory
            state_dir = state_directory()
        path = Path(state_dir).expanduser() / "jev.json"
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_size > MAX_CREDENTIAL_BYTES):
                raise _error("jev_configuration_invalid", "Jev configuration must be an owner-only 0600 file.")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                raw = source.read(MAX_CREDENTIAL_BYTES + 1)
            if len(raw) > MAX_CREDENTIAL_BYTES:
                raise ValueError()
            configuration = json.loads(raw)
            value = configuration.get("api_key") if isinstance(configuration, dict) else None
        except FileNotFoundError:
            raise _error("jev_not_configured", "Configure TYPESAFE_API_KEY or private jev.json before using Jev.") from None
        except WDAError:
            raise
        except (OSError, ValueError, TypeError):
            raise _error("jev_configuration_invalid", "Jev configuration could not be read securely.") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
    if not _valid_key(value):
        raise _error("jev_configuration_invalid", "Jev API key is missing or invalid.")
    return value.strip()


class JevClient:
    """Persistent HTTPS; reconnect a stale transport once within the same budget."""

    def __init__(self, state_dir=None, api_key=None, model=None, timeout=8.0):
        self.state_dir = state_dir
        self._api_key = api_key
        self.model = os.environ.get("TYPESAFE_MODEL") or model or "jev-latest"
        self.timeout = timeout
        self.connection = None
        self._lock = threading.Lock()

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _error("jev_timeout", "Jev decision deadline expired; no phone action executed.")
        return remaining

    def classify(self, state, questions, timeout=None):
        """Return {model, answers, usage, latency_ms}, validating relevant Choices."""
        budget = self.timeout if timeout is None else timeout
        if (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0 or budget > 60
                or not isinstance(self.model, str) or not self.model.strip()
                or state is None or not isinstance(questions, dict) or not questions):
            raise _error("jev_invalid_request", "Jev needs state, choice questions and a timeout from 0 to 60 seconds.")
        for name, question in questions.items():
            if (not isinstance(name, str) or not name or not isinstance(question, dict)
                    or question.get("type") != "choice" or "instructions" not in question
                    or not isinstance(question.get("criteria"), dict)
                    or not 1 <= len(question["criteria"]) <= 255
                    or any(not isinstance(key, str) or not key for key in question["criteria"])):
                raise _error("jev_invalid_request", "Each Jev choice needs instructions and 1 to 255 named options.")
        try:
            body = json.dumps({"model": self.model, "state": state, "questions": questions},
                              ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            raise _error("jev_invalid_request", "Jev request must contain finite JSON data.") from None
        if len(body) > MAX_REQUEST_BYTES:
            raise _error("jev_invalid_request", "Jev request is too large; send a smaller structured phone state.")
        key = load_api_key(self.state_dir, self._api_key)
        started = time.monotonic()
        deadline = started + budget
        if not self._lock.acquire(timeout=self._remaining(deadline)):
            raise _error("jev_timeout", "Jev decision deadline expired; no phone action executed.")
        try:
            requests = 1
            try:
                result = self._request(body, key, deadline)
            except WDAError as error:
                # Classification does not mutate the phone. The provider may
                # close a pooled HTTPS socket without advertising it, so one
                # fresh connection is safe. HTTP/validation failures never
                # retry, and the second attempt keeps the original deadline.
                if error.code not in ("jev_unavailable", "jev_timeout") or time.monotonic() >= deadline:
                    raise
                self.close()
                requests = 2
                result = self._request(body, key, deadline)
            if (not isinstance(result, dict) or not isinstance(result.get("model"), str)
                    or not result["model"] or not isinstance(result.get("usage"), dict)):
                raise _error("jev_invalid_response", "Jev returned an invalid response; no phone action executed.")
            answers = selected_answers(result, questions)
            usage = {name: value for name, value in result["usage"].items()
                     if name in ("input_tokens", "output_tokens") and type(value) is int and value >= 0}
            return {"model": result["model"], "answers": answers, "usage": usage,
                    "http_requests": requests,
                    "latency_ms": round((time.monotonic() - started) * 1000, 3)}
        except WDAError:
            self.close()
            raise
        finally:
            self._lock.release()

    def _request(self, body, key, deadline):
        try:
            remaining = self._remaining(deadline)
            if self.connection is None:
                self.connection = http.client.HTTPSConnection(HOST, timeout=remaining)
            self.connection.timeout = remaining
            if self.connection.sock:
                self.connection.sock.settimeout(remaining)
            self.connection.request("POST", PATH, body,
                                    {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                     "Accept": "application/json"})
            network_socket = self.connection.sock
            if network_socket:
                network_socket.settimeout(self._remaining(deadline))
            response = self.connection.getresponse()
            if response.status != 200:
                codes = {401: "jev_authentication", 403: "jev_authentication", 429: "jev_rate_limited",
                         529: "jev_overloaded", 503: "jev_overloaded"}
                raise _error(codes.get(response.status, "jev_http_error"),
                             f"Jev returned HTTP {response.status}; no phone action executed.",
                             http_status=response.status)
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    valid_length = 0 <= int(declared) <= MAX_RESPONSE_BYTES
                except (TypeError, ValueError):
                    valid_length = False
                if not valid_length:
                    raise _error("jev_invalid_response", "Jev response exceeds the permitted size.")
            chunks, size = [], 0
            while not response.isclosed():
                remaining = self._remaining(deadline)
                if network_socket:
                    network_socket.settimeout(remaining)
                chunk = response.read1(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _error("jev_invalid_response", "Jev response exceeds the permitted size.")
            self._remaining(deadline)
            try:
                return json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise _error("jev_invalid_response", "Jev returned invalid JSON; no phone action executed.") from None
        except (socket.timeout, TimeoutError):
            raise _error("jev_timeout", "Jev decision timed out; no phone action executed.") from None
        except (OSError, http.client.HTTPException):
            raise _error("jev_unavailable", "Jev connection failed; no phone action executed.") from None
