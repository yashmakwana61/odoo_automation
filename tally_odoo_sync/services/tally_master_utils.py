# -*- coding: utf-8 -*-
"""Normalization + validation helpers for the Tally master importer.

All helpers are pure functions (no ORM) so they can be unit-tested in
isolation and reused by batch / matching / import code.
"""
import difflib
import fnmatch
import re

_GSTIN_RE = re.compile(
    r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'
)

_COMPANY_SUFFIXES = [
    'PRIVATE LIMITED', 'PVT LTD', 'PVT. LTD.', 'PVT LTD.', 'PVTLTD',
    'LIMITED', 'LTD', 'LLP', 'OPC', 'INC', 'CORP', 'CO', 'COMPANY',
    '& CO', '& SONS', 'AND SONS', 'TRADERS', 'TRADING', 'ENTERPRISES',
    'ENTERPRISE',
]

_WS_RE = re.compile(r'\s+')
_PUNCT_RE = re.compile(r'[.\-_,;:!?()\[\]{}"\']')


def normalize_name(name):
    """Return a comparable normalized form of a party/ledger name.

    Upper-cased, punctuation collapsed, whitespace collapsed, company
    suffixes stripped for comparison. Original value is NEVER modified
    by callers — this returns a new string.
    """
    if not name:
        return ''
    text = str(name).upper().strip()
    text = _PUNCT_RE.sub(' ', text)
    text = _WS_RE.sub(' ', text).strip()
    # Strip one trailing company suffix (longest first) for comparison.
    for suffix in sorted(_COMPANY_SUFFIXES, key=len, reverse=True):
        if text.endswith(' ' + suffix):
            text = text[:-(len(suffix) + 1)].strip()
            break
    return _WS_RE.sub(' ', text).strip()


def normalize_gstin(gstin):
    """Upper-case, strip spaces. Returns '' when empty."""
    if not gstin:
        return ''
    return re.sub(r'\s+', '', str(gstin)).upper().strip()


def is_valid_gstin(gstin):
    """Strict 15-char GSTIN format check (case-insensitive input)."""
    norm = normalize_gstin(gstin)
    if not norm:
        return False
    return bool(_GSTIN_RE.match(norm))


def normalize_phone(phone):
    """Keep digits and leading +. Strip spaces/dashes/brackets."""
    if not phone:
        return ''
    text = str(phone).strip()
    cleaned = re.sub(r'[^\d+]', '', text)
    # Collapse multiple leading + and strip Indian country prefix variants
    # for comparison purposes only (raw value preserved by callers).
    return cleaned


def normalize_email(email):
    if not email:
        return ''
    return str(email).strip().lower()


def normalize_state(state):
    if not state:
        return ''
    return _WS_RE.sub(' ', str(state).strip()).upper()


def normalize_pin(pin):
    if not pin:
        return ''
    return re.sub(r'\D', '', str(pin)).strip()


def name_similarity(a, b):
    """Fuzzy similarity 0..100 on normalized names (difflib ratio)."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return round(difflib.SequenceMatcher(None, na, nb).ratio() * 100.0, 2)


def match_pattern(pattern, value):
    """Case-insensitive fnmatch (supports * wildcards)."""
    if not pattern or value is None:
        return False
    return fnmatch.fnmatchcase(str(value).upper().strip(), str(pattern).upper().strip())


def _unwrap_list(value):
    """Flatten Tally's wrapped repeated values to a plain string.

    Lists look like ``[{"metadata": true, "type": "String"}, "v1", "v2"]``.
    """
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                continue
            if item is None or item == '':
                continue
            text = str(item).strip()
            if text:
                parts.append(text)
        return ', '.join(parts)
    if isinstance(value, dict):
        return ''
    return '' if value is None else str(value).strip()


def _first_dict(items):
    for item in items or []:
        if isinstance(item, dict):
            return item
    return {}


def _to_float(value):
    try:
        return float(str(value).strip().replace(',', ''))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def extract_stock_bits(payload):
    """Best-effort field extraction from a raw Tally Stock Item payload.

    Returns dict with base_uom / alt_uom / conversion / hsn / gst_rate /
    taxability / stock_group / is_deleted. GST rate is derived from the
    ratedetails (CGST + SGST/UTGST, falling back to IGST) of the first
    gstdetails block. Missing data yields '' / None — never fabricated.
    """
    if not isinstance(payload, dict):
        return {}
    lower = {str(k).strip().lower(): v for k, v in payload.items()}

    def _pick(*names):
        for name in names:
            if name in lower and lower[name] not in (None, ''):
                val = _unwrap_list(lower[name])
                if val and val.strip('\x04 ').upper() != 'NOT APPLICABLE':
                    return val.strip()
        return ''

    hsn = ''
    for block in lower.get('hsndetails') or []:
        if isinstance(block, dict):
            nested = {str(k).strip().lower(): v for k, v in block.items()}
            code = _unwrap_list(nested.get('hsncode') or nested.get('hsn_code') or '')
            if code:
                hsn = code.strip()
                break

    gst_rate, taxability = None, ''
    for block in lower.get('gstdetails') or []:
        if not isinstance(block, dict):
            continue
        nested = {str(k).strip().lower(): v for k, v in block.items()}
        taxability = _unwrap_list(nested.get('taxability') or '')
        states = nested.get('statewisedetails') or []
        rated = []
        for state in states:
            if isinstance(state, dict):
                sub = {str(k).strip().lower(): v for k, v in state.items()}
                rated.extend(sub.get('ratedetails') or [])
        cgst = sgst = igst = 0.0
        for rate in rated:
            if not isinstance(rate, dict):
                continue
            rnest = {str(k).strip().lower(): v for k, v in rate.items()}
            head = _unwrap_list(rnest.get('gstratedutyhead') or '').upper()
            amount = _to_float(rnest.get('gstrate'))
            if 'CGST' in head:
                cgst += amount
            elif 'SGST' in head or 'UTGST' in head:
                sgst += amount
            elif 'IGST' in head:
                igst += amount
        total = round(cgst + sgst, 2) if (cgst or sgst) else round(igst, 2)
        if total:
            gst_rate = total
        break

    alt_uom, conversion = '', ''
    alt = lower.get('additionalunits')
    if isinstance(alt, dict):
        anest = {str(k).strip().lower(): v for k, v in alt.items()}
        alt_uom = _unwrap_list(anest.get('unit') or anest.get('additionalunit') or '')
        conversion = _unwrap_list(anest.get('conversion') or anest.get('factor') or '')

    deleted = lower.get('isdeleted')
    if isinstance(deleted, str):
        is_deleted = deleted.strip().lower() in ('true', '1', 'yes')
    else:
        is_deleted = bool(deleted)

    return {
        'base_uom': _pick('baseunits', 'baseunit', 'unit', 'uom'),
        'alt_uom': alt_uom,
        'conversion': conversion,
        'hsn': hsn,
        'gst_rate': gst_rate,
        'taxability': taxability,
        'stock_group': _pick('parent', 'group', 'stockgroup'),
        'is_deleted': is_deleted,
    }


def extract_address_bits(payload):
    """Best-effort address extraction from a raw Tally ledger payload.

    Tally exports vary; probe common key spellings (case-insensitive).
    Structured blocks win: ``ledmailingdetails[0]`` for street/state/
    country/pin, ``ledgstregdetails[0]`` for GSTIN/registration type.
    Fallbacks: top-level keys, then legacy ``oldaddress``/``priorstatename``.
    Returns dict with street/state/pin/country/email/phone/mobile/website/contact.
    """
    if not isinstance(payload, dict):
        return {}
    lower = {}
    for key, val in payload.items():
        lower[str(key).strip().lower()] = val

    def _pick(*names):
        for name in names:
            if name in lower and lower[name] not in (None, ''):
                val = _unwrap_list(lower[name])
                if val:
                    return val
        return ''

    mail = _first_dict(lower.get('ledmailingdetails'))
    mail_lower = {str(k).strip().lower(): _unwrap_list(v) for k, v in mail.items()}
    gstd = _first_dict(lower.get('ledgstregdetails'))
    gstd_lower = {str(k).strip().lower(): _unwrap_list(v) for k, v in gstd.items()}

    def _mail(*names):
        for name in names:
            if mail_lower.get(name):
                return mail_lower[name]
        return ''

    address = (_mail('address')
               or _pick('mailingaddress', 'address', 'mailing_address', 'ledgeraddress')
               or _pick('oldaddress', 'oldmailingname'))
    # Tally mailing address often embeds newlines — keep first lines as street.
    street = address.replace('\r', '\n')
    gstin = (_pick('partygstin', 'gstin', 'party_gstin', 'gst_no', 'gstnumber')
             or gstd_lower.get('gstin', ''))
    return {
        'street': street,
        'state': (_mail('state')
                  or _pick('state', 'ledgerstate', 'mailingstate', 'statename')
                  or _pick('priorstatename', 'oldledstatename', 'oldstatename')),
        'pin': (_mail('pincode', 'pin', 'zip')
                or _pick('pincode', 'pin', 'pincode_retail', 'zip')),
        'country': (_mail('country')
                    or _pick('country', 'mailingcountry', 'countryname')
                    or _pick('oldcountryname')),
        'email': _pick('email', 'ledgeremail', 'emailid', 'e-mail'),
        'phone': (_pick('ledgerphone', 'phone', 'telephone', 'phoneno')
                  or _mail('phone', 'phoneno', 'telephone')),
        'mobile': _pick('ledgermobile', 'mobile', 'mobileno', 'cellphone'),
        'website': _pick('website', 'url', 'ledgerwebsite'),
        'contact': _pick('contact', 'contactperson', 'ledgercontact', 'contactname'),
        'gstin': gstin,
        'gst_regtype': (_pick('gstregistrationtype', 'gst_regtype', 'registrationtype')
                        or gstd_lower.get('gstregistrationtype', '')),
        'credit_period': _pick('billcreditperiod', 'creditperiod', 'credit_days'),
        'opening_balance': _pick('openingbalance', 'opening_balance', 'openbal'),
    }
