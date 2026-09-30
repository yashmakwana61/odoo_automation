# -*- coding: utf-8 -*-
"""Invoice payload builder and validator for Tally synchronization.

Builds a complete, validated TallyInvoicePayload from an Odoo invoice.
All data is sourced from Odoo (source of truth).
"""
import hashlib
import json
import logging
from collections import OrderedDict

from odoo import _, fields

_logger = logging.getLogger(__name__)

# Tolerance for monetary reconciliation (floating-point / rounding)
_MONEY_TOLERANCE = 0.02


def _fmt_amount(value):
    """Format a monetary value for the payload (clean decimal)."""
    value = float(value or 0.0)
    if value == int(value):
        return str(int(value))
    return ('%.2f' % value).rstrip('0').rstrip('.')


# =========================================================================
# Payload builder
# =========================================================================

def build_payload(invoice, mappings):
    """Build a complete TallyInvoicePayload dict from an Odoo invoice.

    :param invoice: ``account.move`` record (out_invoice, posted)
    :param mappings: dict returned by ``tally.mapping.resolve_all()``
    :return: structured payload dict
    """
    company = invoice.company_id
    partner = invoice.commercial_partner_id

    payload = {
        'invoice_id': invoice.id,
        'invoice_number': invoice.name or '',
        'invoice_date': _fmt_date(invoice),
        'company': _build_company(company),
        'buyer': _build_buyer(partner),
        'lines': _build_lines(invoice, mappings),
        'tax_summary': [],
        'totals': _build_totals(invoice),
        'hsn_summary': [],
        'bank': _build_bank(company),
        'footer': _build_footer(company),
        'tally_mappings': {
            'customer_ledger': mappings['customer']['name'],
            'sales_ledger': company.tally_sales_ledger or 'Sales',
            'stock_items': {p['product_id'].id: p['name'] for p in mappings['products'] if not p['missing']},
            'tax_ledgers': {t['tax_id'].id: t['name'] for t in mappings['taxes'] if not t['missing']},
            'uom_mappings': {u['uom_id'].id: u['name'] for u in mappings['uoms'] if not u['missing']},
        },
        'nature_of_transaction': _nature_of_transaction(invoice),
        'gst_treatment': 'Regular' if partner.vat else 'Unregistered',
        'amount_words': '',
        'amount_words_tax': '',
    }

    # Build tax summary (from lines)
    payload['tax_summary'] = _build_tax_summary(invoice, mappings)

    # Build HSN summary (group by HSN + tax structure)
    payload['hsn_summary'] = _build_hsn_summary(invoice, mappings)

    # Amount in words (Indian numbering)
    try:
        from odoo.addons.tally_odoo_sync.services import tally_amount_words as _aw
        payload['amount_words'] = _aw.amount_in_words(abs(invoice.amount_total))
        payload['amount_words_tax'] = _aw.amount_in_words(abs(invoice.amount_tax))
    except Exception:
        payload['amount_words'] = ''
        payload['amount_words_tax'] = ''

    # Compute request hash for duplicate protection
    payload['request_hash'] = _compute_hash(invoice)

    return payload


def _fmt_date(invoice):
    """Format invoice date as YYYYMMDD for Tally."""
    dt = invoice.invoice_date or invoice.date
    if dt:
        return fields.Date.to_string(dt).replace('-', '')
    return ''


def _build_company(company):
    """Extract company information from Odoo company record."""
    state = company.state_id
    return {
        'company_name': company.tally_company_name or company.name,
        'company_address': _full_address(company),
        'company_gstin': company.vat or '',
        'company_lic': getattr(company, 'company_lic', '') or '',
        'company_contact': company.phone or '',
        'company_email': company.email or '',
        'company_state': state.name if state else '',
        'company_state_code': state.l10n_in_tin if state else '',
        'company_cin': getattr(company, 'company_cin', '') or '',
        'company_pan': getattr(company, 'company_pan', '') or _extract_pan(company.vat),
    }


def _extract_pan(gstin):
    """Extract PAN from GSTIN (characters 3-12 of a 15-char GSTIN)."""
    if gstin and len(gstin) == 15:
        return gstin[2:12]
    return ''


def _full_address(company):
    """Build full address from company fields."""
    parts = [
        getattr(company, 'street', ''),
        getattr(company, 'street2', ''),
        getattr(company, 'city', ''),
        getattr(company, 'zip', ''),
        company.state_id.name if company.state_id else '',
        company.country_id.name if company.country_id else '',
    ]
    return ', '.join(p for p in parts if p)


def _build_buyer(partner):
    """Extract buyer information from Odoo partner record."""
    state = partner.state_id
    country = partner.country_id
    return {
        'buyer_name': partner.name or '',
        'buyer_address': _partner_address(partner),
        'buyer_gstin': partner.vat or '',
        'buyer_state': state.name if state else '',
        'buyer_state_code': state.l10n_in_tin if state else '',
        'buyer_country': country.name if country else '',
        'buyer_country_code': country.code if country else '',
    }


def _partner_address(partner):
    """Build full address from partner fields."""
    parts = [
        partner.street,
        partner.street2,
        partner.city,
        partner.zip,
        partner.state_id.name if partner.state_id else '',
        partner.country_id.name if partner.country_id else '',
    ]
    return ', '.join(p for p in parts if p)


def _build_lines(invoice, mappings):
    """Build line items from invoice lines."""
    stock_items = {p['product_id'].id: p['name'] for p in mappings['products'] if not p['missing']}
    uom_map = {u['uom_id'].id: u['name'] for u in mappings['uoms'] if not u['missing']}

    lines = []
    for line in invoice.invoice_line_ids:
        if line.display_type in ('line_section', 'line_note'):
            continue

        product = line.product_id
        tax_ids = line.tax_ids.ids if line.tax_ids else []

        # Compute tax for this line
        tax_results = {}
        if line.tax_ids:
            computed = line.tax_ids.compute_all(
                line.price_unit,
                currency=line.currency_id or invoice.currency_id,
                quantity=line.quantity,
                product=line.product_id,
                partner=invoice.partner_id,
            )
            for tr in computed.get('taxes', []):
                tax_results[tr['id']] = abs(tr.get('amount', 0.0))

        line_tax_total = sum(tax_results.values())
        rate_incl = line.price_unit + (line.price_unit * sum(
            t.amount for t in line.tax_ids) / 100.0) if line.tax_ids else line.price_unit

        lines.append({
            'item_name': stock_items.get(product.id) if product else (line.name or ''),
            'odoo_product_id': product.id if product else False,
            'tally_stock_item': stock_items.get(product.id, ''),
            'hsn_sac': getattr(product, 'hsn_code', '') or getattr(product, 'l10n_in_hsn_code', '') or '',
            'quantity': line.quantity,
            'uom': line.product_uom_id.name if line.product_uom_id else '',
            'tally_uom': uom_map.get(line.product_uom_id.id, '') if line.product_uom_id else '',
            'rate': _fmt_amount(line.price_unit),
            'rate_including_tax': _fmt_amount(rate_incl),
            'taxable_amount': _fmt_amount(abs(line.price_subtotal)),
            'line_total': _fmt_amount(abs(line.price_subtotal) + line_tax_total),
            'tax_ids': tax_ids,
            'tax_amounts': tax_results,
        })

    return lines


def _build_totals(invoice):
    """Build invoice totals from Odoo invoice."""
    return {
        'subtotal': _fmt_amount(abs(invoice.amount_untaxed)),
        'total_tax': _fmt_amount(abs(invoice.amount_tax)),
        'grand_total': _fmt_amount(abs(invoice.amount_total)),
    }


def _build_tax_summary(invoice, mappings):
    """Compute per-tax summary from invoice lines."""
    tax_map = {}  # (tax_id, tax_type) -> {tax, type, ledger_name, rate, amount}

    for line in invoice.invoice_line_ids:
        if line.display_type in ('line_section', 'line_note') or not line.tax_ids:
            continue
        tax_results = line.tax_ids.compute_all(
            line.price_unit,
            currency=line.currency_id or invoice.currency_id,
            quantity=line.quantity,
            product=line.product_id,
            partner=invoice.partner_id,
        )
        for tr in tax_results.get('taxes', []):
            tax_id = tr['id']
            tax = invoice.env['account.tax'].browse(tax_id)
            tax_type = _classify_tax(tax)
            key = (tax_id, tax_type)
            if key not in tax_map:
                tax_map[key] = {
                    'tax_id': tax_id,
                    'type': tax_type,
                    'rate': tax.amount,
                    'amount': 0.0,
                    'ledger_name': mappings['taxes'][0]['name'] if mappings['taxes'] else '',
                }
            tax_map[key]['amount'] += abs(tr.get('amount', 0.0))

    # Resolve ledger names from mappings
    tax_ledger_map = {t['tax_id'].id: t['name'] for t in mappings['taxes'] if not t['missing']}

    summary = []
    for bucket in tax_map.values():
        ledger_name = tax_ledger_map.get(bucket['tax_id'], bucket['ledger_name'])
        summary.append({
            'tax_id': bucket['tax_id'],
            'type': bucket['type'],
            'ledger_name': ledger_name,
            'rate': bucket['rate'],
            'amount': _fmt_amount(bucket['amount']),
        })
    return summary


def _build_hsn_summary(invoice, mappings):
    """Group invoice lines by HSN/SAC + tax structure for the HSN summary table."""
    hsn_map = OrderedDict()
    stock_items = {p['product_id'].id: p['name'] for p in mappings['products'] if not p['missing']}

    for line in invoice.invoice_line_ids:
        if line.display_type in ('line_section', 'line_note'):
            continue

        product = line.product_id
        hsn = getattr(product, 'hsn_code', '') or getattr(product, 'l10n_in_hsn_code', '') or ''

        # Compute taxes for this line
        line_taxes = {}
        if line.tax_ids:
            computed = line.tax_ids.compute_all(
                line.price_unit,
                currency=line.currency_id or invoice.currency_id,
                quantity=line.quantity,
                product=line.product_id,
                partner=invoice.partner_id,
            )
            for tr in computed.get('taxes', []):
                tax = invoice.env['account.tax'].browse(tr['id'])
                tax_type = _classify_tax(tax)
                rate = tax.amount
                key = (hsn, tax_type, rate)
                if key not in line_taxes:
                    line_taxes[key] = 0.0
                line_taxes[key] += abs(tr.get('amount', 0.0))

        taxable = abs(line.price_subtotal)

        # Group into HSN entries: key = (hsn, tax_structure)
        # tax_structure is a frozenset of (type, rate) tuples
        tax_structure = frozenset((t, r) for (_, t, r) in line_taxes.keys())
        entry_key = (hsn, tax_structure)

        if entry_key not in hsn_map:
            hsn_map[entry_key] = {
                'hsn_sac': hsn,
                'taxable_value': 0.0,
                'cgst_rate': 0.0,
                'cgst_amount': 0.0,
                'sgst_rate': 0.0,
                'sgst_amount': 0.0,
                'igst_rate': 0.0,
                'igst_amount': 0.0,
                'total_tax': 0.0,
            }

        entry = hsn_map[entry_key]
        entry['taxable_value'] += taxable

        for (hsn_key, tax_type, rate), amount in line_taxes.items():
            if tax_type == 'CGST':
                entry['cgst_rate'] = rate
                entry['cgst_amount'] += amount
            elif tax_type == 'SGST':
                entry['sgst_rate'] = rate
                entry['sgst_amount'] += amount
            elif tax_type == 'IGST':
                entry['igst_rate'] = rate
                entry['igst_amount'] += amount
            entry['total_tax'] += amount

    summary = []
    for entry in hsn_map.values():
        summary.append({
            'hsn_sac': entry['hsn_sac'],
            'taxable_value': _fmt_amount(entry['taxable_value']),
            'cgst_rate': _fmt_amount(entry['cgst_rate']) if entry['cgst_rate'] else '',
            'cgst_amount': _fmt_amount(entry['cgst_amount']) if entry['cgst_amount'] else '',
            'sgst_rate': _fmt_amount(entry['sgst_rate']) if entry['sgst_rate'] else '',
            'sgst_amount': _fmt_amount(entry['sgst_amount']) if entry['sgst_amount'] else '',
            'igst_rate': _fmt_amount(entry['igst_rate']) if entry['igst_rate'] else '',
            'igst_amount': _fmt_amount(entry['igst_amount']) if entry['igst_amount'] else '',
            'total_tax': _fmt_amount(entry['total_tax']),
        })
    return summary


def _build_bank(company):
    """Build bank details from company configuration."""
    bank = company.bank_ids[:1] if hasattr(company, 'bank_ids') and company.bank_ids else None
    # Branch: explicit tally_branch on company overrides bank record branch
    branch = getattr(company, 'tally_branch', '') or ''
    if not branch and bank:
        # Try to derive branch from bank record: bank_name may contain it, else empty
        branch = getattr(bank, 'branch', '') or getattr(bank.bank_id, 'branch', '') if hasattr(bank, 'bank_id') and bank.bank_id else ''
    return {
        'bank_name': bank.bank_name if bank else '',
        'account_holder': bank.acc_holder_name if bank and getattr(bank, 'acc_holder_name', '') else company.name,
        'account_number': bank.acc_number if bank else '',
        'ifsc': bank.bank_bic if bank else '',
        'branch': branch or '',
    }


def _build_footer(company):
    """Build footer information (declaration, signatory)."""
    return {
        'declaration': getattr(company, 'tally_declaration', '') or 'Subject to jurisdiction',
        'authorised_signatory': getattr(company, 'tally_signatory', '') or company.partner_id.name or '',
    }


def _nature_of_transaction(invoice):
    """Determine inter-state or intra-state."""
    partner = invoice.commercial_partner_id
    company = invoice.company_id
    if (partner.country_id and partner.country_id.code == 'IN'
            and partner.state_id and company.state_id
            and partner.state_id.id != company.state_id.id):
        return 'Inter-State'
    return 'Intra-State'


def _classify_tax(tax):
    """Classify a tax into CGST / SGST / IGST / CESS (or OTHER).

    Resolution: l10n_in tags -> tax group name -> tax name.
    """
    # Check l10n_in tags if available
    try:
        from odoo.addons.tally_odoo_sync.services.tally_service import (
            _TAX_TAG_XMLIDS, _TAX_TYPES,
        )
        for tax_type, xmlid in _TAX_TAG_XMLIDS.items():
            try:
                tag = tax.env.ref(xmlid, raise_if_not_found=False)
            except Exception:
                tag = None
            if tag and tag in tax.tag_ids:
                return tax_type
    except Exception:
        pass

    _TAX_TYPES = ('CGST', 'SGST', 'IGST', 'CESS')
    # Fall back to tax group name
    group_name = (tax.tax_group_id.name or '').upper()
    for tax_type in _TAX_TYPES:
        if tax_type in group_name:
            return tax_type

    # Fall back to tax name
    tax_name = (tax.name or '').upper()
    for tax_type in _TAX_TYPES:
        if tax_type in tax_name:
            return tax_type
    return 'OTHER'


def _compute_hash(invoice):
    """Compute a deterministic hash of the invoice data for duplicate detection."""
    parts = [
        str(invoice.company_id.id),
        str(invoice.id),
        str(invoice.name or ''),
        str(invoice.amount_total),
        str(invoice.amount_untaxed),
        str(invoice.amount_tax),
    ]
    for line in invoice.invoice_line_ids:
        if line.display_type not in ('line_section', 'line_note'):
            parts.append('%s:%s:%s' % (line.product_id.id, line.quantity, line.price_unit))
    raw = '|'.join(parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


# =========================================================================
# Invoice validation
# =========================================================================

def validate_invoice_for_tally(invoice, mappings=None):
    """Validate an invoice for Tally synchronization.

    :param invoice: ``account.move`` record
    :param mappings: optional pre-resolved mappings dict (will resolve if not given)
    :return: dict with 'ready' (bool) and 'errors' (list of error dicts)
    """
    errors = []

    # Resolve mappings if not provided
    if mappings is None:
        from odoo.addons.tally_odoo_sync.models.tally_mapping import TallyMapping
        mappings = TallyMapping.resolve_all(invoice)

    company = invoice.company_id
    partner = invoice.commercial_partner_id

    # --- COMPANY ---
    if not (company.tally_company_name or company.name):
        errors.append(_err('MISSING_COMPANY_NAME', 'company.company_name', 'Company name is required'))
    if not company.vat:
        errors.append(_err('MISSING_COMPANY_GSTIN', 'company.company_gstin', 'Company GSTIN is required'))
    if not company.state_id:
        errors.append(_err('MISSING_COMPANY_STATE', 'company.company_state', 'Company state is required'))

    # --- BUYER ---
    if not partner:
        errors.append(_err('MISSING_CUSTOMER', 'buyer.buyer_name', 'Invoice has no customer'))
    else:
        if not partner.name:
            errors.append(_err('MISSING_CUSTOMER_NAME', 'buyer.buyer_name', 'Customer name is required'))
        # GSTIN is required for registered dealers; optional for unregistered
        # We flag it as a warning rather than hard block when the customer has no VAT

    # --- INVOICE ---
    if not invoice.name:
        errors.append(_err('MISSING_INVOICE_NUMBER', 'invoice.invoice_number', 'Invoice number is required'))
    if not (invoice.invoice_date or invoice.date):
        errors.append(_err('MISSING_INVOICE_DATE', 'invoice.invoice_date', 'Invoice date is required'))
    if invoice.state != 'posted':
        errors.append(_err('INVOICE_NOT_POSTED', 'invoice.state', 'Invoice must be posted before Tally sync'))

    # Invoice lines
    valid_lines = [l for l in invoice.invoice_line_ids if l.display_type not in ('line_section', 'line_note')]
    if not valid_lines:
        errors.append(_err('NO_INVOICE_LINES', 'invoice.lines', 'Invoice must have at least one line'))

    # --- PRODUCTS ---
    for idx, line in enumerate(valid_lines):
        prefix = 'lines[%d]' % idx
        if not line.product_id:
            errors.append(_err('MISSING_PRODUCT', '%s.product' % prefix,
                               'Line %d has no product' % (idx + 1)))
        elif line.product_id:
            hsn = getattr(line.product_id, 'hsn_code', '') or getattr(line.product_id, 'l10n_in_hsn_code', '')
            if not hsn:
                errors.append(_err('MISSING_HSN', '%s.hsn_sac' % prefix,
                                   'Product "%s" has no HSN/SAC code' % line.product_id.display_name))
        if line.quantity <= 0:
            errors.append(_err('INVALID_QUANTITY', '%s.quantity' % prefix,
                               'Line %d quantity must be greater than 0' % (idx + 1)))
        if not line.product_uom_id:
            errors.append(_err('MISSING_UOM', '%s.uom' % prefix,
                               'Line %d has no Unit of Measure' % (idx + 1)))
        if line.price_unit <= 0:
            errors.append(_err('INVALID_PRICE', '%s.rate' % prefix,
                               'Line %d price must be greater than 0' % (idx + 1)))

    # --- TALLY MAPPINGS ---
    if mappings.get('missing'):
        for missing_msg in mappings['missing']:
            errors.append(_err('MISSING_TALLY_MAPPING', 'tally_mappings', missing_msg))

    # --- BANK (warning only, not blocking) ---
    # Bank details are recommended but not strictly required for all invoice types

    return {
        'ready': len(errors) == 0,
        'errors': errors,
    }


def _err(code, field, message):
    """Create a structured error dict."""
    return {'code': code, 'field': field, 'message': message}


# =========================================================================
# Tax reconciliation
# =========================================================================

def reconcile_totals(invoice, payload):
    """Compare Odoo invoice totals against the payload totals.

    :return: dict with 'reconciled' (bool) and 'errors' (list)
    """
    errors = []
    odoo_subtotal = abs(invoice.amount_untaxed)
    odoo_tax = abs(invoice.amount_tax)
    odoo_total = abs(invoice.amount_total)

    payload_subtotal = float(payload['totals']['subtotal'])
    payload_tax = float(payload['totals']['total_tax'])
    payload_total = float(payload['totals']['grand_total'])

    if abs(odoo_subtotal - payload_subtotal) > _MONEY_TOLERANCE:
        errors.append(_err('SUBTOTAL_MISMATCH', 'totals.subtotal',
                           'Subtotal mismatch: Odoo=%.2f, Payload=%.2f' % (odoo_subtotal, payload_subtotal)))
    if abs(odoo_tax - payload_tax) > _MONEY_TOLERANCE:
        errors.append(_err('TAX_MISMATCH', 'totals.total_tax',
                           'Tax mismatch: Odoo=%.2f, Payload=%.2f' % (odoo_tax, payload_tax)))
    if abs(odoo_total - payload_total) > _MONEY_TOLERANCE:
        errors.append(_err('TOTAL_MISMATCH', 'totals.grand_total',
                           'Total mismatch: Odoo=%.2f, Payload=%.2f' % (odoo_total, payload_total)))

    # HSN summary total tax should reconcile with invoice tax
    hsn_total_tax = sum(float(h['total_tax']) for h in payload['hsn_summary'])
    if abs(odoo_tax - hsn_total_tax) > _MONEY_TOLERANCE:
        errors.append(_err('HSN_TAX_MISMATCH', 'hsn_summary.total_tax',
                           'HSN summary tax total %.2f != invoice tax %.2f' % (hsn_total_tax, odoo_tax)))

    return {
        'reconciled': len(errors) == 0,
        'errors': errors,
    }
