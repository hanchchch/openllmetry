"""OpenTelemetry Anthropic instrumentation"""

import json
import logging
import os
import time
from typing import Collection

from anthropic._streaming import AsyncStream, Stream
from anthropic.types.message import ContentBlock, Message, Usage
from opentelemetry import context as context_api
from opentelemetry.instrumentation.anthropic.config import Config
from opentelemetry.instrumentation.anthropic.streaming import (
    _abuild_from_streaming_response,
    _build_from_streaming_response,
)
from opentelemetry.instrumentation.anthropic.utils import (
    set_span_attribute,
    should_send_prompts,
)
from opentelemetry.instrumentation.anthropic.version import __version__
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import _SUPPRESS_INSTRUMENTATION_KEY, unwrap
from opentelemetry.metrics import Counter, Histogram, Meter, get_meter
from opentelemetry.semconv.ai import LLMRequestTypeValues, SpanAttributes
from opentelemetry.trace import SpanKind, Tracer, get_tracer
from opentelemetry.trace.status import Status, StatusCode
from wrapt import wrap_function_wrapper

logger = logging.getLogger(__name__)

_instruments = ("anthropic >= 0.3.11",)

WRAPPED_METHODS = [
    {
        "package": "anthropic.resources.completions",
        "object": "Completions",
        "method": "create",
        "span_name": "anthropic.completion",
    },
    {
        "package": "anthropic.resources.messages",
        "object": "Messages",
        "method": "create",
        "span_name": "anthropic.completion",
    },
    {
        "package": "anthropic.resources.messages",
        "object": "Messages",
        "method": "stream",
        "span_name": "anthropic.completion",
    },
]
WRAPPED_AMETHODS = [
    {
        "package": "anthropic.resources.completions",
        "object": "AsyncCompletions",
        "method": "create",
        "span_name": "anthropic.completion",
    },
    {
        "package": "anthropic.resources.messages",
        "object": "AsyncMessages",
        "method": "create",
        "span_name": "anthropic.completion",
    },
    {
        "package": "anthropic.resources.messages",
        "object": "AsyncMessages",
        "method": "stream",
        "span_name": "anthropic.completion",
    },
]


def is_streaming_response(response):
    return isinstance(response, Stream) or isinstance(response, AsyncStream)


def _dump_content(content):
    if isinstance(content, str):
        return content
    json_serializable = []
    for item in content:
        if item.get("type") == "text":
            json_serializable.append({"type": "text", "text": item.get("text")})
        elif item.get("type") == "image":
            json_serializable.append(
                {
                    "type": "image",
                    "source": {
                        "type": item.get("source").get("type"),
                        "media_type": item.get("source").get("media_type"),
                        "data": str(item.get("source").get("data")),
                    },
                }
            )
    return json.dumps(json_serializable)


def _set_input_attributes(span, kwargs):
    set_span_attribute(span, SpanAttributes.LLM_REQUEST_MODEL, kwargs.get("model"))
    set_span_attribute(
        span, SpanAttributes.LLM_REQUEST_MAX_TOKENS, kwargs.get("max_tokens_to_sample")
    )
    set_span_attribute(span, SpanAttributes.LLM_TEMPERATURE, kwargs.get("temperature"))
    set_span_attribute(span, SpanAttributes.LLM_TOP_P, kwargs.get("top_p"))
    set_span_attribute(
        span, SpanAttributes.LLM_FREQUENCY_PENALTY, kwargs.get("frequency_penalty")
    )
    set_span_attribute(
        span, SpanAttributes.LLM_PRESENCE_PENALTY, kwargs.get("presence_penalty")
    )
    set_span_attribute(span, SpanAttributes.LLM_IS_STREAMING, kwargs.get("stream"))

    if should_send_prompts():
        if kwargs.get("prompt") is not None:
            set_span_attribute(
                span, f"{SpanAttributes.LLM_PROMPTS}.0.user", kwargs.get("prompt")
            )

        elif kwargs.get("messages") is not None:
            for i, message in enumerate(kwargs.get("messages")):
                set_span_attribute(
                    span,
                    f"{SpanAttributes.LLM_PROMPTS}.{i}.user",
                    _dump_content(message.get("content")),
                )


def _set_span_completions(span, response):
    index = 0
    prefix = f"{SpanAttributes.LLM_COMPLETIONS}.{index}"
    set_span_attribute(span, f"{prefix}.finish_reason", response.get("stop_reason"))
    if response.get("completion"):
        set_span_attribute(span, f"{prefix}.content", response.get("completion"))
    elif response.get("content"):
        for i, content in enumerate(response.get("content")):
            set_span_attribute(
                span,
                f"{SpanAttributes.LLM_COMPLETIONS}.{i}.content",
                content.text,
            )


async def _set_token_usage_a(span, anthropic, request, response):
    if not isinstance(response, dict):
        response = response.__dict__

    prompt_tokens = 0
    if request.get("prompt"):
        prompt_tokens = await anthropic.count_tokens(request.get("prompt"))
    elif request.get("messages"):
        prompt_tokens = sum(
            [
                await anthropic.count_tokens(m.get("content"))
                for m in request.get("messages")
            ]
        )

    completion_tokens = 0
    if response.get("completion"):
        completion_tokens = await anthropic.count_tokens(response.get("completion"))
    elif response.get("content"):
        completion_tokens = await anthropic.count_tokens(
            response.get("content")[0].text
        )

    total_tokens = prompt_tokens + completion_tokens

    set_span_attribute(span, SpanAttributes.LLM_USAGE_PROMPT_TOKENS, prompt_tokens)
    set_span_attribute(
        span, SpanAttributes.LLM_USAGE_COMPLETION_TOKENS, completion_tokens
    )
    set_span_attribute(span, SpanAttributes.LLM_USAGE_TOTAL_TOKENS, total_tokens)


def _set_token_usage(
    span,
    anthropic,
    request,
    response,
    metric_attributes: dict = {},
    token_counter: Counter = None,
    choice_counter: Counter = None,
):
    if not isinstance(response, dict):
        response = response.__dict__

    prompt_tokens = 0
    if request.get("prompt"):
        prompt_tokens = anthropic.count_tokens(request.get("prompt"))
    elif request.get("messages"):
        prompt_tokens = sum(
            [anthropic.count_tokens(m.get("content")) for m in request.get("messages")]
        )

    if token_counter and type(prompt_tokens) is int and prompt_tokens >= 0:
        token_counter.add(
            prompt_tokens,
            attributes={
                **metric_attributes,
                "llm.usage.token_type": "prompt",
            },
        )

    completion_tokens = 0
    if response.get("completion"):
        completion_tokens = anthropic.count_tokens(response.get("completion"))
    elif response.get("content"):
        completion_tokens = anthropic.count_tokens(response.get("content")[0].text)

    if token_counter and type(completion_tokens) is int and completion_tokens >= 0:
        token_counter.add(
            completion_tokens,
            attributes={
                **metric_attributes,
                "llm.usage.token_type": "completion",
            },
        )

    total_tokens = prompt_tokens + completion_tokens

    choices = 0
    if type(response.get("content")) is list:
        choices = len(response.get("content"))
    elif response.get("completion"):
        choices = 1

    if choices > 0 and choice_counter:
        choice_counter.add(
            choices,
            attributes={
                **metric_attributes,
                "llm.response.stop_reason": response.get("stop_reason"),
            },
        )

    set_span_attribute(span, SpanAttributes.LLM_USAGE_PROMPT_TOKENS, prompt_tokens)
    set_span_attribute(
        span, SpanAttributes.LLM_USAGE_COMPLETION_TOKENS, completion_tokens
    )
    set_span_attribute(span, SpanAttributes.LLM_USAGE_TOTAL_TOKENS, total_tokens)


def _set_response_attributes(span, response):
    if not isinstance(response, dict):
        response = response.__dict__
    set_span_attribute(span, SpanAttributes.LLM_RESPONSE_MODEL, response.get("model"))

    if response.get("usage"):
        prompt_tokens = response.get("usage").input_tokens
        completion_tokens = response.get("usage").output_tokens
        set_span_attribute(span, SpanAttributes.LLM_USAGE_PROMPT_TOKENS, prompt_tokens)
        set_span_attribute(
            span, SpanAttributes.LLM_USAGE_COMPLETION_TOKENS, completion_tokens
        )
        set_span_attribute(
            span,
            SpanAttributes.LLM_USAGE_TOTAL_TOKENS,
            prompt_tokens + completion_tokens,
        )

    if should_send_prompts():
        _set_span_completions(span, response)


def _with_tracer_wrapper(func):
    """Helper for providing tracer for wrapper functions."""

    def _with_tracer(tracer, to_wrap):
        def wrapper(wrapped, instance, args, kwargs):
            return func(tracer, to_wrap, wrapped, instance, args, kwargs)

        return wrapper

    return _with_tracer


def _with_chat_telemetry_wrapper(func):
    """Helper for providing tracer for wrapper functions. Includes metric collectors."""

    def _with_chat_telemetry(
        tracer,
        meter,
        to_wrap,
    ):
        def wrapper(wrapped, instance, args, kwargs):
            return func(
                tracer,
                meter,
                to_wrap,
                wrapped,
                instance,
                args,
                kwargs,
            )

        return wrapper

    return _with_chat_telemetry


def _create_metrics(meter: Meter):
    token_counter = meter.create_counter(
        name="llm.anthropic.completion.tokens",
        unit="token",
        description="Number of tokens used in prompt and completions",
    )

    choice_counter = meter.create_counter(
        name="llm.anthropic.completion.choices",
        unit="choice",
        description="Number of choices returned by chat completions call",
    )

    duration_histogram = meter.create_histogram(
        name="llm.anthropic.completion.duration",
        unit="s",
        description="Duration of chat completion operation",
    )

    exception_counter = meter.create_counter(
        name="llm.anthropic.completion.exceptions",
        unit="time",
        description="Number of exceptions occurred during chat completions",
    )

    return token_counter, choice_counter, duration_histogram, exception_counter


def _get_shared_metric_attributes(response: dict = None, exception: Exception = None):
    if response:
        if not isinstance(response, dict):
            response = response.__dict__
        return {
            "llm.response.model": response.get("model"),
        }
    if exception:
        return {
            "error.type": exception.__class__.__name__,
        }
    return {}


@_with_chat_telemetry_wrapper
def _wrap(
    tracer: Tracer,
    meter: Meter,
    to_wrap,
    wrapped,
    instance,
    args,
    kwargs,
):
    """Instruments and calls every function defined in TO_WRAP."""
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    if is_metrics_enabled():
        (
            token_counter,
            choice_counter,
            duration_histogram,
            exception_counter,
        ) = _create_metrics(meter)
    else:
        (
            token_counter,
            choice_counter,
            duration_histogram,
            exception_counter,
        ) = (None, None, None)

    name = to_wrap.get("span_name")
    span = tracer.start_span(
        name,
        kind=SpanKind.CLIENT,
        attributes={
            SpanAttributes.LLM_VENDOR: "Anthropic",
            SpanAttributes.LLM_REQUEST_TYPE: LLMRequestTypeValues.COMPLETION.value,
        },
    )
    try:
        if span.is_recording():
            _set_input_attributes(span, kwargs)

    except Exception as ex:  # pylint: disable=broad-except
        logger.warning(
            "Failed to set input attributes for anthropic span, error: %s", str(ex)
        )

    response = None
    exception = None
    metric_attributes = {}

    start_time = time.time()
    end_time = None
    try:
        response = wrapped(*args, **kwargs)
    except Exception as e:  # pylint: disable=broad-except
        exception = e
        raise e
    finally:
        end_time = time.time()

        metric_attributes = _get_shared_metric_attributes(response, exception)

        if end_time and duration_histogram:
            duration = end_time - start_time
            duration_histogram.record(duration, attributes=metric_attributes)

        if exception and exception_counter:
            exception_counter.add(1, attributes=metric_attributes)

    if is_streaming_response(response):
        return _build_from_streaming_response(span, response, instance._client, kwargs)
    elif response:
        try:
            if span.is_recording():
                _set_response_attributes(span, response)
                _set_token_usage(
                    span,
                    instance._client,
                    kwargs,
                    response,
                    metric_attributes,
                    token_counter,
                    choice_counter,
                )

        except Exception as ex:  # pylint: disable=broad-except
            logger.warning(
                "Failed to set response attributes for anthropic span, error: %s",
                str(ex),
            )
        if span.is_recording():
            span.set_status(Status(StatusCode.OK))
    span.end()
    return response

@_with_tracer_wrapper
async def _awrap(tracer, to_wrap, wrapped, instance, args, kwargs):
    """Instruments and calls every function defined in TO_WRAP."""
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    name = to_wrap.get("span_name")
    span = tracer.start_span(
        name,
        kind=SpanKind.CLIENT,
        attributes={
            SpanAttributes.LLM_VENDOR: "Anthropic",
            SpanAttributes.LLM_REQUEST_TYPE: LLMRequestTypeValues.COMPLETION.value,
        },
    )
    try:
        if span.is_recording():
            _set_input_attributes(span, kwargs)

    except Exception as ex:  # pylint: disable=broad-except
        logger.warning(
            "Failed to set input attributes for anthropic span, error: %s", str(ex)
        )

    response = await wrapped(*args, **kwargs)

    if is_streaming_response(response):
        return _abuild_from_streaming_response(span, response, instance._client, kwargs)
    elif response:
        try:
            if span.is_recording():
                _set_response_attributes(span, response)
                await _set_token_usage_a(span, instance._client, kwargs, response)

        except Exception as ex:  # pylint: disable=broad-except
            logger.warning(
                "Failed to set response attributes for anthropic span, error: %s",
                str(ex),
            )
        if span.is_recording():
            span.set_status(Status(StatusCode.OK))
    span.end()
    return response


def is_metrics_enabled() -> bool:
    return (os.getenv("TRACELOOP_METRICS_ENABLED") or "true").lower() == "true"


class AnthropicInstrumentor(BaseInstrumentor):
    """An instrumentor for Anthropic's client library."""

    def __init__(self, enrich_token_usage: bool = False):
        super().__init__()
        Config.enrich_token_usage = enrich_token_usage

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer_provider = kwargs.get("tracer_provider")
        tracer = get_tracer(__name__, __version__, tracer_provider)

        # meter and counters are inited here
        meter_provider = kwargs.get("meter_provider")
        meter = get_meter(__name__, __version__, meter_provider)

        for wrapped_method in WRAPPED_METHODS:
            wrap_package = wrapped_method.get("package")
            wrap_object = wrapped_method.get("object")
            wrap_method = wrapped_method.get("method")
            try:
                wrap_function_wrapper(
                    wrap_package,
                    f"{wrap_object}.{wrap_method}",
                    _wrap(
                        tracer,
                        meter,
                        wrapped_method,
                    ),
                )
            except ModuleNotFoundError:
                pass  # that's ok, we don't want to fail if some methods do not exist

        for wrapped_method in WRAPPED_AMETHODS:
            wrap_package = wrapped_method.get("package")
            wrap_object = wrapped_method.get("object")
            wrap_method = wrapped_method.get("method")
            try:
                wrap_function_wrapper(
                    wrap_package,
                    f"{wrap_object}.{wrap_method}",
                    _awrap(tracer, wrapped_method),
                )
            except ModuleNotFoundError:
                pass  # that's ok, we don't want to fail if some methods do not exist

    def _uninstrument(self, **kwargs):
        for wrapped_method in WRAPPED_METHODS:
            wrap_package = wrapped_method.get("package")
            wrap_object = wrapped_method.get("object")
            unwrap(
                f"{wrap_package}.{wrap_object}",
                wrapped_method.get("method"),
            )
        for wrapped_method in WRAPPED_AMETHODS:
            wrap_object = wrapped_method.get("object")
            unwrap(
                f"anthropic.resources.completions.{wrap_object}",
                wrapped_method.get("method"),
            )
