from __future__ import annotations

import json

import pytest

from networkclaw_harness.runtime import (
    FactKind,
    ProviderChunk,
    ProviderConfigRef,
    ProviderError,
    ProviderErrorClass,
    ProviderRecoveryError,
    ProviderRequest,
    ProviderRuntime,
    ReferenceProviderResolver,
    RetryDecision,
    RetryPolicy,
    classify_provider_error,
)


class ScriptedProvider:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def stream(self, *_args, **_kwargs):
        self.calls += 1
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        yield from value


def _config(provider="primary", model="model", reference="config", attempts=2):
    return ProviderConfigRef(provider, model, reference, 100, attempts, "v1")


def _request(request_id="request-1"):
    return ProviderRequest("session", b"private prompt", ({"role": "user", "content": "secret"},),
                           "prompt-hash", 100, request_id, "workspace-hash", "policy-hash", "budget-hash")


@pytest.mark.parametrize(("code", "kind"), (
    ("provider_unauthorized", ProviderErrorClass.CREDENTIAL),
    ("provider_rate_limited", ProviderErrorClass.RATE_LIMIT),
    ("provider_timeout", ProviderErrorClass.TIMEOUT),
    ("provider_server_error", ProviderErrorClass.TRANSIENT),
    ("provider_network_error", ProviderErrorClass.TRANSIENT),
    ("provider_response_invalid", ProviderErrorClass.PROTOCOL),
    ("provider_context_overflow", ProviderErrorClass.CONTEXT_OVERFLOW),
    ("provider_content_policy", ProviderErrorClass.CONTENT_POLICY),
    ("model_not_authorized", ProviderErrorClass.LOCAL_VALIDATION),
))
def test_error_classifier_is_typed_and_complete(code, kind):
    assert classify_provider_error(ProviderError(code, "safe")) is kind


def test_attempt_fact_is_allowlisted_stable_and_deduplicable():
    client = ScriptedProvider([ProviderError("provider_rate_limited", "raw private detail"),
                               (ProviderChunk("done", {"input_tokens": 3, "bad": -1}),)])
    delays = []
    attempts = []
    runtime = ProviderRuntime(ReferenceProviderResolver(lambda _config: client),
                              retry_policy=RetryPolicy(base_backoff_seconds=1, max_backoff_seconds=8, jitter_ratio=.5),
                              sleep=delays.append, random_source=lambda: 1.0)

    result = runtime.invoke(_request(), primary=_config(), attempt_sink=attempts.append)

    assert result.text == "done"
    assert delays == [1.5]
    assert [item.retry_decision for item in attempts] == [RetryDecision.RETRY, RetryDecision.SUCCEEDED]
    assert [item.attempt_id for item in attempts] == ["request-1:r1:a1", "request-1:r1:a2"]
    assert len({item.dedupe_key for item in attempts}) == 2
    fact = attempts[0].semantic_fact()
    assert fact.kind is FactKind.PROVIDER_ATTEMPT
    encoded = json.dumps(dict(fact.payload))
    assert "private prompt" not in encoded and "secret" not in encoded and "raw private detail" not in encoded


def test_credential_refresh_rebuilds_client_then_retries_once():
    clients = iter((ScriptedProvider([ProviderError("provider_unauthorized", "denied")]),
                    ScriptedProvider([(ProviderChunk("ok"),)])))
    resolver = ReferenceProviderResolver(lambda _config: next(clients), refresh=lambda _config: True)
    result = ProviderRuntime(resolver, sleep=lambda _delay: None).invoke(_request(), primary=_config())

    assert result.text == "ok"
    assert len(resolver.refreshed) == 1 and len(resolver.resolved) == 2
    assert result.attempt_ledger[0].retry_decision == RetryDecision.REFRESH_CREDENTIAL


def test_protocol_falls_back_without_retry_and_contract_snapshot_stays_frozen():
    primary = ScriptedProvider([ProviderError("provider_response_invalid", "bad response")])
    fallback = ScriptedProvider([(ProviderChunk("ok"),)])
    resolver = ReferenceProviderResolver(lambda config: primary if config.provider_id == "primary" else fallback)
    result = ProviderRuntime(resolver).invoke(
        _request(), primary=_config(), fallbacks=(_config("backup", "backup-model", "backup-ref"),),
        allowed_routes=(("primary", "model"), ("backup", "backup-model")),
    )

    assert primary.calls == fallback.calls == 1
    assert [item.retry_decision for item in result.attempt_ledger] == [RetryDecision.FALLBACK, RetryDecision.SUCCEEDED]
    assert len({item.route_snapshot_hash for item in result.attempt_ledger}) == 1
    assert result.route_snapshot.contract_hash == _request().contract_hash
    with pytest.raises(ProviderError) as denied:
        ProviderRuntime(resolver).invoke(_request(), primary=_config(), allowed_routes=(("other", "model"),))
    assert denied.value.code == "provider_route_not_allowed"


def test_retry_ceiling_is_global_across_route_chain():
    clients = {}
    resolver = ReferenceProviderResolver(lambda config: clients[config.provider_id])
    clients.update({name: ScriptedProvider([ProviderError("provider_server_error", "down")] * 4)
                    for name in ("one", "two")})
    runtime = ProviderRuntime(resolver, retry_policy=RetryPolicy(max_total_attempts=3, base_backoff_seconds=0,
                                                                 max_backoff_seconds=0, jitter_ratio=0))
    with pytest.raises(ProviderRecoveryError) as failed:
        runtime.invoke(_request(), primary=_config("one", attempts=4), fallbacks=(_config("two", attempts=4),))
    assert len(failed.value.attempts) == 3
    assert sum(client.calls for client in clients.values()) == 3


def test_overflow_returns_typed_restart_verdict_without_fallback():
    overflow = ProviderError("provider_context_overflow", "too large", provider_window=128_000,
                             available_output_tokens=250, compression_restart_allowed=True,
                             exhausted_reason="input_exceeds_window")
    primary = ScriptedProvider([overflow])
    fallback = ScriptedProvider([(ProviderChunk("must not run"),)])
    resolver = ReferenceProviderResolver(lambda config: primary if config.provider_id == "primary" else fallback)
    with pytest.raises(ProviderRecoveryError) as failed:
        ProviderRuntime(resolver).invoke(_request(), primary=_config(),
                                         fallbacks=(_config("backup", "backup", "backup-ref"),))

    verdict = failed.value.overflow
    assert verdict is not None
    assert verdict.provider_reported_window == 128_000
    assert verdict.available_output_tokens == 100
    assert verdict.compression_restart_allowed is True
    assert verdict.exhausted_reason == "input_exceeds_window"
    assert failed.value.attempts[-1].retry_decision == RetryDecision.RESTART_WITH_COMPRESSION
    assert failed.value.attempts[-1].restart is True
    assert fallback.calls == 0


@pytest.mark.parametrize("code", ("provider_content_policy", "model_not_authorized"))
def test_policy_and_local_validation_are_terminal(code):
    primary = ScriptedProvider([ProviderError(code, "stopped")])
    fallback = ScriptedProvider([(ProviderChunk("must not run"),)])
    resolver = ReferenceProviderResolver(lambda config: primary if config.provider_id == "primary" else fallback)
    with pytest.raises(ProviderRecoveryError) as failed:
        ProviderRuntime(resolver).invoke(_request(), primary=_config(),
                                         fallbacks=(_config("backup", "backup", "backup-ref"),))
    assert failed.value.attempts[-1].retry_decision == RetryDecision.TERMINAL
    assert fallback.calls == 0


def test_visible_partial_stream_is_never_retried_or_fallen_back():
    class Partial:
        def stream(self, *_args, **_kwargs):
            yield ProviderChunk("visible")
            raise ProviderError("provider_server_error", "down")

    fallback = ScriptedProvider([(ProviderChunk("duplicate"),)])
    resolver = ReferenceProviderResolver(lambda config: Partial() if config.provider_id == "primary" else fallback)
    projected = []
    with pytest.raises(ProviderRecoveryError) as failed:
        ProviderRuntime(resolver).invoke(_request(), primary=_config(),
            fallbacks=(_config("backup", "backup", "backup-ref"),), on_chunk=lambda chunk: projected.append(chunk.delta))
    assert projected == ["visible"]
    assert failed.value.code == "provider_stream_interrupted"
    assert fallback.calls == 0
