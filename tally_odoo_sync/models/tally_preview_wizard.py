# -*- coding: utf-8 -*-
import json
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class TallyInvoicePreviewWizard(models.TransientModel):
    """Readonly wizard to display Tally invoice preview without touching Tally."""

    _name = 'tally.invoice.preview.wizard'
    _description = 'Tally Invoice Preview Wizard'

    invoice_id = fields.Many2one('account.move', string='Invoice', readonly=True)
    invoice_name = fields.Char(string='Invoice Number', readonly=True)
    invoice_date = fields.Char(string='Invoice Date', readonly=True)
    company_name = fields.Char(string='Company', readonly=True)
    customer_name = fields.Char(string='Customer', readonly=True)

    readiness_status = fields.Selection(
        selection=[('ready', 'READY'), ('not_ready', 'NOT READY')],
        string='Readiness',
        readonly=True,
    )
    is_ready = fields.Boolean(string='Is Ready', readonly=True)

    # Summary fields
    subtotal = fields.Char(string='Subtotal', readonly=True)
    total_tax = fields.Char(string='Total Tax', readonly=True)
    grand_total = fields.Char(string='Grand Total', readonly=True)
    amount_words = fields.Char(string='Amount in Words', readonly=True)

    # Detailed HTML/text
    products_html = fields.Html(string='Products', readonly=True)
    tax_html = fields.Html(string='Tax Summary', readonly=True)
    hsn_html = fields.Html(string='HSN Summary', readonly=True)
    validation_html = fields.Html(string='Validation', readonly=True)
    validation_summary = fields.Text(string='Validation Summary', readonly=True)

    # Raw data
    payload_json = fields.Text(string='Payload JSON', readonly=True)
    xml_content = fields.Text(string='XML Content', readonly=True)
    error_message = fields.Text(string='Errors', readonly=True)

    # Flags for UI
    has_payload = fields.Boolean(readonly=True)
    has_xml = fields.Boolean(readonly=True)

    @api.model
    def create_from_preview(self, invoice, preview_data, payload=None, xml_data=None):
        """Factory: build wizard vals from preview_data dict."""
        company = invoice.company_id
        buyer = invoice.commercial_partner_id

        # Validation HTML
        validation = preview_data.get('validation', {})
        errors = preview_data.get('errors', [])
        is_ready = preview_data.get('ready', False)

        if is_ready:
            validation_html = '<p><b style="color:green;">READY</b> — Invoice is ready for Tally sync.</p>'
        else:
            items = ''.join('<li>%s</li>' % (e.get('message') or str(e)) for e in errors) if errors else '<li>Unknown validation failure</li>'
            validation_html = '<p><b style="color:red;">NOT READY</b></p><p><b>Missing / Errors:</b></p><ul>%s</ul>' % items

        # Add per-category PASS/FAIL if we have payload
        if payload:
            # Build simple category checks
            cats = []
            def _cat(name, ok):
                color = 'green' if ok else 'red'
                status = 'PASS' if ok else 'FAIL'
                cats.append('<tr><td>%s</td><td style="color:%s;"><b>%s</b></td></tr>' % (name, color, status))
            cats.append('<table class="table table-sm"><tr><th>Category</th><th>Status</th></tr>')
            _cat('Company', bool(payload.get('company', {}).get('company_name')))
            _cat('Customer', bool(payload.get('buyer', {}).get('buyer_name')))
            # Product checks: at least one line with hsn
            lines_ok = all(l.get('hsn_sac') for l in payload.get('lines', [])) if payload.get('lines') else False
            _cat('Product', bool(payload.get('lines')))
            _cat('HSN/SAC', lines_ok)
            uom_ok = all(l.get('uom') for l in payload.get('lines', [])) if payload.get('lines') else False
            _cat('UOM', uom_ok)
            tax_ok = bool(payload.get('tax_summary'))
            _cat('Tax', tax_ok)
            _cat('Tally Mapping', is_ready)
            cats.append('</table>')
            validation_html += ''.join(cats)

        # Products HTML
        products_html = ''
        if payload and payload.get('lines'):
            rows = []
            rows.append('<table class="table table-sm"><thead><tr><th>Item</th><th>HSN</th><th>Qty</th><th>UOM</th><th>Rate</th><th>Rate Incl Tax</th><th>Taxable</th><th>Total</th></tr></thead><tbody>')
            for l in payload['lines']:
                rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                    l.get('tally_stock_item') or l.get('item_name', ''),
                    l.get('hsn_sac', ''),
                    l.get('quantity', ''),
                    l.get('tally_uom') or l.get('uom', ''),
                    l.get('rate', ''),
                    l.get('rate_including_tax', ''),
                    l.get('taxable_amount', ''),
                    l.get('line_total', ''),
                ))
            rows.append('</tbody></table>')
            products_html = ''.join(rows)
        elif preview_data.get('ready') is False:
            products_html = '<p><i>Products unavailable — invoice not ready.</i></p>'

        # Tax HTML
        tax_html = ''
        if payload and payload.get('tax_summary'):
            rows = ['<table class="table table-sm"><thead><tr><th>Type</th><th>Ledger</th><th>Rate</th><th>Amount</th></tr></thead><tbody>']
            for t in payload['tax_summary']:
                rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (t.get('type',''), t.get('ledger_name',''), t.get('rate',''), t.get('amount','')))
            rows.append('</tbody></table>')
            tax_html = ''.join(rows)

        # HSN HTML
        hsn_html = ''
        if payload and payload.get('hsn_summary'):
            rows = ['<table class="table table-sm"><thead><tr><th>HSN/SAC</th><th>Taxable</th><th>CGST</th><th>SGST</th><th>IGST</th><th>Total Tax</th></tr></thead><tbody>']
            for h in payload['hsn_summary']:
                rows.append('<tr><td>%s</td><td>%s</td><td>%s %s</td><td>%s %s</td><td>%s %s</td><td>%s</td></tr>' % (
                    h.get('hsn_sac',''), h.get('taxable_value',''),
                    h.get('cgst_rate',''), h.get('cgst_amount',''),
                    h.get('sgst_rate',''), h.get('sgst_amount',''),
                    h.get('igst_rate',''), h.get('igst_amount',''),
                    h.get('total_tax',''),
                ))
            rows.append('</tbody></table>')
            hsn_html = ''.join(rows)

        # Amount in words
        amount_words_val = ''
        if payload:
            try:
                from odoo.addons.tally_odoo_sync.services import tally_amount_words as aw
                amount_words_val = aw.amount_in_words(float(payload.get('totals', {}).get('grand_total', 0)))
            except Exception:
                amount_words_val = ''

        # Validation summary text
        validation_summary = ''
        if is_ready:
            validation_summary = 'READY — All validations passed.'
        else:
            validation_summary = 'NOT READY\n' + '\n'.join('- %s' % (e.get('message') or str(e)) for e in errors)

        payload_json_str = ''
        if payload:
            try:
                payload_json_str = json.dumps(payload, indent=2, default=str)
            except Exception:
                payload_json_str = str(payload)

        vals = {
            'invoice_id': invoice.id,
            'invoice_name': invoice.name or '',
            'invoice_date': preview_data.get('invoice_date') or (payload.get('invoice_date') if payload else '') or '',
            'company_name': payload.get('company', {}).get('company_name') if payload else (company.tally_company_name or company.name or ''),
            'customer_name': preview_data.get('customer') or (payload.get('buyer', {}).get('buyer_name') if payload else '') or (buyer.name if buyer else ''),
            'readiness_status': 'ready' if is_ready else 'not_ready',
            'is_ready': is_ready,
            'subtotal': payload.get('totals', {}).get('subtotal', '') if payload else str(preview_data.get('subtotal', '')),
            'total_tax': payload.get('totals', {}).get('total_tax', '') if payload else str(preview_data.get('tax', '')),
            'grand_total': payload.get('totals', {}).get('grand_total', '') if payload else str(preview_data.get('total', '')),
            'amount_words': amount_words_val,
            'products_html': products_html,
            'tax_html': tax_html,
            'hsn_html': hsn_html,
            'validation_html': validation_html,
            'validation_summary': validation_summary,
            'payload_json': payload_json_str,
            'xml_content': xml_data or '',
            'error_message': '\n'.join(e.get('message','') for e in errors) if errors else '',
            'has_payload': bool(payload),
            'has_xml': bool(xml_data),
        }
        return vals
