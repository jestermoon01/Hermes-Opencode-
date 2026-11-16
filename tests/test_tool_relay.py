"""Exercise both sides of the tool relay without using a hosted model or real memory."""

import json

import pytest
from fastapi.testclient import TestClient

from hermes_gateway import opencode_agent as native
from hermes_gateway.tool_relay import ToolRelay

MEMORY = {
    "type": "function",
    "function": {
        "name": "memory",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"enum": ["add"]},
                "content": {"type": "string"},
            },
            "required": ["action", "content"],
            "additionalProperties": False,
        },
    },
}


def request(**kwargs):
    return {
        "messages": [
            {"role": "system", "content": "Stored preference: use pytest."},
            {"role": "user", "content": "Remember I prefer uv."},
        ],
        "tools": [MEMORY],
        **kwargs,
    }


def envelope(nonce, **overrides):
    return json.dumps(
        {
            "bridge_nonce": nonce,
            "content": "Saving your preference in Hermes.",
            "tool_calls": [{"name": "memory", "arguments": {"action": "add", "content": "Use uv"}}],
            **overrides,
        }
    )


@pytest.mark.parametrize("stream", [False, True])
def test_memory_request_result_and_answer_round_trip(tmp_path, monkeypatch, stream):
    prompts = []

    async def events(settings, command, prompt):
        prompts.append(prompt)
        contract = json.loads(prompt.split("\n\n")[-1].split("\n")[-1])
        yield {"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed"}}}
        if len(prompts) == 1:
            text = envelope(contract["bridge_nonce"])
        else:
            assert '"tool_call_id": "call_' in prompt
            assert "MEMORY_SAVED_OK" in prompt
            text = "Your preference was saved by Hermes."
        yield {"type": "text", "part": {"text": text}}

    monkeypatch.setattr(native, "agent_events", events)
    settings = native.AgentSettings(
        workdir=tmp_path, log_file=tmp_path / "usage.jsonl", hermes_tools=True
    )
    with TestClient(native.create_agent_app(settings, "unused.exe")) as client:
        payload = request(stream=stream)
        response = client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200
        if stream:
            frames = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: {")
            ]
            choices = [c for frame in frames for c in frame.get("choices", [])]
            assert choices[-1]["finish_reason"] == "tool_calls"
            call = next(
                c["delta"]["tool_calls"][0] for c in choices if "tool_calls" in c.get("delta", {})
            )
            call.pop("index")
            # The control envelope must never leak into the assistant's chat text.
            text = "".join(c.get("delta", {}).get("content", "") for c in choices)
            assert "bridge_nonce" not in text
            assert "[OpenCode]" not in text
            assert "Completed: read" not in text
            assert "Saving your preference in Hermes." in text
        else:
            choice = response.json()["choices"][0]
            assert choice["finish_reason"] == "tool_calls"
            call = choice["message"]["tool_calls"][0]
        assert call["function"]["name"] == "memory"
        assert json.loads(call["function"]["arguments"])["content"] == "Use uv"
        payload["stream"] = False
        payload["messages"] += [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": "MEMORY_SAVED_OK"},
        ]
        final = client.post("/v1/chat/completions", json=payload).json()["choices"][0]
        assert final["finish_reason"] == "stop"
        assert final["message"]["content"] == "Your preference was saved by Hermes."
    assert "Stored preference: use pytest." in prompts[0]
    assert len(prompts) == 2
    assert "Use uv" not in settings.log_file.read_text()


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_calls": [{"name": "terminal", "arguments": {"command": "whoami"}}]},
        {"tool_calls": [{"name": "memory", "arguments": {"action": "add", "content": 42}}]},
        {"tool_calls": [{"name": "memory", "arguments": {"action": "add"}}]},
        {"bridge_nonce": "wrong"},
        {"tool_calls": []},
    ],
)
def test_invalid_tool_output_is_rejected(overrides):
    relay = ToolRelay(request())
    with pytest.raises(ValueError):
        relay.decode(envelope(relay.nonce, **overrides))


def test_tool_choice_constraints():
    relay = ToolRelay(request(tool_choice="none"))
    with pytest.raises(ValueError):
        relay.decode(envelope(relay.nonce))
    relay = ToolRelay(request(tool_choice="required"))
    with pytest.raises(ValueError):
        relay.decode("I saved it without using a tool")
    relay = ToolRelay(request(parallel_tool_calls=False))
    calls = json.loads(envelope(relay.nonce))["tool_calls"] * 2
    with pytest.raises(ValueError):
        relay.decode(envelope(relay.nonce, tool_calls=calls))


def test_unsupported_tools_and_remote_schema_refs_are_rejected():
    for tool in [
        {"type": "function", "function": {"name": "terminal"}},
        {
            "type": "function",
            "function": {
                "name": "memory",
                "parameters": {"$ref": "http://127.0.0.1/private"},
            },
        },
    ]:
        with pytest.raises(ValueError):
            ToolRelay(request(tools=[tool]))


def test_skill_creation_is_a_real_hermes_tool_request():
    relay = ToolRelay(
        request(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "skill_manage",
                        "parameters": {
                            "type": "object",
                            "required": ["action", "name", "content"],
                        },
                    },
                }
            ]
        )
    )
    message, reason = relay.decode(
        envelope(
            relay.nonce,
            tool_calls=[
                {
                    "name": "skill_manage",
                    "arguments": {"action": "create", "name": "uv-workflow", "content": "# Use uv"},
                }
            ],
        )
    )
    assert reason == "tool_calls"
    assert message["tool_calls"][0]["function"]["name"] == "skill_manage"


@pytest.mark.parametrize("choice", [[], {}, {"type": "function", "function": None}])
def test_malformed_tool_choices_are_rejected(choice):
    with pytest.raises(ValueError):
        ToolRelay(request(tool_choice=choice))


def test_mixed_protocol_output_is_not_displayed_as_a_normal_reply():
    relay = ToolRelay(request())
    with pytest.raises(ValueError):
        relay.decode("Here is my request:\n" + envelope(relay.nonce))


def test_worker_tool_discovery_is_allowed_only_with_workers_enabled():
    payload = request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "tool_search",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
    )
    with pytest.raises(ValueError, match="tool_search"):
        ToolRelay(payload)
    relay = ToolRelay(payload, allow_workers=True)
    message, reason = relay.decode(
        envelope(
            relay.nonce,
            tool_calls=[
                {
                    "name": "tool_search",
                    "arguments": {"query": "spawn OpenCode workers"},
                }
            ],
        )
    )
    assert reason == "tool_calls"
    assert message["tool_calls"][0]["function"]["name"] == "tool_search"


def deferred_payload(**overrides):
    return request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "tool_describe",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "names": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["names"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tool_call",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "calls": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "arguments": {"type": "object"},
                                    },
                                    "required": ["name", "arguments"],
                                },
                            }
                        },
                        "required": ["calls"],
                    },
                },
            },
        ],
        **overrides,
    )


@pytest.mark.parametrize("name", ["tool_describe", "tool_call"])
def test_native_deferred_worker_bridge_is_supported(name):
    from hermes_gateway.workers import PREFIX

    relay = ToolRelay(deferred_payload(), allow_workers=True)
    target = PREFIX + "spawn_worker"
    arguments = (
        {"names": [target]}
        if name == "tool_describe"
        else {
            "calls": [{"name": target, "arguments": {"task": "Investigate"}}],
        }
    )
    message, reason = relay.decode(
        envelope(
            relay.nonce,
            tool_calls=[
                {
                    "name": name,
                    "arguments": arguments,
                }
            ],
        )
    )
    assert reason == "tool_calls"
    assert message["tool_calls"][0]["function"]["name"] == name


@pytest.mark.parametrize("name", ["tool_describe", "tool_call"])
def test_deferred_bridge_does_not_allow_unsupported_targets(name):
    relay = ToolRelay(deferred_payload(), allow_workers=True)
    arguments = (
        {"names": ["terminal"]}
        if name == "tool_describe"
        else {
            "calls": [{"name": "terminal", "arguments": {"command": "whoami"}}],
        }
    )
    with pytest.raises(ValueError, match="unsupported tool"):
        relay.decode(envelope(relay.nonce, tool_calls=[{"name": name, "arguments": arguments}]))


def test_deferred_worker_arguments_and_parallel_constraints_are_checked():
    from hermes_gateway.workers import PREFIX

    relay = ToolRelay(deferred_payload(parallel_tool_calls=False), allow_workers=True)
    call = {"name": PREFIX + "spawn_worker", "arguments": {"task": "Investigate"}}
    with pytest.raises(ValueError, match="count"):
        relay.decode(
            envelope(
                relay.nonce,
                tool_calls=[
                    {
                        "name": "tool_call",
                        "arguments": {"calls": [call, call]},
                    }
                ],
            )
        )
    with pytest.raises(ValueError, match="arguments"):
        relay.decode(
            envelope(
                relay.nonce,
                tool_calls=[
                    {
                        "name": "tool_call",
                        "arguments": {"calls": [{**call, "arguments": {"task": 42}}]},
                    }
                ],
            )
        )


@pytest.mark.parametrize("stream", [False, True])
def test_final_step_protocol_survives_earlier_coding_narration(tmp_path, monkeypatch, stream):
    async def events(settings, command, prompt):
        contract = json.loads(prompt.split("\n\n")[-1].split("\n")[-1])
        yield {"type": "step_start", "part": {}}
        yield {"type": "text", "part": {"text": "Checking the project first."}}
        yield {"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed"}}}
        yield {"type": "step_start", "part": {}}
        yield {"type": "text", "part": {"text": envelope(contract["bridge_nonce"])}}

    monkeypatch.setattr(native, "agent_events", events)
    settings = native.AgentSettings(
        workdir=tmp_path, log_file=tmp_path / "usage.jsonl", hermes_tools=True
    )
    with TestClient(native.create_agent_app(settings, "unused.exe")) as client:
        response = client.post("/v1/chat/completions", json=request(stream=stream))
    assert response.status_code == 200
    if stream:
        assert '"finish_reason": "tool_calls"' in response.text
        assert "Checking the project first" not in response.text
    else:
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
