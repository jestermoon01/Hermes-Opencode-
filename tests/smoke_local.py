"""Opt-in live test: python tests/smoke_local.py (requires the gateway running)."""

import json

import httpx

URL = "http://127.0.0.1:8787/v1/chat/completions"


def main():
    with httpx.Client(timeout=180, trust_env=False) as client:
        response = client.post(
            URL,
            json={
                "model": "hermes-local",
                "messages": [
                    {"role": "user", "content": "Reply exactly MODEL_OK. No explanation."}
                ],
                "max_tokens": 1024,
                "temperature": 0,
            },
        )
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"].strip()
        assert answer == "MODEL_OK", answer
        print("JSON completion: MODEL_OK", flush=True)
        text = ""
        with client.stream(
            "POST",
            URL,
            json={
                "model": "hermes-local",
                "messages": [
                    {"role": "user", "content": "Reply exactly STREAM_OK. No explanation."}
                ],
                "max_tokens": 1024,
                "temperature": 0,
                "stream": True,
            },
        ) as stream:
            stream.raise_for_status()
            done = False
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                event = json.loads(payload)
                assert "error" not in event, event
                for choice in event.get("choices", []):
                    text += choice.get("delta", {}).get("content") or ""
            assert done and text.strip() == "STREAM_OK", text
        print("SSE completion: STREAM_OK", flush=True)
        messages = [
            {
                "role": "user",
                "content": "Use the add_numbers tool to add 17 and 25. After getting its result, "
                "reply with only the resulting number.",
            }
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "add_numbers",
                    "description": "Adds two integers.",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"],
                    },
                },
            }
        ]
        result = client.post(
            URL,
            json={
                "model": "hermes-local",
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0,
                "max_tokens": 1024,
            },
        )
        result.raise_for_status()
        message = result.json()["choices"][0]["message"]
        assert message.get("tool_calls"), message
        messages.append(message)
        for call in message["tool_calls"]:
            assert call["function"]["name"] == "add_numbers"
            arguments = json.loads(call["function"]["arguments"])
            value = arguments["a"] + arguments["b"]
            assert value == 42
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(value)})
        final = client.post(
            URL,
            json={
                "model": "hermes-local",
                "messages": messages,
                "tools": tools,
                "tool_choice": "none",
                "temperature": 0,
                "max_tokens": 1024,
            },
        )
        final.raise_for_status()
        answer = final.json()["choices"][0]["message"]["content"].strip()
        assert answer == "42", answer
        print("Tool call + result + final completion: 42", flush=True)


if __name__ == "__main__":
    main()
