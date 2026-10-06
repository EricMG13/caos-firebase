"""The one model factory: every production call goes through OpenRouter (D8).

Spec section 3 (D7, D8). `chat_model` returns the LangChain chat model the host
talks to -- `ChatOpenAI` on OpenRouter's OpenAI-compatible API -- and
`ChatCompletions` adapts it to the `CompletionProvider` seam the canonical
executor was built on, so nothing downstream of this module knows which vendor
answered. Tests inject another `BaseChatModel` through `completions(chat=...)`;
the app package never imports one.

Two things differ from the legacy transport and are decided here, not hidden.
The charge is computed: a gateway response carries token counts, not money, so
the call is billed as `tokens x the dated price` under one Decimal context
(invariant 7). The generation id is the response's own when it names one; a
response that does not is given a host-minted one, prefixed so a reader can
tell, because an accepted artifact must be able to say what produced it
(invariant 3) and a blank would be refused outright.
"""

from __future__ import annotations

import contextvars
import hashlib
import math
import os
import threading
import time
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from typing import Any
from urllib.parse import urlsplit

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from openai import OpenAIError
from pydantic import SecretStr

# The configuration names live beside the price they configure, so the API,
# which may not import this module (D4), reads them too.
from caos.pricing import DEFAULT_ENDPOINT as DEFAULT_ENDPOINT
from caos.pricing import ENDPOINT_ENV as ENDPOINT_ENV
from caos.pricing import MODEL_PRICE_ENV as MODEL_PRICE_ENV
from caos.pricing import REASONING_EFFORT_ENV as REASONING_EFFORT_ENV
from caos.pricing import ModelPrice, exact_context, model_choices, reasoning_effort
from caos.pricing import configured_endpoint as configured_endpoint
from caos.provider import (
    MAX_COMPLETION_TOKENS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    NEVER_RETRIED,
    TIMEOUT_SECONDS,
    Completion,
    check_resend,
    encode_request,
    finish_refusal,
    reported_charge,
)
from caos.refusals import Refusal, RefusalCode
from caos.store.outcomes import producer_identifier

PLATFORM = "openrouter"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
PROVIDER_ORDER_ENV = "CAOS_PROVIDER_ORDER"
# Where the OpenAI-compatible API is answered (D8): OpenRouter unless set;
# https, or plain http to this machine alone (a local stand-in).
MODEL_BASE_URL_ENV = "CAOS_MODEL_BASE_URL"
LOOPBACK_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
HOST_MINTED = "host-"
# A 429 reached no model, so it is asked again under the same reservation
# (DP-5): this many tries in all, waiting `Retry-After` up to the cap.
RATE_LIMITED = 429
RATE_LIMIT_TRIES = 3
RETRY_AFTER_SECONDS = 2.0
RETRY_AFTER_CAP_SECONDS = 20.0
# A re-send starts only with at least this long left of the call's one
# deadline (N11): half of it, 210 s since D83 (the 120 s whole-call deadline
# F89 measured as too short to deliver a few thousand tokens at real
# throughput was half of the 240 before it). A re-send started with less is
# expected to be abandoned mid-generation at the deadline while the provider
# may still bill it, so the 429 is the answer instead. Two capped waits spend
# at most 40 s of the 420, so a rate limit answered promptly is still re-sent;
# only 429s that were themselves slow to arrive leave less.
MIN_RESEND_SECONDS = TIMEOUT_SECONDS / 2
_sleep = time.sleep
_clock = time.monotonic


def identity_of(
    model: str, reasoning_effort: str | None = None, order: tuple[str, ...] = ()
) -> str:
    """The execution profile a verdict binds (D8), from the names alone, so a
    caller can refuse an unexpected one before any client is built: the
    provider pin, when set, as `model@order` with the names joined by `,`."""
    named = f"{model}@{','.join(order)}" if order else model
    return "/".join(
        (PLATFORM, named, reasoning_effort or "none", str(MAX_COMPLETION_TOKENS))
    )


def provider_order() -> tuple[str, ...]:
    """The providers `CAOS_PROVIDER_ORDER` pins, in order; empty when unpinned."""
    listed = os.environ.get(PROVIDER_ORDER_ENV, "").split(",")
    return tuple(name.strip() for name in listed if name.strip())


def model_base_url() -> str:
    """The API's base URL (D8): OpenRouter's unless `CAOS_MODEL_BASE_URL` is
    set; https, or http to a loopback host, else `PROVIDER_NOT_CONFIGURED`."""
    url = os.environ.get(MODEL_BASE_URL_ENV) or OPENROUTER_BASE_URL
    try:
        parts = urlsplit(url)
        # A malformed port raises on reading it, here rather than at the call.
        host, _port = parts.hostname, parts.port
    except ValueError:
        raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED) from None
    if not host or parts.username or parts.password:
        raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK_HOSTS):
        return url
    raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)


def chat_model(*, endpoint: str | None = None) -> BaseChatModel:
    """The production chat model: OpenRouter's OpenAI-compatible API (D5, D8).

    The key is read at the call and never stored or printed. The reasoning
    effort and the provider pin travel in the request body, so the identity
    a verdict binds names what the model actually received.
    """
    key = os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)
    base_url = model_base_url()
    from langchain_openai import ChatOpenAI

    body: dict[str, object] = {}
    effort = reasoning_effort()
    if effort:
        body["reasoning"] = {"effort": effort}
    order = provider_order()
    if order:
        body["provider"] = {"order": list(order), "allow_fallbacks": False}
    # The same deadline and no retry below the seam as before (F40): a retry
    # is the caller's reservation.
    return ChatOpenAI(
        base_url=base_url,
        api_key=SecretStr(key),
        model=endpoint or configured_endpoint(),
        max_completion_tokens=MAX_COMPLETION_TOKENS,
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
        extra_body=body or None,
    )


@dataclass(frozen=True, slots=True)
class ChatCompletions:
    """A `CompletionProvider` over one LangChain chat model, priced and identified.

    `model` is the endpoint name: the identity the host configured, which is
    what a run's price must name and what an accepted artifact records.
    """

    chat: BaseChatModel = field(repr=False)
    model: str
    price: ModelPrice
    reasoning_effort: str | None = None
    provider_order: tuple[str, ...] = ()

    @property
    def qualification_identity(self) -> str:
        """The execution profile a qualification verdict binds (D8)."""
        return identity_of(self.model, self.reasoning_effort, self.provider_order)

    def request_bytes(self, prompt: str, *, json_object: bool = False) -> bytes:
        """The request the call is priced on: model, prompt, ceiling, format."""
        return encode_request(self.model, prompt, json_object=json_object)

    def complete(self, prompt: str, *, json_object: bool = False) -> Completion:
        """Ask once; a rate limit alone is asked again, under the same
        reservation and inside one deadline for the whole call (ST-9)."""
        if producer_identifier(self.model, limit=256) is None:
            raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)
        if not isinstance(prompt, str) or (
            len(self.request_bytes(prompt, json_object=json_object)) > MAX_REQUEST_BYTES
        ):
            raise Refusal(RefusalCode.PROVIDER_CALL_INVALID)
        # JSON mode travels on the wire (F27): the legacy request carried it,
        # `request_bytes` already budgets for it, and the canonical executor
        # asks for it on every module call.
        options: dict[str, Any] = {}
        if json_object:
            options["response_format"] = {"type": "json_object"}
        # One deadline for every try and every wait (ST-9, MAX-21), so the
        # worst case the lease and the stale threshold are sized against is
        # `TIMEOUT_SECONDS`, not three of them and two waits.
        deadline = _clock() + TIMEOUT_SECONDS
        sent = 0
        while True:
            sent += 1
            answer = _invoked(self.chat, prompt, options, deadline - _clock())
            if isinstance(answer, OpenAIError):
                if not _sends_again(answer, sent, deadline):
                    return Completion(None, None, None, _status_refusal(answer))
                continue
            if answer is None:
                # Indeterminate: the request may have been delivered and
                # billed, so the attempt keeps its reservation. Nothing of
                # the error travels.
                return Completion(None, None, None, RefusalCode.PROVIDER_UNAVAILABLE)
            if not isinstance(answer, AIMessage):
                return Completion(
                    None, None, None, RefusalCode.PROVIDER_RESPONSE_INVALID
                )
            return self._completion(prompt, answer, json_object=json_object)

    def _completion(
        self, prompt: str, message: AIMessage, *, json_object: bool = False
    ) -> Completion:
        content = _text(message.content)
        charge = self._charge(
            message.usage_metadata,
            sent=len(self.request_bytes(prompt, json_object=json_object)),
            answered=bool(content),
        )
        generation = producer_identifier(_claimed_id(message), limit=512)
        if generation is None:
            generation = (
                HOST_MINTED
                + hashlib.sha256(
                    (prompt + (content or "")).encode("utf-8", "surrogatepass")
                ).hexdigest()[:40]
            )
        # A finish reason the response does not state is not `stop` (F34):
        # `stop` is the one completed reason, and an answer with none is a
        # response the host does not understand.
        finish = message.response_metadata.get("finish_reason")
        refusal = (
            finish_refusal(finish)
            if isinstance(finish, str) and finish
            else RefusalCode.PROVIDER_RESPONSE_INVALID
        )
        if refusal is not None:
            return Completion(None, charge, generation, refusal)
        # Blank text -- empty, whitespace, a fence around nothing -- is no
        # answer, as no text is (CF-047): billed, and never handed on as a
        # handoff to be refused as malformed.
        if not content or charge is None:
            return Completion(
                None, charge, generation, RefusalCode.PROVIDER_RESPONSE_INVALID
            )
        if len(content.encode("utf-8", "surrogatepass")) > MAX_RESPONSE_BYTES:
            # Billed, never stored or parsed (F35): no prefix of an oversize
            # answer is an answer.
            return Completion(
                None, charge, generation, RefusalCode.PROVIDER_RESPONSE_INVALID
            )
        return Completion(content, charge, generation)

    def _charge(
        self, usage: Mapping[str, Any] | None, *, sent: int, answered: bool
    ) -> Decimal | None:
        """`tokens x price`, exact, or unknown when the usage is not accounting
        the host understands.

        Counts are whole and never negative (AR-14), and possible (ST-11,
        MAX-05): the client reads an absent or null count as zero, so a
        request billed no input, or an answer billed no output, is a count
        the provider never stated; no request carries more tokens than bytes
        (the premise `priced_request` reserves on) and no answer more than
        `MAX_COMPLETION_TOKENS`. The product is computed in the reservation's
        own exact context (MAX-N03), so it is refused rather than rounded.
        """
        if usage is None:
            return None
        try:
            input_tokens = _count(usage["input_tokens"], most=sent)
            output_tokens = _count(usage["output_tokens"], most=MAX_COMPLETION_TOKENS)
            if not input_tokens or (answered and not output_tokens):
                return None
            exact = exact_context()
            amount = exact.add(
                exact.multiply(Decimal(input_tokens), self.price.input_per_token),
                exact.multiply(Decimal(output_tokens), self.price.output_per_token),
            )
        except (KeyError, TypeError, ValueError, DecimalException):
            return None
        return reported_charge(amount)


def _count(value: object, *, most: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= most:
        raise ValueError
    return value


def _invoked(
    chat: BaseChatModel, prompt: str, options: dict[str, Any], seconds: float
) -> object:
    """One `invoke`: its answer, the vendor error it raised, or None when the
    call is indeterminate.

    Abandoned once `seconds` pass (ST-9): the client's timeout bounds each
    read, not the call, so a server that sends a byte at a time would hold it
    open with no limit, past the lease it was sized in. The call runs on a
    helper thread; an answer that has not arrived by the deadline is None, as
    a socket timeout is. An abandoned thread holds only its own socket and
    never writes to the store.

    Once the request may have been sent, whatever else the stack raises is
    None too (ST-8): an empty `choices` is an `IndexError` from the client,
    and no failure may escape untyped ahead of `record_outcome`.
    """
    answered: list[object] = []
    context = contextvars.copy_context()

    def send() -> None:
        with suppress(Exception):  # indeterminate, never text (ST-8)
            try:
                with _content_parts_contained():
                    answered.append(
                        context.run(
                            chat.invoke, [HumanMessage(content=prompt)], **options
                        )
                    )
            except OpenAIError as failed:
                answered.append(failed)

    sender = threading.Thread(target=send, name="caos-model-call", daemon=True)
    sender.start()
    sender.join(max(seconds, 0.0))
    return answered[0] if answered else None


# Pydantic's serializer warning when the client dumps a response message whose
# `content` is a list of parts where its model declares a string (CF-077). The
# warning quotes that value -- the model's answer, which quotes the evidence --
# and Python prints it to stderr. Matched by category and the whole message:
# one content-list entry, nothing after it; no other warning is touched.
_CONTENT_PARTS_WARNING = (
    r"Pydantic serializer warnings:\s+PydanticSerializationUnexpectedValue\("
    r"Expected `str` - serialized value may not be as expected \["
    r"field_name='content', input_value=.*, input_type=list\]\)\Z"
)


@contextmanager
def _content_parts_contained() -> Iterator[None]:
    """Contain `_CONTENT_PARTS_WARNING` for the one call inside the block.

    Python 3.13's filter state is process-wide. A call abandoned at its
    deadline and still running when the next one starts can end the next
    call's containment early, or leave this one filter in place after both;
    neither touches any other warning.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=_CONTENT_PARTS_WARNING, category=UserWarning
        )
        yield


def _sends_again(failed: OpenAIError, sent: int, deadline: float) -> bool:
    """Whether the call is sent again after `failed`: a rate limit waited out
    (`_waited_out`), every installed check asked, and `MIN_RESEND_SECONDS`
    still left."""
    if not _waited_out(failed, sent, deadline):
        return False
    # Nothing was billed yet; what the caller installed decides whether the
    # call may still be made (ST-7), and raises if not.
    check_resend()
    # The wait and the fence can spend what was left (R24-08): nothing is
    # started without the time a generation needs (N11), past the one deadline
    # least of all, and the rate limit is then the answer.
    return deadline - _clock() >= MIN_RESEND_SECONDS


def _waited_out(failed: OpenAIError, sent: int, deadline: float) -> bool:
    """Whether a rate limit was waited out and the call may be sent again: a
    429 reached no model (DP-5), within the tries, and the wait still leaves
    `MIN_RESEND_SECONDS` of the call's one deadline (ST-9, N11) -- a wait that
    could not be followed by a re-send is not waited."""
    if not _rate_limited(failed) or sent >= RATE_LIMIT_TRIES:
        return False
    wait = _retry_after(failed)
    if wait + MIN_RESEND_SECONDS > deadline - _clock():
        return False
    _sleep(wait)
    return True


def _rate_limited(failed: OpenAIError) -> bool:
    return getattr(failed, "status_code", None) == RATE_LIMITED


def _retry_after(failed: OpenAIError) -> float:
    """The gateway's `Retry-After` in seconds, capped; the default otherwise."""
    headers = getattr(getattr(failed, "response", None), "headers", None)
    stated = headers.get("retry-after") if headers is not None else None
    try:
        seconds = float(stated) if isinstance(stated, str) else RETRY_AFTER_SECONDS
    except ValueError:
        seconds = RETRY_AFTER_SECONDS
    if not math.isfinite(seconds):
        # `nan` survives the clamp below and `sleep(nan)` raises (ST-8, MAX-16).
        seconds = RETRY_AFTER_SECONDS
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_SECONDS)


# LangChain fills an id the response did not carry with its own run id, so a
# message id with this prefix names nothing the provider said (F36).
_LANGCHAIN_RUN_PREFIX = "lc_run"


def _claimed_id(message: AIMessage) -> object:
    """The id the response itself carried, or None: the completion's id when
    the client surfaced it, else the message id unless LangChain minted it."""
    claimed = message.response_metadata.get("id")
    if isinstance(claimed, str) and claimed:
        return claimed
    own = message.id
    if isinstance(own, str) and own.startswith(_LANGCHAIN_RUN_PREFIX):
        return None
    return own


def _status_refusal(failed: OpenAIError) -> RefusalCode:
    """A vendor error by its status class alone; its message never travels."""
    status = getattr(failed, "status_code", None)
    if isinstance(status, int) and status in NEVER_RETRIED:
        return RefusalCode.PROVIDER_CALL_INVALID
    return RefusalCode.PROVIDER_UNAVAILABLE


def _text(content: object) -> str | None:
    """The answer as one string, with a single code fence unwrapped.

    A chat model asked for JSON may wrap it in a fence; the fence is transport
    dressing, not the answer, so it is removed here and nowhere else. The
    envelope is still parsed and validated by the host (invariant 3).
    """
    if isinstance(content, list):
        parts = [
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, (str, dict))
        ]
        content = "".join(parts)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1 and text.endswith("```"):
            text = text[first_newline + 1 : -3].strip()
    return text


def completions(
    price: ModelPrice,
    *,
    chat: BaseChatModel | None = None,
    endpoint: str | None = None,
    reasoning_effort: str | None = None,
    provider_order: tuple[str, ...] = (),
) -> ChatCompletions:
    """The provider a run executes through, priced for exactly its endpoint.

    `chat` is the test seam: a suite passes its own `BaseChatModel` and no
    OpenRouter client is built. Production passes nothing and gets
    `chat_model()`.
    """
    name = endpoint or configured_endpoint()
    if price.model != name:
        raise Refusal(RefusalCode.PROVIDER_NOT_CONFIGURED)
    return ChatCompletions(
        chat if chat is not None else chat_model(endpoint=name),
        name,
        price,
        reasoning_effort,
        provider_order,
    )


def from_environment() -> ChatCompletions:
    """The provider for the configured endpoint, or `PROVIDER_NOT_CONFIGURED`
    for it or for any malformed choice beside it (`model_choices`)."""
    endpoint = configured_endpoint()
    return completions(
        model_choices()[endpoint],
        endpoint=endpoint,
        reasoning_effort=reasoning_effort(),
        provider_order=provider_order(),
    )
