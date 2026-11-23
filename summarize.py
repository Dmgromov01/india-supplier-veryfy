#!/usr/bin/env python3
"""Consolidate results/<role>/<slug>.json into one table.

Outputs:
  results_summary.csv  — one row per company with the key fields of all four roles
  summary.md           — coverage counts and the main gaps
Re-runnable; reads whatever is present.
"""
import csv
import glob
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, 'results')
ROLES = ('legal', 'ship', 'prod', 'risk')


def load(role):
    out = {}
    for path in glob.glob(os.path.join(RES, role, '*.json')):
        slug = os.path.basename(path)[:-5]
        try:
            out[slug] = json.load(open(path, encoding='utf-8'))
        except Exception:
            out[slug] = {'_failed': True, 'note': 'unreadable json'}
    return out


def main():
    data = {r: load(r) for r in ROLES}
    failed = {r: len(glob.glob(os.path.join(RES, '_failed', r, '*.json'))) for r in ROLES}
    slugs = sorted(set().union(*[set(d) for d in data.values()]))
    if not slugs:
        print('no results yet')
        return 0

    rows = []
    for s in slugs:
        L = data['legal'].get(s, {})
        S = data['ship'].get(s, {})
        P = data['prod'].get(s, {})
        R = data['risk'].get(s, {})
        rows.append({
            'company': L.get('name_input') or S.get('name_input') or P.get('name_input') or R.get('name_input') or s,
            'legal_name': L.get('legal_name') or '',
            'cin': L.get('cin_llpin') or '',
            'pan': L.get('pan') or '',
            'gstin': L.get('gstin') or '',
            'state_registered': L.get('state_registered') or '',
            'mca_status': L.get('mca_status') or '',
            'role': L.get('role') or '',
            'revenue_fy25_cr': L.get('revenue_fy25_cr') or '',
            'revenue_fy26_cr': L.get('revenue_fy26_cr') or '',
            'export_status': S.get('export_status') or '',
            'export_evidence': (S.get('export_evidence') or '')[:300],
            'domestic_status': S.get('domestic_status') or '',
            'declared_hs': P.get('declared_hs') or '',
            'actual_hs': P.get('actual_hs_list') or '',
            'hs_match': P.get('match_status') or '',
            'product_summary': (P.get('product_summary') or '')[:300],
            'role_hint': P.get('role_hint') or '',
            'sanctions': R.get('sanctions_hit') or '',
            'court_cases': (R.get('court_cases') or '')[:200],
            'rf_supply': R.get('rf_supply') or '',
            'roles_done': '+'.join(r for r in ROLES if s in data[r]),
            'notes': ' | '.join(x for x in ((L.get('note') or ''), (S.get('note') or ''),
                                            (P.get('comment') or ''), (R.get('note') or '')) if x)[:800],
        })

    out = os.path.join(ROOT, 'results_summary.csv')
    with open(out, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=';')
        w.writeheader()
        w.writerows(rows)

    complete = sum(1 for r in rows if len(r['roles_done'].split('+')) == 4)
    exp = Counter(r['export_status'] or 'unknown' for r in rows)
    hs = Counter(r['hs_match'] or 'unknown' for r in rows)
    roles = Counter(r['role'] or 'unknown' for r in rows)
    lines = [
        '# Supplier verification summary', '',
        f'Companies in results: **{len(rows)}**, fully covered (4/4 roles): **{complete}**', '',
        '| Role files | count |', '|---|---|',
    ]
    for r in ROLES:
        lines.append(f'| {r} | {len(data[r])} |')
    lines += ['| failed (quarantined) | ' + str(sum(failed.values())) + ' |', '',
              '## Export evidence', '', '| status | companies |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in exp.most_common()]
    lines += ['', '## HS match', '', '| status | companies |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in hs.most_common()]
    lines += ['', '## Company role', '', '| role | companies |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in roles.most_common()]
    open(os.path.join(ROOT, 'summary.md'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print(f'{out}: {len(rows)} rows, complete {complete}, failed {sum(failed.values())}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
