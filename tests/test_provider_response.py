import io
import json
from unittest.mock import patch

import pytest

from prompt_lib.providers import OpenAICompatProvider, ProviderError


def complete(response):
    provider = OpenAICompatProvider({"base_url": "https://provider.example/v1", "api_key": "test"})
    content = response if isinstance(response, bytes) else json.dumps(response).encode()
    with patch("prompt_lib.providers.urllib.request.urlopen", return_value=io.BytesIO(content)):
        return provider.complete("generation", "model", [], {"max_tokens": 10}, "hash")


def test_missing_usage_and_finish_remain_unknown():
    result = complete({"choices": [{"message": {"content": "answer"}}]})
    assert result.text == "answer"
    assert result.finish == "unknown"
    assert result.usage == {"in": None, "out": None}


@pytest.mark.parametrize("response", [b"bad JSON", [], {}, {"choices": []},
                                       {"choices": [None]}, {"choices": [{"message": {"content": {}}}]}])
def test_malformed_response_is_explicit_failure(response):
    with pytest.raises(ProviderError) as error:
        complete(response)
    assert error.value.code == "INVALID_RESPONSE"


def test_declared_unsupported_parameter_rejected_before_dispatch():
    provider = OpenAICompatProvider({"base_url": "https://provider.example/v1",
                                     "unsupported_params": ["seed"]})
    with patch("prompt_lib.providers.urllib.request.urlopen") as transport:
        with pytest.raises(ProviderError) as error:
            provider.complete("generation", "m", [], {"seed": 1}, "hash")
        assert error.value.code == "PARAM_UNSUPPORTED"
        transport.assert_not_called()
