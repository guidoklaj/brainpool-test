"""Tools the agent can call. Local, fast, and deterministic on purpose.

Nothing here talks to the network -- the only network cost in a run is the
provider calls, so throughput numbers measure your limiter rather than these.
"""

from __future__ import annotations

_PASSAGES = {
    "quota policy": (
        "Quotas apply per API key and per model. Both a request budget and a "
        "token budget are enforced, and completion tokens count toward the "
        "token budget once the response is generated."
    ),
    "default": "No matching passages were found for that query.",
}


async def search_kb(query: str) -> str:
    return _PASSAGES.get(query, _PASSAGES["default"])


async def fetch_document(doc_id: str) -> str:
    return f"Document {doc_id}: quota headers are returned on every response, including rejections."


async def summarise(text: str) -> str:
    return f"Summary: {text[:80]}"


TOOLS = {
    "search_kb": search_kb,
    "fetch_document": fetch_document,
    "summarise": summarise,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "Search the knowledge base for passages matching a query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_document",
            "description": "Fetch the full text of a document by id.",
            "parameters": {
                "type": "object",
                "properties": {"doc_id": {"type": "string"}},
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarise",
            "description": "Summarise a passage of text.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]
