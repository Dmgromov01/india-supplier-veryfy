#!/usr/bin/env python3
"""Supplier verification runner (GitHub Actions friendly).

Each run processes a slice of queue.json (shard) and writes one JSON per
company+role into results/<role>/<slug>.json. Re-runs skip finished work.

Env:
  DEEPSEEK_API_KEY  required
  SHARD, SHARDS     slice of the queue (default 0 / 1)
  BATCH             companies per run (default 8)
  MAX_SECONDS       wall-clock budget (default 19800 = 5.5h)
  WORKERS           parallel agents (default 4)
"""
import csv
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from agents import run_agent

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "results")
ROLES = ("legal", "ship", "prod", "risk")

ROLE_TASKS = {
    "legal": """ROLE: legal/registry specialist ONLY.
For each company find: exact legal entity name, CIN/LLPIN, PAN (format and holder type from the 4th character),
GSTIN from a public profile, GST status, date of incorporation, registered-office state vs the state claimed
in the input, MCA status, role (manufacturer / trader / manufacturer_trader / service_provider / unknown),
and revenue FY25 and FY26 in INR crore ONLY from audited accounts or official filings - otherwise null.
Reject only if the entity does not exist, is struck off, liquidated or bankrupt.
Do NOT research customs shipments, courts or product catalogues.""",
    "ship": """ROLE: shipments evidence specialist ONLY. Rule 5: verify actual shipments, never claims.
For each company find (a) export records with date, destination, HS code and bill-of-lading number, and
(b) domestic supply evidence (GST turnover band, GeM/CPPP tenders, contracts, annual-report splits).
Start with https://importkey.com/countries/india/<slug> where <slug> is the company name in lower case with
dash-separated words; try two or three slug variants. volza, eximpedia, importgenius and trademo free views
are acceptable second sources.
Classify export_status and domestic_status as one of: confirmed / declared_only / none_found / unknown.
Always quote one exact evidence line and record the source counter versus the number of rows actually seen.
Registries and product catalogues are out of scope.""",
    "prod": """ROLE: product/HS specialist ONLY. Rule 3: an HS mismatch never rejects a company - keep it and correct the code.
For each company open its website or catalogue, list the real products, and map them to correct HS-6 codes.
Compare with the declared codes: exact / partial / wrong / cannot_verify.
Anything not corroborated by a customs line must be marked "заявлено, не подтверждено".
Note industry misclassification, trader signs and product lines missing from the declaration.
Do NOT research registries, revenue or customs volumes.""",
    "risk": """ROLE: risk specialist ONLY. Rule 2: sanctions, court cases and criminal matters are never rejection grounds -
keep the company and note them neutrally.
For each company collect as of today: sanctions exposure (OFAC SDN, UN, EU, UK, BIS entity list),
Indian court cases with party role and status, regulator actions and negative news with dates,
credit rating with date and status, and any evidence of supplies to Russia or the CIS.
Do NOT research legal identity, HS codes or shipment volumes.""",
}

SCHEMA = {
    "legal": ["name_input", "legal_name", "cin_llpin", "pan", "pan_holder_type", "gstin", "gst_status",
              "incorporation_date", "state_registered", "state_claimed", "mca_status", "role",
              "revenue_fy25_cr", "revenue_fy26_cr", "revenue_source", "note"],
    "ship": ["name_input", "export_status", "export_evidence", "domestic_status", "domestic_evidence", "note"],
    "prod": ["name_input", "declared_hs", "actual_hs_list", "match_status", "product_summary", "role_hint", "comment"],
    "risk": ["name_input", "sanctions_hit", "court_cases", "rating", "news_risk", "rf_supply", "note"],
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:80] or "unknown"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def done_set():
    done = set()
    for role in ROLES:
        d = os.path.join(RES, role)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".json"):
                done.add(f[:-5])
    return done


def role_done(company_slug, role):
    return os.path.exists(os.path.join(RES, role, company_slug + ".json"))


def save(role, company_slug, payload, failed=False):
    d = os.path.join(RES, "_failed", role) if failed else os.path.join(RES, role)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, company_slug + ".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, company_slug + ".json"))


def company_prompt(company, role):
    hs = company.get("hs") or []
    if isinstance(hs, list):
        hs = ";".join(hs)
    return (
        "Companies in this batch (JSON array). Work through every one of them, do not skip any:\n"
        + json.dumps([{
            "name": company.get("name"),
            "legal_claimed": company.get("legal"),
            "declared_hs": hs,
            "industry_claimed": company.get("industry"),
            "pan_claimed": company.get("pan"),
            "state_claimed": company.get("state"),
        }], ensure_ascii=False, indent=1)
        + "\n\nWhen finished, call the finish tool with json_payload = a JSON array, one object per company, "
          "with exactly these keys: " + ", ".join(SCHEMA[role]) + "."
    )


def parse_payload(text):
    if not text:
        return []
    text = text.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def work(company, role, rules):
    slug_c = slug(company.get("name"))
    system = (
        "You are a meticulous supplier-verification researcher working on Indian companies.\n"
        "Rules of the project:\n" + rules + "\n\n" + ROLE_TASKS[role] +
        "\n\nEvery claim needs a source. Never invent numbers, CINs, PANs or shipment records. "
        "If something is not public, use null and say so in the note. "
        "Use the tools: web_search to find pages, http_get to read them. Keep to at most 10 tool calls and call finish early."
    )
    raw = run_agent(system, company_prompt(company, role), schema_hint=", ".join(SCHEMA[role]))
    rows = parse_payload(raw)
    row = rows[0] if rows else {}
    if not row:
        row = {"name_input": company.get("name"), "note": "agent returned no parsable JSON", "_failed": True}
        save(role, slug_c, row, failed=True)
        return slug_c, role, True
    row.setdefault("name_input", company.get("name"))
    save(role, slug_c, row)
    return slug_c, role, False


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is not set — refusing to run", file=sys.stderr)
        return 2
    shard = int(os.environ.get("SHARD", "0"))
    shards = max(1, int(os.environ.get("SHARDS", "1")))
    batch = int(os.environ.get("BATCH", "8"))
    budget = int(os.environ.get("MAX_SECONDS", "19800"))
    workers = int(os.environ.get("WORKERS", "4"))
    started = time.time()

    rules = open(os.path.join(ROOT, "RULES.md"), encoding="utf-8").read()
    queue = load_json(os.path.join(ROOT, "queue.json"), [])
    done = done_set()

    preflight = "skipped"
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.deepseek.com/models",
            headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            preflight = "ok" if r.status == 200 else "http %s" % r.status
    except Exception as e:
        preflight = "error: %s" % str(e)[:200]
    print("preflight:", preflight)

    picked = []
    for idx, c in enumerate(queue):
        if idx % shards != shard:
            continue
        s = slug(c.get("name"))
        if s in done:
            continue
        if all(role_done(s, r) for r in ROLES):
            continue
        picked.append(c)
        if len(picked) >= batch:
            break

    if not picked:
        print("shard %d: nothing left in this slice" % shard)
        return 0

    print("shard %d/%d: %d companies, workers=%d" % (shard, shards, len(picked), workers))
    failures, errors = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for c in picked:
            for role in ROLES:
                if role_done(slug(c.get("name")), role):
                    continue
                futures.append(pool.submit(work, c, role, rules))
        for fut in as_completed(futures):
            if time.time() - started > budget:
                print("time budget reached, stopping")
                break
            try:
                s, role, failed = fut.result()
                failures += 1 if failed else 0
                print("  ok %-45s %s" % (s[:45], role))
            except Exception:
                failures += 1
                tb = traceback.format_exc(limit=2)
                if len(errors) < 8:
                    errors.append(tb.strip().splitlines()[-1][:300])
                print("  fail: %s" % tb)
    print("done, failures=%d, elapsed=%.0fs" % (failures, time.time() - started))

    # Diagnostics are committed with the results so failures are visible without job logs.
    diag = {
        "shard": shard, "shards": shards, "picked": len(picked),
        "roles_submitted": len(futures), "failures": failures,
        "api_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "api_key_len": len(os.environ.get("DEEPSEEK_API_KEY", "")),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "search": os.environ.get("SEARXNG_URL", "duckduckgo"),
        "preflight": preflight,
        "elapsed_s": round(time.time() - started), "errors": errors,
        "worker": "github" if os.environ.get("GITHUB_ACTIONS") else "local",
    }
    d = os.path.join(RES, "_runlog")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "shard-%02d.json" % shard), "w", encoding="utf-8") as fh:
        json.dump(diag, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
