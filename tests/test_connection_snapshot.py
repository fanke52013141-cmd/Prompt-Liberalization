import json
import pytest


def test_frozen_connection_keeps_model_and_rotates_only_matching_endpoint_secret(client):
    from prompt_lib.db import get_db
    from prompt_lib.engine import get_connection
    from prompt_lib.core import BizError
    db = get_db()
    connection = {"id": "snapshot_test", "provider": "openai_compatible", "base_url": "https://example.invalid/v1", "model": "new", "api_key": "rotated"}
    db.execute("UPDATE settings SET value_json=? WHERE key='connections'", (json.dumps([connection]),))
    config = {"connection_id": connection["id"], "connection_snapshot": {"id": connection["id"],
              "provider": "openai_compatible", "base_url": connection["base_url"], "model": "old"}}
    resolved = get_connection(config)
    assert resolved["model"] == "old"
    assert resolved["api_key"] == "rotated"
    connection["base_url"] = "https://different.invalid/v1"
    db.execute("UPDATE settings SET value_json=? WHERE key='connections'", (json.dumps([connection]),))
    with pytest.raises(BizError) as error:
        get_connection(config)
    assert error.value.code == "CONNECTION_ENDPOINT_CHANGED"
