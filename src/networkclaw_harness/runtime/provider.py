"""Provider routing, bounded recovery, and safe attempt accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import random
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence


class ProviderErrorClass(StrEnum):
    CREDENTIAL = "credential"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    PROTOCOL = "protocol"
    CONTEXT_OVERFLOW = "context_overflow"
    CONTENT_POLICY = "content_policy"
    LOCAL_VALIDATION = "local_validation"
    EMPTY_RESPONSE = "empty_response"
    STREAM_INTERRUPTED = "stream_interrupted"
    UNKNOWN = "unknown"


class RetryDecision(StrEnum):
    SUCCEEDED = "succeeded"
    RETRY = "retry"
    REFRESH_CREDENTIAL = "refresh_credential"
    FALLBACK = "fallback"
    RESTART_WITH_COMPRESSION = "restart_with_compression"
    TERMINAL = "terminal"


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 provider_window: int | None = None,
                 available_output_tokens: int | None = None,
                 compression_restart_allowed: bool = False,
                 exhausted_reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider_window = provider_window
        self.available_output_tokens = available_output_tokens
        self.compression_restart_allowed = compression_restart_allowed
        self.exhausted_reason = exhausted_reason


class ProviderRateLimited(ProviderError):
    def __init__(self, message: str = "provider rate limit reached") -> None:
        super().__init__("rate_limited", message, retryable=True)


class ProviderTimedOut(ProviderError):
    def __init__(self, message: str = "provider request timed out") -> None:
        super().__init__("provider_timeout", message, retryable=True)


@dataclass(frozen=True, slots=True)
class ProviderConfigRef:
    """Host-owned configuration reference; credential material is never represented here."""
    provider_id: str
    model_id: str
    config_ref: str
    timeout_ms: int
    max_attempts: int = 2
    config_version: str = ""

    def __post_init__(self) -> None:
        if any(not value or not value.isascii() for value in (self.provider_id, self.model_id, self.config_ref)):
            raise ValueError("provider, model, and config references must be non-empty ASCII strings")
        if not self.config_version.isascii() or self.timeout_ms <= 0 or self.max_attempts <= 0:
            raise ValueError("provider config or limits are invalid")

    @property
    def client_key(self) -> tuple[str, str, str]:
        return self.provider_id, self.config_ref, self.config_version

    @property
    def route_key(self) -> tuple[str, str]:
        return self.provider_id, self.model_id


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    session_id: str
    system_prompt: bytes
    messages: tuple[Mapping[str, Any], ...]
    prompt_cache_key: str
    max_output_tokens: int
    request_id: str = ""
    workspace_snapshot_hash: str = ""
    policy_snapshot_hash: str = ""
    budget_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        if not self.session_id or self.max_output_tokens <= 0:
            raise ValueError("provider request requires a session and positive output budget")
        if any(not value.isascii() for value in (self.request_id, self.workspace_snapshot_hash,
                                                  self.policy_snapshot_hash, self.budget_snapshot_hash)):
            raise ValueError("provider request identities must be ASCII")

    @property
    def stable_request_id(self) -> str:
        if self.request_id:
            return self.request_id
        return f"provider-{hashlib.sha256(f'{self.session_id}:{self.prompt_cache_key}'.encode()).hexdigest()[:20]}"

    @property
    def contract_hash(self) -> str:
        return _hash_json({"workspace": self.workspace_snapshot_hash, "policy": self.policy_snapshot_hash,
                           "budget": self.budget_snapshot_hash, "prompt": self.prompt_cache_key,
                           "max_output_tokens": self.max_output_tokens})


@dataclass(frozen=True, slots=True)
class ProviderChunk:
    delta: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    sequence: int | None = None
    tool_call: Mapping[str, Any] | None = None
    reasoning_delta: str = ""
    refusal: str = ""

    def __post_init__(self) -> None:
        if self.sequence is not None and self.sequence <= 0:
            raise ValueError("provider chunk sequence must be positive")
        object.__setattr__(self, "usage", MappingProxyType(_safe_usage(self.usage)))
        if self.tool_call is not None:
            object.__setattr__(self, "tool_call", MappingProxyType(dict(self.tool_call)))


@dataclass(frozen=True, slots=True)
class OverflowVerdict:
    provider_reported_window: int | None
    available_output_tokens: int
    compression_restart_allowed: bool
    exhausted_reason: str

    def __post_init__(self) -> None:
        if self.provider_reported_window is not None and self.provider_reported_window <= 0:
            raise ValueError("provider window must be positive")
        if self.available_output_tokens < 0 or not self.exhausted_reason:
            raise ValueError("overflow verdict is invalid")


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider_id: str
    model_id: str
    attempt: int
    outcome: str
    emitted_chars: int = 0
    request_emitted: bool = False
    token_emitted: bool = False
    restart: bool = False
    request_id: str = ""
    attempt_id: str = ""
    route_index: int = 0
    route_snapshot_hash: str = ""
    config_snapshot_hash: str = ""
    latency_ms: float = 0.0
    usage: Mapping[str, int] = field(default_factory=dict)
    error_class: str | None = None
    retry_decision: str = RetryDecision.TERMINAL
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if self.attempt <= 0 or self.route_index < 0 or self.latency_ms < 0:
            raise ValueError("provider attempt counters are invalid")
        object.__setattr__(self, "usage", MappingProxyType(_safe_usage(self.usage)))

    def public_payload(self) -> Mapping[str, Any]:
        return MappingProxyType({"request_id": self.request_id, "attempt_id": self.attempt_id,
            "attempt": self.attempt, "route_index": self.route_index, "provider_id": self.provider_id,
            "model_id": self.model_id, "route_snapshot_hash": self.route_snapshot_hash,
            "config_snapshot_hash": self.config_snapshot_hash, "latency_ms": round(self.latency_ms, 3),
            "usage": dict(self.usage), "outcome": self.outcome, "error_class": self.error_class,
            "retry_decision": self.retry_decision, "request_emitted": self.request_emitted,
            "token_emitted": self.token_emitted, "restart": self.restart, "dedupe_key": self.dedupe_key})

    def semantic_fact(self):
        """Build the durable safe fact without importing model types at module load time."""
        from .models import FactKind, SemanticFact
        return SemanticFact(FactKind.PROVIDER_ATTEMPT, self.attempt_id, self.outcome, self.public_payload())


@dataclass(frozen=True, slots=True)
class RouteSnapshot:
    routes: tuple[tuple[str, str, str], ...]
    snapshot_hash: str
    config_snapshot_hash: str = ""
    contract_hash: str = ""

    @classmethod
    def from_configs(cls, configs: Sequence[ProviderConfigRef], *, contract_hash: str = "",
                     allowed_routes: Sequence[tuple[str, str]] | None = None) -> "RouteSnapshot":
        if not configs:
            raise ValueError("provider route chain cannot be empty")
        allowlist = frozenset(allowed_routes or (config.route_key for config in configs))
        if any(config.route_key not in allowlist for config in configs):
            raise ProviderError("provider_route_not_allowed", "provider route is not allowlisted")
        routes = tuple((c.provider_id, c.model_id, c.config_ref) for c in configs)
        if len(set(routes)) != len(routes):
            raise ValueError("provider route chain contains duplicates")
        config_hash = _hash_json(tuple((c.provider_id, c.model_id, c.config_ref, c.config_version,
                                         c.timeout_ms, c.max_attempts) for c in configs))
        return cls(routes, _hash_json({"routes": routes, "config": config_hash, "contract": contract_hash}),
                   config_hash, contract_hash)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_total_attempts: int = 6
    base_backoff_seconds: float = 0.25
    max_backoff_seconds: float = 4.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_total_attempts <= 0 or self.base_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("retry limits and delays must be non-negative")
        if self.base_backoff_seconds > self.max_backoff_seconds or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("retry backoff or jitter is invalid")

    def delay(self, retry_number: int, random_value: float) -> float:
        base = min(self.base_backoff_seconds * (2 ** max(retry_number - 1, 0)), self.max_backoff_seconds)
        return max(0.0, base * (1 + self.jitter_ratio * ((2 * random_value) - 1)))


@dataclass(frozen=True, slots=True)
class ProviderResult:
    config: ProviderConfigRef
    chunks: tuple[ProviderChunk, ...]
    usage: Mapping[str, int]
    attempts: int
    route_snapshot: RouteSnapshot | None = None
    attempt_ledger: tuple[ProviderAttempt, ...] = ()
    finish_reason: str | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    delivery_sequence: int = 0

    @property
    def text(self) -> str:
        return "".join(chunk.delta for chunk in self.chunks)


class ProviderClient(Protocol):
    def stream(self, request: ProviderRequest, *, model_id: str, timeout_ms: int) -> Iterator[ProviderChunk]: ...


class ProviderClientResolver(Protocol):
    def resolve(self, config: ProviderConfigRef) -> ProviderClient: ...


class ProviderRuntime:
    def __init__(self, resolver: ProviderClientResolver, *, retry_policy: RetryPolicy | None = None,
                 sleep: Callable[[float], None] = time.sleep, random_source: Callable[[], float] = random.random,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self._resolver, self._retry_policy, self._sleep, self._random, self._clock = resolver, retry_policy or RetryPolicy(), sleep, random_source, clock
        self._clients: dict[tuple[str, str, str], ProviderClient] = {}
        self._lock = threading.RLock()

    def invoke(self, request: ProviderRequest, *, primary: ProviderConfigRef, fallbacks: Sequence[ProviderConfigRef] = (),
               allowed_routes: Sequence[tuple[str, str]] | None = None, on_chunk: Callable[[ProviderChunk], None] | None = None,
               attempt_sink: Callable[[ProviderAttempt], None] | None = None, iteration_receipt: Any | None = None) -> ProviderResult:
        configs = (primary, *fallbacks)
        snapshot = RouteSnapshot.from_configs(configs, contract_hash=request.contract_hash, allowed_routes=allowed_routes)
        ledger: list[ProviderAttempt] = []
        failures: list[ProviderError] = []
        total = 0
        refreshed: set[int] = set()
        request_id = request.stable_request_id
        for route_index, config in enumerate(configs):
            for attempt in range(1, config.max_attempts + 1):
                if total >= self._retry_policy.max_total_attempts:
                    break
                total += 1
                from .streaming import BoundedStreamAssembler
                started, chunks, request_emitted, token_emitted = self._clock(), [], False, False
                assembler = BoundedStreamAssembler(max_text_bytes=max(request.max_output_tokens * 16, 4096))
                try:
                    client = self._client(config)
                    if iteration_receipt is not None: iteration_receipt.request_emitted()
                    request_emitted = True
                    for chunk in client.stream(request, model_id=config.model_id, timeout_ms=config.timeout_ms):
                        chunk = assembler.accept(chunk)
                        chunks.append(chunk); token_emitted = token_emitted or bool(chunk.delta)
                        if chunk.delta and iteration_receipt is not None: iteration_receipt.token_emitted()
                        if on_chunk is not None: on_chunk(chunk)
                    assembled = assembler.finish()
                    if assembled.invalid_reason:
                        raise ProviderError("provider_response_invalid", "provider stream shape was invalid")
                    if assembled.empty:
                        raise ProviderError("empty_response", "provider returned an empty response", retryable=True)
                    receipt = self._attempt(request_id, snapshot, config, route_index, attempt, "succeeded", RetryDecision.SUCCEEDED,
                                            started, chunks, request_emitted, token_emitted)
                    _record_attempt(ledger, receipt, attempt_sink)
                    if iteration_receipt is not None: iteration_receipt.commit()
                    return ProviderResult(config, tuple(chunks), assembled.usage, attempt, snapshot, tuple(ledger),
                                          assembled.finish_reason, assembled.tool_calls, assembled.last_sequence)
                except TimeoutError as error:
                    error_obj = ProviderTimedOut(); error_obj.__cause__ = error
                except Exception as error:
                    from .streaming import StreamAssemblyError
                    if isinstance(error, StreamAssemblyError):
                        error_obj = ProviderError("provider_response_invalid", "provider stream shape was invalid")
                        error_obj.__cause__ = error
                    elif isinstance(error, ProviderError):
                        error_obj = error
                    else:
                        error_obj = ProviderError("provider_network_error", "provider request failed", retryable=True)
                        error_obj.__cause__ = error
                error_class = classify_provider_error(error_obj)
                if token_emitted:
                    error_obj = ProviderError("provider_stream_interrupted", "provider stream ended after visible output")
                    error_class = ProviderErrorClass.STREAM_INTERRUPTED
                can_retry = attempt < config.max_attempts and total < self._retry_policy.max_total_attempts
                has_fallback = route_index + 1 < len(configs) and total < self._retry_policy.max_total_attempts
                refresh_available = route_index not in refreshed and callable(getattr(self._resolver, "refresh", None))
                decision = self._decision(error_class, can_retry, has_fallback, refresh_available)
                if decision is RetryDecision.REFRESH_CREDENTIAL:
                    refreshed.add(route_index)
                    if bool(self._resolver.refresh(config)): self._drop_client(config)
                    else: decision = RetryDecision.FALLBACK if has_fallback else RetryDecision.TERMINAL
                receipt = self._attempt(request_id, snapshot, config, route_index, attempt, error_obj.code, decision,
                                        started, chunks, request_emitted, token_emitted, error_class)
                _record_attempt(ledger, receipt, attempt_sink); failures.append(error_obj)
                if decision in {RetryDecision.RETRY, RetryDecision.REFRESH_CREDENTIAL}:
                    self._backoff(total); continue
                if decision is RetryDecision.FALLBACK: break
                if iteration_receipt is not None: iteration_receipt.refund()
                if error_class is ProviderErrorClass.CONTEXT_OVERFLOW:
                    raise ProviderRecoveryError(error_obj.code, "provider context requires a bounded restart",
                        attempts=tuple(ledger), route_snapshot=snapshot,
                        overflow=overflow_verdict(error_obj, requested_output_tokens=request.max_output_tokens)) from error_obj
                terminal_code = "provider_unavailable" if error_class is ProviderErrorClass.UNKNOWN else error_obj.code
                raise ProviderRecoveryError(terminal_code, "provider recovery stopped", attempts=tuple(ledger), route_snapshot=snapshot) from error_obj
        if iteration_receipt is not None: iteration_receipt.refund()
        raise ProviderRecoveryError("provider_unavailable", "all configured providers failed", attempts=tuple(ledger), route_snapshot=snapshot) from (failures[-1] if failures else None)

    def _decision(self, kind: ProviderErrorClass, can_retry: bool, has_fallback: bool, refresh_available: bool) -> RetryDecision:
        if kind is ProviderErrorClass.CREDENTIAL:
            return RetryDecision.REFRESH_CREDENTIAL if refresh_available and can_retry else (RetryDecision.FALLBACK if has_fallback else RetryDecision.TERMINAL)
        if kind in {ProviderErrorClass.RATE_LIMIT, ProviderErrorClass.TIMEOUT, ProviderErrorClass.TRANSIENT, ProviderErrorClass.EMPTY_RESPONSE}:
            return RetryDecision.RETRY if can_retry else (RetryDecision.FALLBACK if has_fallback else RetryDecision.TERMINAL)
        if kind is ProviderErrorClass.CONTEXT_OVERFLOW: return RetryDecision.RESTART_WITH_COMPRESSION
        if kind is ProviderErrorClass.PROTOCOL: return RetryDecision.FALLBACK if has_fallback else RetryDecision.TERMINAL
        return RetryDecision.TERMINAL

    def _client(self, config: ProviderConfigRef) -> ProviderClient:
        with self._lock:
            if config.client_key not in self._clients: self._clients[config.client_key] = self._resolver.resolve(config)
            return self._clients[config.client_key]

    def _drop_client(self, config: ProviderConfigRef) -> None:
        with self._lock: self._clients.pop(config.client_key, None)

    def _backoff(self, number: int) -> None:
        delay = self._retry_policy.delay(number, self._random())
        if delay: self._sleep(delay)

    def _attempt(self, request_id, snapshot, config, route_index, attempt, outcome, decision, started, chunks, request_emitted, token_emitted, error_class=None):
        attempt_id = f"{request_id}:r{route_index + 1}:a{attempt}"
        return ProviderAttempt(config.provider_id, config.model_id, attempt, outcome, len("".join(c.delta for c in chunks)), request_emitted, token_emitted,
            decision is RetryDecision.RESTART_WITH_COMPRESSION, request_id, attempt_id, route_index, snapshot.snapshot_hash,
            snapshot.config_snapshot_hash, max(0.0, (self._clock() - started) * 1000), _usage(chunks),
            error_class.value if error_class else None, decision.value, f"provider-attempt:{attempt_id}")

    @property
    def client_count(self) -> int:
        with self._lock: return len(self._clients)


class ProviderRecoveryError(ProviderError):
    def __init__(self, code: str, message: str, *, attempts: tuple[ProviderAttempt, ...], route_snapshot: RouteSnapshot,
                 overflow: OverflowVerdict | None = None) -> None:
        super().__init__(code, message, retryable=False); self.attempts, self.route_snapshot, self.overflow = attempts, route_snapshot, overflow


def classify_provider_error(error: BaseException) -> ProviderErrorClass:
    code = str(getattr(error, "code", "provider_unavailable")).lower()
    if code in {"unauthorized", "provider_unauthorized", "forbidden", "provider_forbidden", "provider_credentials_missing"}: return ProviderErrorClass.CREDENTIAL
    if code in {"rate_limited", "provider_rate_limited"}: return ProviderErrorClass.RATE_LIMIT
    if code in {"provider_timeout", "timeout", "request_timeout"}: return ProviderErrorClass.TIMEOUT
    if code in {"provider_unavailable", "provider_network_error", "network_error", "provider_server_error", "server_error"}: return ProviderErrorClass.TRANSIENT
    if code in {"provider_response_invalid", "provider_protocol_error", "protocol_error", "provider_http_error"}: return ProviderErrorClass.PROTOCOL
    if code in {"provider_context_overflow", "context_overflow"}: return ProviderErrorClass.CONTEXT_OVERFLOW
    if code in {"provider_content_policy", "content_policy", "content_filter"}: return ProviderErrorClass.CONTENT_POLICY
    if code in {"provider_selection_invalid", "model_not_authorized", "config_ref_not_authorized", "provider_endpoint_invalid", "provider_egress_denied", "reasoning_parameter_invalid", "local_validation"}: return ProviderErrorClass.LOCAL_VALIDATION
    if code in {"provider_stream_interrupted", "stream_interrupted"}: return ProviderErrorClass.STREAM_INTERRUPTED
    if code in {"empty_response", "provider_empty_response"}: return ProviderErrorClass.EMPTY_RESPONSE
    return ProviderErrorClass.UNKNOWN


def overflow_verdict(error: BaseException, *, requested_output_tokens: int) -> OverflowVerdict:
    window = getattr(error, "provider_window", None); available = getattr(error, "available_output_tokens", None)
    window = window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None
    available = available if isinstance(available, int) and not isinstance(available, bool) and available >= 0 else 0
    return OverflowVerdict(window, min(available, requested_output_tokens), bool(getattr(error, "compression_restart_allowed", False)), str(getattr(error, "exhausted_reason", None) or "provider_context_window_exhausted")[:128])


class ReferenceProviderResolver:
    def __init__(self, factory: Callable[[ProviderConfigRef], ProviderClient], refresh: Callable[[ProviderConfigRef], bool] | None = None) -> None:
        self._factory, self._refresh, self.resolved, self.refreshed = factory, refresh, [], []
    def resolve(self, config: ProviderConfigRef) -> ProviderClient:
        self.resolved.append(config); return self._factory(config)
    def refresh(self, config: ProviderConfigRef) -> bool:
        self.refreshed.append(config); return bool(self._refresh and self._refresh(config))


def _record_attempt(ledger, receipt, sink):
    ledger.append(receipt)
    if sink is not None: sink(receipt)


def _safe_usage(value: Mapping[str, Any]) -> dict[str, int]:
    return {str(name): amount for name, amount in value.items() if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0}


def _usage(chunks: Sequence[ProviderChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        for name, value in _safe_usage(chunk.usage).items(): result[name] = result.get(name, 0) + value
    return result


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
