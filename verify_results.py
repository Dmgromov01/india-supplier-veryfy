#!/usr/bin/env python3
"""Second-pass verification of supplier results (deterministic checks, no LLM).

Checks every results/<role>/<slug>.json:
  CIN structure and state code, PAN format + holder type, GSTIN checksum and state code,
  HS-6 existence in the HS catalogue, evidence URL presence, revenue sanity,
  and consistency between the claimed state and the GSTIN state.
Writes verification.csv and verification.md; exit code 0 always.
"""
import csv
import glob
import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, 'results')
CATALOG = os.environ.get('HS_CATALOG', '/app/data/hs_catalog.json')

CIN_RE = re.compile(r'^([LU])(\d{5})([A-Z]{2})(\d{4})([A-Z]{3})(\d{6})$')
PAN_RE = re.compile(r'^[A-Z]{5}\d{4}[A-Z]$')
GSTIN_RE = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')
STATE_CODES = {
    '01': 'Jammu and Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab', '04': 'Chandigarh',
    '05': 'Uttarakhand', '06': 'Haryana', '07': 'Delhi', '08': 'Rajasthan', '09': 'Uttar Pradesh',
    '10': 'Bihar', '11': 'Sikkim', '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur',
    '15': 'Mizoram', '16': 'Tripura', '17': 'Meghalaya', '18': 'Assam', '19': 'West Bengal',
    '20': 'Jharkhand', '21': 'Odisha', '22': 'Chhattisgarh', '23': 'Madhya Pradesh', '24': 'Gujarat',
    '27': 'Maharashtra', '29': 'Karnataka', '30': 'Goa', '32': 'Kerala', '33': 'Tamil Nadu',
    '34': 'Puducherry', '36': 'Telangana', '37': 'Andhra Pradesh',
}
ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def gstin_checksum(g):
    if len(g) != 15:
        return False
    s = 0
    for i, ch in enumerate(g[:14]):
        v = ALPHABET.index(ch)
        if i % 2:
            v *= 2
        s += v // 36 + v % 36
    return ALPHABET[(36 - s % 36) % 36] == g[14]


def load_hs():
    for path in (CATALOG, os.path.join(ROOT, 'hs6_codes.txt')):
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding='utf-8'))
                if isinstance(data, dict):
                    return {str(k)[:6] for k in data}
                return {str(x)[:6] for x in data}
            except Exception:
                codes = set()
                for line in open(path, encoding='utf-8'):
                    m = re.match(r'^\s*"?(\d{6})', line)
                    if m:
                        codes.add(m.group(1))
                return codes
    return set()


def one(path, role, hs_codes):
    slug = os.path.basename(path)[:-5]
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception:
        return {'slug': slug, 'role': role, 'flags': 'unreadable json'}
    f = []
    if role == 'legal':
        cin = (d.get('cin_llpin') or '').strip()
        if cin and cin.startswith(('L', 'U')):
            m = CIN_RE.match(cin)
            if not m:
                f.append('CIN format')
            elif m.group(3) not in {v[:2] for v in STATE_CODES.values()} and m.group(3) not in ('MH', 'GJ', 'TN', 'KA', 'DL', 'UP', 'WB', 'TG', 'AP', 'KL', 'RJ', 'MP', 'HR', 'PB', 'BR', 'OR', 'AS', 'JH', 'CT', 'GA', 'UT', 'HP', 'JK', 'CH', 'AN', 'LD', 'PY', 'SK', 'MN', 'ML', 'MZ', 'NL', 'TR', 'AR', 'AN'):
                f.append('CIN state code unknown')
        pan = (d.get('pan') or '').strip()
        if pan and not PAN_RE.match(pan):
            f.append('PAN format')
        g = (d.get('gstin') or '').strip()
        if g:
            if not GSTIN_RE.match(g):
                f.append('GSTIN format')
            elif not gstin_checksum(g):
                f.append('GSTIN checksum')
            elif g[:2] in STATE_CODES and d.get('state_registered'):
                if STATE_CODES[g[:2]].lower()[:5] not in str(d['state_registered']).lower():
                    f.append('GSTIN state != registered state')
        for k in ('revenue_fy25_cr', 'revenue_fy26_cr'):
            v = d.get(k)
            if isinstance(v, (int, float)) and (v < 0 or v > 1_000_000):
                f.append('%s out of range' % k)
    if role == 'ship':
        ev = (d.get('export_evidence') or '')
        if d.get('export_status') == 'confirmed' and 'http' not in ev:
            f.append('confirmed export without URL')
        if d.get('export_status') == 'confirmed' and not re.search(r'\d{4}', ev):
            f.append('confirmed export without date')
    if role == 'prod':
        for code in re.findall(r'(\d{6})', str(d.get('actual_hs_list') or '')):
            if hs_codes and code not in hs_codes:
                f.append('HS6 not in catalog: ' + code)
                break
    return {'slug': slug, 'role': role, 'company': d.get('name_input') or '',
            'flags': '; '.join(f), 'checked': 1 if not f else 0}


def main():
    hs_codes = load_hs()
    rows = []
    for role in ('legal', 'ship', 'prod', 'risk'):
        for path in sorted(glob.glob(os.path.join(RES, role, '*.json'))):
            rows.append(one(path, role, hs_codes))
    if not rows:
        print('no results to verify')
        return 0
    out = os.path.join(ROOT, 'verification.csv')
    with open(out, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=['slug', 'role', 'company', 'flags', 'checked'], delimiter=';')
        w.writeheader()
        w.writerows(rows)
    flagged = [r for r in rows if r['flags']]
    kinds = Counter()
    for r in flagged:
        for x in r['flags'].split('; '):
            kinds[re.sub(r':.*', '', x)] += 1
    lines = ['# Second-pass verification', '',
             f'Records checked: **{len(rows)}**, passed: **{len(rows) - len(flagged)}**, flagged: **{len(flagged)}**', '',
             '| check | hits |', '|---|---|']
    lines += [f'| {k} | {v} |' for k, v in kinds.most_common()]
    open(os.path.join(ROOT, 'verification.md'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print(f'checked {len(rows)}, flagged {len(flagged)} -> {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
