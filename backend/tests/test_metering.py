"""Usage parsing for the gateway (doc 07b)."""

from __future__ import annotations

from gaia.llm.metering import (
    UsageAccumulator,
    anthropic_usage_from_payload,
    deepseek_token_buckets,
    openai_usage_from_payload,
)


def test_deepseek_buckets_keep_explicit_cache_fields() -> None:
    assert deepseek_token_buckets(prompt_tokens=100, cache_hit_tokens=70, cache_miss_tokens=20) == (
        20,
        70,
    )


def test_deepseek_buckets_derive_misses_when_missing() -> None:
    assert deepseek_token_buckets(prompt_tokens=100, cache_hit_tokens=70, cache_miss_tokens=0) == (
        30,
        70,
    )


def test_deepseek_buckets_treat_prompt_as_miss_when_no_cache_fields() -> None:
    assert deepseek_token_buckets(prompt_tokens=100, cache_hit_tokens=0, cache_miss_tokens=0) == (
        100,
        0,
    )


def test_anthropic_json_usage_is_parsed() -> None:
    usage = anthropic_usage_from_payload(
        {
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
        provider="anthropic",
        request_id="req_1",
    )

    assert usage is not None
    assert usage.provider == "anthropic"
    assert usage.model == "claude-opus-5-5"
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 30
    assert usage.cache_creation_tokens == 40
    assert usage.request_id == "req_1"


def test_anthropic_json_without_usage_is_none() -> None:
    assert anthropic_usage_from_payload({"model": "claude-opus-5-5"}, provider="anthropic") is None


def test_openai_json_usage_uses_deepseek_buckets() -> None:
    usage = openai_usage_from_payload(
        {
            "model": "deepseek-v4-pro",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 30,
            },
        },
        provider="deepseek",
    )

    assert usage is not None
    assert usage.provider == "deepseek"
    assert usage.model == "deepseek-v4-pro"
    assert usage.input_tokens == 30
    assert usage.output_tokens == 25
    assert usage.cache_read_tokens == 60
    assert usage.cache_creation_tokens == 0


def test_openai_json_without_usage_is_none() -> None:
    assert openai_usage_from_payload({"model": "deepseek-v4-pro"}, provider="deepseek") is None


def test_anthropic_non_stream_accumulates_json() -> None:
    accumulator = UsageAccumulator(provider="anthropic", parser="anthropic", stream=False)
    accumulator.feed(b'{"model":"claude-opus-5-5","usage":{"input_tokens":1,')
    accumulator.feed(b'"output_tokens":2}}')

    usage = accumulator.finish(request_id="req_2")

    assert usage is not None
    assert usage.input_tokens == 1
    assert usage.output_tokens == 2
    assert usage.request_id == "req_2"


def test_openai_non_stream_accumulates_json() -> None:
    accumulator = UsageAccumulator(provider="deepseek", parser="openai", stream=False)
    accumulator.feed(b'{"model":"deepseek-v4-pro","usage":{"prompt_tokens":50,')
    accumulator.feed(b'"completion_tokens":5}}')

    usage = accumulator.finish()

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.output_tokens == 5


def test_anthropic_sse_splits_events_across_chunks_and_keeps_last_output() -> None:
    accumulator = UsageAccumulator(provider="anthropic", parser="anthropic", stream=True)
    accumulator.feed(
        b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
        b'"usage":{"input_tokens":11,"output_tokens":1,"cache_read_input_tokens":2}}}'
    )
    accumulator.feed(b"\n\n")
    accumulator.feed(
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    )
    accumulator.feed(
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
    )

    usage = accumulator.finish()

    assert usage is not None
    assert usage.provider == "anthropic"
    assert usage.model == "claude-opus-5-5"
    assert usage.input_tokens == 11
    assert usage.output_tokens == 9
    assert usage.cache_read_tokens == 2


def test_openai_sse_keeps_the_chunk_with_usage() -> None:
    accumulator = UsageAccumulator(provider="deepseek", parser="openai", stream=True)
    accumulator.feed(
        b'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"content":"hi"}}]}\n\n'
    )
    accumulator.feed(
        b'data: {"model":"deepseek-v4-pro","usage":{"prompt_tokens":80,'
        b'"completion_tokens":8,"prompt_cache_hit_tokens":70,"prompt_cache_miss_tokens":5}}\n\n'
    )
    accumulator.feed(b"data: [DONE]\n\n")

    usage = accumulator.finish(request_id="req_3")

    assert usage is not None
    assert usage.input_tokens == 5
    assert usage.output_tokens == 8
    assert usage.cache_read_tokens == 70
    assert usage.request_id == "req_3"


def test_streaming_without_recognised_usage_is_none() -> None:
    anthropic = UsageAccumulator(provider="anthropic", parser="anthropic", stream=True)
    anthropic.feed(
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
    )
    assert anthropic.finish() is None

    openai = UsageAccumulator(provider="deepseek", parser="openai", stream=True)
    openai.feed(b"data: [DONE]\n\n")
    assert openai.finish() is None


def test_invalid_non_stream_json_is_none() -> None:
    accumulator = UsageAccumulator(provider="anthropic", parser="anthropic", stream=False)
    accumulator.feed(b"not-json")
    assert accumulator.finish() is None

    openai = UsageAccumulator(provider="deepseek", parser="openai", stream=False)
    openai.feed(b"not-json")
    assert openai.finish() is None


def test_non_integer_usage_fields_fall_back_to_zero() -> None:
    usage = anthropic_usage_from_payload(
        {"usage": {"input_tokens": "not-an-int"}}, provider="anthropic"
    )
    assert usage is not None
    assert usage.input_tokens == 0


def test_anthropic_sse_ignores_ignorable_lines_and_bad_data() -> None:
    accumulator = UsageAccumulator(provider="anthropic", parser="anthropic", stream=True)
    accumulator.feed(b": keepalive\n")
    accumulator.feed(b"retry: 100\n")
    accumulator.feed(b"event: message_start\ndata: [1, 2]\n\n")
    accumulator.feed(b"event: message_start\ndata: not-json\n\n")
    accumulator.feed(b"event: message_start\ndata: [DONE]\n\n")
    accumulator.feed(b'event: message_start\ndata: {"message":"not-an-object"}\n\n')
    accumulator.feed(
        b'event: message_start\ndata: {"message":{"model":"m","usage":"not-an-object"}}\n\n'
    )
    accumulator.feed(b'event: message_delta\ndata: {"usage":{"output_tokens":null}}\n\n')
    accumulator.feed(b'event: content_block_delta\ndata: {"delta":"ignored"}\n\n')
    accumulator.feed(b"data:\n\n")

    usage = accumulator.finish()
    assert usage is not None
    assert usage.model == "m"
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_openai_sse_ignores_invalid_json() -> None:
    accumulator = UsageAccumulator(provider="deepseek", parser="openai", stream=True)
    accumulator.feed(b"data: not-json\n\n")
    accumulator.feed(b"data: [DONE]\n\n")

    assert accumulator.finish() is None
