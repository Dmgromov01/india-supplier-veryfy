#!/usr/bin/env python3
"""Minimal research agent: DeepSeek function-calling loop with web tools.

No OpenClaw dependency. Requires env DEEPSEEK_API_KEY.
Tools: http_get(url), web_search(query), finish(result_json).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

API = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Fetch a URL and return plain text (HTML stripped, truncated to 6000 chars).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "DuckDuckGo search. Returns top 8 result titles, urls and snippets.",
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
            "name": "finish",
            "description": "Return the final JSON result for all companies in the batch.",
            "parameters": {
                "type": "object",
                "properties": {"json_payload": {"type": "string"}},
                "required": ["json_payload"],
            },
        },
    },
]


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        txt = raw.decode("utf-8", "replace")
    except Exception:
        txt = str(raw[:2000])
    return txt


def http_get(url):
    if not url.startswith("http"):
        return "error: url must start with http"
    try:
        txt = _get(url)
    except Exception as e:
        return "fetch_error: %s" % e
    txt = re.sub(r"(?is)<script.*?</script>", " ", txt)
    txt = re.sub(r"(?is)<style.*?</style>", " ", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt[:6000]


def web_search(query):
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    try:
        html = _get(url)
    except Exception as e:
        return "search_error: %s" % e
    rows = re.findall(r'href="(http[^"]+)"[^>]*>(.*?)</a>', html, re.S)
    out = []
    seen = set()
    for href, title in rows:
        if "duckduckgo.com" in href or href in seen:
            continue
        seen.add(href)
        title = re.sub(r"(?s)<[^>]+>", "", title).strip()
        out.append("%s — %s" % (title[:120], href))
        if len(out) >= 8:
            break
    return "\n".join(out) or "no results"


def run_agent(system_prompt, user_prompt, max_steps=28, temperature=0.2):
    key = os.environ["DEEPSEEK_API_KEY"]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    for _ in range(max_steps):
        body = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "temperature": temperature,
            "max_tokens": 8000,
        }
        req = urllib.request.Request(
            API,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        messages.append(msg)
        if not calls:
            # model answered in prose: treat as finished
            return msg.get("content") or ""
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            if name == "finish":
                return args.get("json_payload") or ""
            if name == "http_get":
                result = http_get(args.get("url", ""))
            elif name == "web_search":
                result = web_search(args.get("query", ""))
            else:
                result = "unknown tool"
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result[:6000],
            })
    return ""


if __name__ == "__main__":
    print(run_agent(sys.argv[1], sys.argv[2])[:2000])
