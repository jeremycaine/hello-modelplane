#!/usr/bin/env python3
"""
A minimal LLM client that pulls a Wikipedia summary, sends it to the local Qwen model
served through Modelplane (kind + Traefik + vllm-metal), and prints the
model's answer.

If using podman locally ensure portforwarding is set up to reach the Modelplane gateway, e.g.:
    kubectl port-forward -n traefik-system svc/traefik 8080:80

Usage:
    python3 modelplane_agent.py "Earth"
    python3 modelplane_agent.py "Australia" --question "List 3 key facts"
"""

import argparse
import json
import sys
import urllib.request

# --- Config -----------------------------------------------------------
GATEWAY_URL = "http://localhost:8080/ml-team/qwen/chat/completions"  
MODEL_ID = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"


def fetch_wikipedia_summary(title: str) -> str:
    """Grab the plain-text extract from Wikipedia's summary endpoint."""
    url = WIKI_API.format(urllib.parse.quote(title))
    req = urllib.request.Request(url, headers={"User-Agent": "modelplane-agent-demo/0.1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    extract = data.get("extract")
    if not extract:
        raise ValueError(f"No summary found for '{title}'")
    return extract


def ask_model(context: str, question: str) -> str:
    """Send a chat completion request to the Modelplane-fronted Qwen model."""
    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise assistant. Base your answer only on the provided text.",
            },
            {
                "role": "user",
                "content": f"Text:\n{context}\n\nTask: {question}",
            },
        ],
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        GATEWAY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.load(resp)
    return result["choices"][0]["message"]["content"]


def main():
    parser = argparse.ArgumentParser(description="Summarize a Wikipedia topic via Modelplane/Qwen")
    parser.add_argument("topic", help="Wikipedia page title, e.g. 'Kubernetes'")
    parser.add_argument(
        "--question",
        default="Summarize this in 3 short bullet points.",
        help="What to ask the model about the text",
    )
    args = parser.parse_args()

    print(f"Fetching Wikipedia summary for '{args.topic}'...")
    try:
        text = fetch_wikipedia_summary(args.topic)
    except Exception as e:
        print(f"Failed to fetch Wikipedia summary: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n--- Source text ({len(text)} chars) ---\n{text}\n")

    print(f"Asking Qwen: {args.question}")
    try:
        answer = ask_model(text, args.question)
    except Exception as e:
        print(f"Failed to call Modelplane endpoint: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n--- Model response ---")
    print(answer)


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (kept near use for readability)
    main()