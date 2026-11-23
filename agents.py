#!/usr/bin/env python3
"""Minimal research agent: DeepSeek function-calling loop with web tools.

No OpenClaw dependency. Requires env DEEPSEEK_API_KEY.
Tools: http_get(url), web_search(query), finish(result_json).
"""
import json
import os
import re
import sys
import time
import urllib.error
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


def _get(url, timeout=25):
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


def _ddg(url):
    html = _get(url, timeout=15)
    rows = re.findall(r'href="(http[^"]+)"[^>]*>(.*?)</a>', html, re.S)
    out, seen = [], set()
    for href, title in rows:
        if "duckduckgo.com" in href or href in seen:
            continue
        seen.add(href)
        title = re.sub(r"(?s)<[^>]+>", "", title).strip()
        out.append("%s — %s" % (title[:120], href))
        if len(out) >= 8:
            break
    return out


def _searx(query):
    base = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8080").rstrip("/")
    data = _get(base + "/search?q=" + urllib.parse.quote(query) + "&format=json", timeout=8)
    items = json.loads(data).get("results", [])[:8]
    return ["%s — %s" % (re.sub(r"(?s)<[^>]+>", "", x.get("title", ""))[:120], x.get("url", ""))
            for x in items]


def web_search(query):
    q = urllib.parse.quote(query)
    errors = []
    try:
        out = _searx(query)
        if out:
            return "\n".join(out)
    except Exception as e:
        errors.append("searx: %s" % e)
    for url in ("https://lite.duckduckgo.com/lite/?q=" + q,
                "https://html.duckduckgo.com/html/?q=" + q):
        try:
            out = _ddg(url)
        except Exception as e:
            errors.append(str(e))
            continue
        if out:
            return "\n".join(out)
    return "no results" + (" (%s)" % "; ".join(errors[:1]) if errors else "")


def _post(body, key, timeout=300, retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            API,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1) + 2)
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


def run_agent(system_prompt, user_prompt, max_steps=10, temperature=0.2, schema_hint=""):
    key = os.environ["DEEPSEEK_API_KEY"]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    for step in range(max_steps):
        force_finish = step >= max_steps - 2
        body = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "temperature": temperature,
            "max_tokens": 8000,
        }
        if force_finish:
            body["tool_choice"] = {"type": "function", "function": {"name": "finish"}}
        data = _post(body, key)
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
    # Last resort: no tools, demand the JSON payload explicitly.
    messages.append({
        "role": "user",
        "content": ("Stop research now. Return ONLY the final JSON array for the required keys"
                    + (": " + schema_hint if schema_hint else "") +
                    ". No prose, no markdown."),
    })
    body = {"model": MODEL, "messages": messages, "temperature": 0, "max_tokens": 8000}
    data = _post(body, key)
    return (data["choices"][0]["message"].get("content") or "")


if __name__ == "__main__":
    print(run_agent(sys.argv[1], sys.argv[2])[:2000])
