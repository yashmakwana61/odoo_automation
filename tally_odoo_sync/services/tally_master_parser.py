# -*- coding: utf-8 -*-
"""Parser for Tally master export files (Master.json and future variants).

Responsibilities:
  * Detect/decode input bytes (UTF-8 / UTF-16 LE-BE, with or without BOM).
  * Parse JSON strictly (never silently drop malformed records).
  * Locate the Tally record list (``tallymessage`` in any casing / nesting).
  * Flatten each entry into a uniform dict:
        {record_type, tally_name, tally_guid, tally_alter_id,
         tally_parent, raw_payload}

Supported record types (extensible): Ledger, Group, Currency, StockItem,
Unit, Godown, Voucher, CostCentre, CostCategory, Employee + unknown passthrough.

Product/StockItem records are reported — never fabricated from Ledgers.
"""
import codecs
import json
import logging

_logger = logging.getLogger(__name__)

SUPPORTED_TYPES = (
    'ledger', 'group', 'currency', 'stockitem', 'stock item', 'stock_group',
    'unit', 'godown', 'voucher', 'costcentre', 'cost centre',
    'costcategory', 'cost category', 'employee',
)

# Canonical mapping of loose spellings -> canonical record_type label.
_TYPE_ALIASES = {
    'ledger': 'Ledger',
    'group': 'Group',
    'currency': 'Currency',
    'stockitem': 'Stock Item',
    'stock item': 'Stock Item',
    'stock_group': 'Stock Group',
    'unit': 'Unit',
    'godown': 'Godown',
    'voucher': 'Voucher',
    'costcentre': 'Cost Centre',
    'cost centre': 'Cost Centre',
    'costcategory': 'Cost Category',
    'cost category': 'Cost Category',
    'employee': 'Employee',
}

# Candidate key spellings inside a record payload.
# NOTE: ``currencyname`` is deliberately NOT a name key: on Ledger payloads
# it holds the ledger's currency reference (e.g. '\u20b9'), not the entity name.
_NAME_KEYS = ('name', 'ledgername', 'groupname', 'displayname', 'remotename',
              'stockitemname', 'unitname', 'partyledgername',
              'mailingname', 'oldmailingname')
_GUID_KEYS = ('guid', 'voucherguid', 'masterguid', 'tallyguid')
_ALTER_KEYS = ('alterid', 'alter_id', 'alterationid')
_PARENT_KEYS = ('parent', 'parentgroup', 'group', 'under', 'ledgergroup',
                'basicgroup', 'reservedname')


def detect_encoding(raw_bytes):
    """Return (encoding, bom_stripped_bytes) for Tally export bytes."""
    if raw_bytes.startswith(codecs.BOM_UTF16_LE):
        return 'utf-16', raw_bytes[len(codecs.BOM_UTF16_LE):]
    if raw_bytes.startswith(codecs.BOM_UTF16_BE):
        return 'utf-16-be', raw_bytes[len(codecs.BOM_UTF16_BE):]
    if raw_bytes.startswith(codecs.BOM_UTF8):
        return 'utf-8-sig', raw_bytes[len(codecs.BOM_UTF8):]
    # Heuristic: null-byte interleaving indicates UTF-16 without BOM.
    sample = raw_bytes[:200]
    if b'\x00' in sample:
        nulls = sample.count(b'\x00')
        if nulls > len(sample) * 0.15:
            # Guess LE vs BE by position of nulls.
            even_nulls = sum(1 for i in range(0, len(sample), 2) if sample[i:i + 1] == b'\x00')
            odd_nulls = nulls - even_nulls
            return ('utf-16-be' if even_nulls > odd_nulls else 'utf-16'), raw_bytes
    return 'utf-8', raw_bytes


def decode_bytes(raw_bytes):
    """Decode raw file bytes -> (text, encoding_used). Raises ValueError."""
    if raw_bytes is None:
        raise ValueError('No file content provided.')
    if isinstance(raw_bytes, str):
        return raw_bytes, 'utf-8 (already decoded)'
    encoding, stripped = detect_encoding(bytes(raw_bytes))
    try:
        text = stripped.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        # Fallbacks before giving up.
        for fallback in ('utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'cp1252'):
            try:
                text = bytes(raw_bytes).decode(fallback)
                return text, fallback + ' (fallback)'
            except Exception:
                continue
        raise ValueError('Unable to decode file (tried %s and fallbacks).' % encoding)
    # Strip stray BOM char if decoder kept it.
    if text and text[0] == '\ufeff':
        text = text[1:]
    return text, encoding


def parse_master_json(raw_bytes):
    """Full pipeline: bytes -> (root_object, encoding_used).

    Raises ValueError with a clear message for empty / malformed JSON.
    """
    text, encoding = decode_bytes(raw_bytes)
    if not text or not text.strip():
        raise ValueError('Uploaded file is empty.')
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError('Invalid JSON: %s (line %s col %s)' % (
            exc.msg, exc.lineno, exc.colno)) from exc
    return root, encoding


def _find_tally_messages(node, depth=0):
    """Recursively locate the first list that looks like tally messages."""
    if depth > 6 or node is None:
        return None
    if isinstance(node, dict):
        for key in node:
            if str(key).strip().lower() in ('tallymessage', 'tallymessages', 'messages'):
                val = node[key]
                if isinstance(val, list):
                    return val
                if isinstance(val, dict):
                    return [val]
        # Recurse into common envelope nestings first, then everything.
        for key in ('envelope', 'body', 'data', 'requestdata', 'importdata', 'result', 'tally'):
            if key in node or str(key).upper() in node:
                found = _find_tally_messages(node.get(key, node.get(str(key).upper())), depth + 1)
                if found is not None:
                    return found
        for val in node.values():
            if isinstance(val, (dict, list)):
                found = _find_tally_messages(val, depth + 1)
                if found is not None:
                    return found
        return None
    if isinstance(node, list):
        # A bare list of single-key dicts is itself the message list.
        if node and all(isinstance(item, dict) and len(item) == 1 for item in node[:20]):
            return node
        for item in node:
            found = _find_tally_messages(item, depth + 1)
            if found is not None:
                return found
        return None
    return None


def _unwrap_tally_value(value):
    """Flatten Tally's wrapped repeated values to a plain string.

    Tally exports encode lists as ``[{"metadata": true, "type": "String"},
    "value1", "value2"]`` — the descriptor dicts are skipped and the string
    parts joined. Plain scalars pass through stripped.
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


def _first_present(payload, keys):
    if not isinstance(payload, dict):
        return ''
    lower = {str(k).strip().lower(): v for k, v in payload.items()}
    for key in keys:
        if key in lower and lower[key] not in (None, ''):
            val = _unwrap_tally_value(lower[key])
            if val:
                return val
    return ''


def _metadata_block(payload):
    """Return the nested metadata/header/info dict of a payload, if any."""
    if not isinstance(payload, dict):
        return {}
    for nest_key in ('metadata', 'header', 'info'):
        for k, v in payload.items():
            if str(k).strip().lower() == nest_key and isinstance(v, dict):
                return v
    return {}


def _canonical_type(raw_type):
    key = str(raw_type or '').strip().lower().replace('_', '').replace('-', ' ')
    key = ' '.join(key.split())
    if key in _TYPE_ALIASES:
        return _TYPE_ALIASES[key]
    # e.g. 'stockitem' with space variants already handled; fallback title-case.
    cleaned = str(raw_type or '').strip()
    return cleaned[:64] if cleaned else 'Unknown'


def flatten_messages(messages):
    """Convert raw tallymessage entries -> uniform record dicts.

    Each entry is expected to be a single-key dict {Type: payload} or a
    dict containing a type hint + fields. Unknown shapes are preserved
    with record_type 'Unknown' so they are reported, never dropped.
    """
    records = []
    for idx, entry in enumerate(messages or []):
        try:
            if isinstance(entry, dict) and len(entry) == 1:
                raw_type, payload = next(iter(entry.items()))
                record_type = _canonical_type(raw_type)
                if not isinstance(payload, dict):
                    payload = {'value': payload}
            elif isinstance(entry, dict):
                # Probe for an explicit type marker. Real Tally master
                # exports nest it as metadata.type (e.g.
                # {"metadata": {"type": "Ledger", "name": ...}, ...}).
                raw_type = ''
                metadata = _metadata_block(entry)
                if metadata:
                    lowered_meta = {str(k).strip().lower(): v for k, v in metadata.items()}
                    for marker in ('type', 'recordtype', 'objecttype', 'objtype', 'tallyobject'):
                        if lowered_meta.get(marker) not in (None, ''):
                            raw_type = _unwrap_tally_value(lowered_meta[marker])
                            break
                if not raw_type:
                    for marker in ('recordtype', 'type', 'objecttype', 'objtype', 'tallyobject'):
                        for k, v in entry.items():
                            if str(k).strip().lower() == marker:
                                raw_type = _unwrap_tally_value(v)
                                break
                        if raw_type:
                            break
                record_type = _canonical_type(raw_type) if raw_type else 'Unknown'
                payload = entry
            else:
                record_type, payload = 'Unknown', {'value': entry}
            # Name priority: metadata.name (authoritative in Tally exports),
            # then top-level name keys. Never fall back to reference fields
            # such as currencyname.
            metadata = _metadata_block(payload) if isinstance(payload, dict) else {}
            name = (_first_present(metadata, _NAME_KEYS)
                    or _first_present(payload, _NAME_KEYS))
            merged = dict(payload) if isinstance(payload, dict) else {}
            if metadata:
                for k, v in metadata.items():
                    merged.setdefault(k, v)
            records.append({
                'record_type': record_type,
                'tally_name': name or _first_present(merged, _NAME_KEYS),
                'tally_guid': _first_present(merged, _GUID_KEYS),
                'tally_alter_id': _first_present(merged, _ALTER_KEYS),
                'tally_parent': _first_present(merged, _PARENT_KEYS),
                'raw_payload': payload if isinstance(payload, dict) else {'value': payload},
                'source_index': idx,
            })
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception('Failed to flatten tally message #%d', idx)
            records.append({
                'record_type': 'Unknown',
                'tally_name': '',
                'tally_guid': '',
                'tally_alter_id': '',
                'tally_parent': '',
                'raw_payload': {'value': str(entry)[:2000], 'error': str(exc)},
                'source_index': idx,
            })
    return records


def analyse_root(root):
    """Return structural summary of the parsed root object."""
    messages = _find_tally_messages(root)
    if messages is None:
        raise ValueError(
            'No Tally records found: expected a "tallymessage" list '
            '(checked envelope/body/data nestings and bare lists).')
    records = flatten_messages(messages)
    by_type = {}
    for rec in records:
        by_type[rec['record_type']] = by_type.get(rec['record_type'], 0) + 1
    has_stock = any(rec['record_type'] == 'Stock Item' for rec in records)
    return {
        'messages': messages,
        'records': records,
        'total': len(records),
        'by_type': by_type,
        'has_stock_items': has_stock,
    }
