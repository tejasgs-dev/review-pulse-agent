import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from pulse.analysis.llm import FALLBACK_BETA, LlmClient, LlmError, LlmRefusal
from pulse.config import LlmConfig


class Out(BaseModel):
    answer: str


def message(text='{"answer": "yes"}', stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        id="msg_1", _request_id="req_1", model="claude-opus-5-5", stop_reason=stop_reason,
        stop_details=stop_details, content=[SimpleNamespace(type="thinking", thinking=""),
                                            SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
                              cache_creation_input_tokens=None),
    )


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeSdk:
    """Mimics `client.beta.messages.stream(**kwargs)`."""

    def __init__(self, msg=None, error=None):
        self.kwargs = None
        self._msg, self._error = msg or message(), error
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.kwargs = kwargs
        if self._error:
            raise self._error
        return FakeStream(self._msg)


def client(sdk, tmp_path=None, **cfg):
    return LlmClient(LlmConfig(model="claude-opus-5-5", **cfg), client=sdk,
                     log_dir=tmp_path)


def call(llm, **kw):
    return llm.parse(name="discover", system="SYS", prefix=kw.pop("prefix", "PREFIX"),
                     user="USER", schema=Out, effort="high", **kw)


def test_request_shape(tmp_path):
    sdk = FakeSdk()
    assert call(client(sdk)).answer == "yes"
    k = sdk.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["thinking"] == {"type": "adaptive"}
    assert k["output_config"]["effort"] == "high"
    assert k["output_config"]["format"]["type"] == "json_schema"
    assert k["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert k["fallbacks"] == "default" and k["betas"] == [FALLBACK_BETA]
    assert k["system"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = k["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "PREFIX", "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": "USER"}
    assert "temperature" not in k and "output_format" not in k


def test_no_prefix_and_no_fallbacks():
    sdk = FakeSdk()
    call(client(sdk, fallbacks=None), prefix=None)
    assert "fallbacks" not in sdk.kwargs and "betas" not in sdk.kwargs
    assert sdk.kwargs["messages"][0]["content"] == [{"type": "text", "text": "USER"}]


def test_usage_and_log(tmp_path):
    llm = client(FakeSdk(), tmp_path)
    call(llm)
    call(llm)
    assert llm.usage.calls == 2 and llm.usage.input_tokens == 200
    assert llm.usage.cache_read_tokens == 100 and llm.usage.cache_write_tokens == 0
    assert llm.usage.models == {"claude-opus-5-5": 2}
    assert llm.usage.estimated_usd > 0
    logs = sorted(p.name for p in tmp_path.iterdir())
    assert logs == ["01_discover.json", "02_discover.json"]
    data = json.loads((tmp_path / logs[0]).read_text(encoding="utf-8"))
    assert data["response"]["output"] == {"answer": "yes"}
    assert data["response"]["stop_reason"] == "end_turn"
    assert data["request"]["effort"] == "high"


def test_refusal_is_checked_before_content():
    details = SimpleNamespace(type="refusal", category="cyber", explanation="x")
    llm = client(FakeSdk(message(text="", stop_reason="refusal", stop_details=details)))
    with pytest.raises(LlmRefusal) as e:
        call(llm)
    assert e.value.category == "cyber" and e.value.kind == "refusal"


def test_max_tokens_is_reported_as_truncation():
    llm = client(FakeSdk(message(text='{"answ', stop_reason="max_tokens")))
    with pytest.raises(LlmError, match="max_tokens") as e:
        call(llm)
    assert e.value.kind == "max_tokens"


def test_schema_mismatch():
    with pytest.raises(LlmError) as e:
        call(client(FakeSdk(message(text='{"wrong": 1}'))))
    assert e.value.kind == "schema"


def _status_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=req), body=None)


@pytest.mark.parametrize(("error", "kind", "retryable"), [
    (_status_error(anthropic.RateLimitError, 429), "rate_limit", True),
    (_status_error(anthropic.InternalServerError, 500), "api_status", True),
    (_status_error(anthropic.BadRequestError, 400), "api_status", False),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "connection",
     True),
])
def test_api_errors_are_classified(error, kind, retryable):
    with pytest.raises(LlmError) as e:
        call(client(FakeSdk(error=error)))
    assert e.value.kind == kind and e.value.retryable is retryable
