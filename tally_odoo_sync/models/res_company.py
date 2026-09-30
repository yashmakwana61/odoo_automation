# -*- coding: utf-8 -*-
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    """Per-company configuration for the Tally integration."""

    _inherit = 'res.company'

    # -- Master switches -------------------------------------------------------
    tally_sync_enabled = fields.Boolean(
        string='Enable Tally Sync',
        default=False,
        help="Push posted customer invoices to Tally.",
    )
    tally_sync_on_post = fields.Boolean(
        string='Sync Automatically on Post',
        default=True,
        help="Push the invoice to Tally as soon as it is posted. "
             "If disabled, invoices are only synchronized manually or via the retry cron.",
    )

    # -- Connection ------------------------------------------------------------
    tally_server_url = fields.Char(
        string='Tally Server URL',
        default='http://localhost:9000',
        help="Tally HTTP API endpoint (Tally must be running with the HTTP server enabled).",
    )
    tally_company_name = fields.Char(
        string='Tally Company Name',
        help="Name of the company inside Tally (SVCURRENTCOMPANY). "
             "Defaults to the Odoo company name when empty.",
    )
    tally_username = fields.Char(string='Tally HTTP Username')
    tally_password = fields.Char(string='Tally HTTP Password')
    tally_timeout = fields.Integer(
        string='Tally Timeout (seconds)',
        default=30,
    )
    tally_verify_ssl = fields.Boolean(
        string='Verify SSL Certificate',
        default=False,
        help="Disable for local HTTP endpoints.",
    )

    # -- Ledgers ---------------------------------------------------------------
    tally_sales_ledger = fields.Char(
        string='Sales Ledger Name',
        default='Sales',
        help="Tally ledger used to post the sales (credit) leg of the voucher.",
    )
    tally_auto_create_ledgers = fields.Boolean(
        string='Auto-create Missing Ledgers',
        default=False,
        help="Send LEDGER creation instructions alongside the voucher so missing "
             "party / sales / tax ledgers are created by Tally.",
    )
    tally_party_ledger_parent = fields.Char(
        string='Party Ledger Parent Group',
        default='Sundry Debtors',
        help="Tally group under which the customer (party) ledger is created.",
    )
    tally_sales_ledger_parent = fields.Char(
        string='Sales Ledger Parent Group',
        default='Sales Accounts',
        help="Tally group under which the sales ledger is created.",
    )
    tally_tax_ledger_parent = fields.Char(
        string='Tax Ledger Parent Group',
        default='Duties & Taxes',
        help="Tally group under which the GST/CESS output tax ledgers are created.",
    )

    # -- Company identity (for invoice footer) ---------------------------------
    tally_declaration = fields.Char(
        string='Invoice Declaration',
        default='Subject to jurisdiction',
        help="Declaration text shown on the Tally invoice.",
    )
    tally_signatory = fields.Char(
        string='Authorised Signatory',
        help="Name of the authorised signatory shown on the Tally invoice. "
             "Defaults to the company contact person.",
    )

    # -- Additional company master data (CIN/PAN/LIC etc.) --------------------
    # These are stored explicitly so the golden invoice can distinguish
    # "master data missing in Odoo" from "field not needed in voucher XML".
    # Company GSTIN is already `vat`; PAN can be derived from GSTIN but
    # an explicit field allows overrides and clearer diagnostics.
    company_cin = fields.Char(
        string='CIN',
        help="Corporate Identity Number (21 chars). Printed via Tally Company Master.",
    )
    company_pan = fields.Char(
        string='PAN',
        help="Company PAN (10 chars). Derived from GSTIN if empty; explicit value overrides.",
    )
    company_lic = fields.Char(
        string='LIC No.',
        help="Company LIC / Trade Licence number. Printed via Tally Company Master.",
    )
    tally_branch = fields.Char(
        string='Bank Branch',
        help="Branch name for the primary bank account shown on the invoice.",
    )

    # -- Retry / retry policy -------------------------------------------------
    tally_retry_max_attempts = fields.Integer(
        string='Max Retry Attempts',
        default=3,
        help="Maximum number of automatic retry attempts for failed syncs.",
    )
    tally_sync_mode = fields.Selection(
        [('manual', 'Manual'), ('automatic', 'Automatic')],
        string='Sync Mode',
        default='manual',
        help="Automatic = cron retries; Manual = only on button/post.",
    )
    tally_connector_type = fields.Selection(
        [('direct', 'Direct (localhost)'), ('secure_connector', 'Secure Connector')],
        string='Connector Type',
        default='direct',
        help="Secure connector is recommended for VPS → client Tally over localhost:9000.",
    )

    # -- Validation helpers --------------------------------------------------
    def validate_tally_configuration(self):
        """Validate Tally configuration for this company.

        Returns dict with checks: url, connectivity, company, ledgers, tax, uom.
        """
        self.ensure_one()
        result = {'checks': [], 'valid': True}
        def add(check, ok, msg):
            result['checks'].append({'check': check, 'ok': ok, 'message': msg})
            if not ok:
                result['valid'] = False

        # URL
        url = (self.tally_server_url or '').strip()
        add('Tally URL', bool(url), 'Configured' if url else 'Missing URL')
        if url and url.startswith('http://0.0.0.0'):
            add('URL Security', False, 'Do not expose 0.0.0.0:9000 publicly; use secure connector')
        else:
            add('URL Security', True, 'URL not exposing 0.0.0.0')

        # Company
        add('Company Name', bool(self.tally_company_name or self.name), 'OK' if (self.tally_company_name or self.name) else 'Missing')
        add('Company GSTIN', bool(self.vat), 'OK' if self.vat else 'Missing GSTIN (vat)')
        add('Company State', bool(self.state_id), 'OK' if self.state_id else 'Missing state')
        add('Sales Ledger', bool(self.tally_sales_ledger), 'OK' if self.tally_sales_ledger else 'Missing sales ledger')

        # Connectivity: probe the SAME endpoint invoice sync uses.
        # The sync layer rewrites localhost for WSL (Windows host gateway),
        # so the check must resolve first — otherwise Test Connection fails
        # while real syncs would succeed.
        if url:
            service = self.env['tally.service']
            target = service._resolve_tally_url(url)
            try:
                resp = requests.get(target, timeout=5)
                detail = 'HTTP %s via %s' % (resp.status_code, target)
                if target != url:
                    detail += ' (localhost rewritten for WSL)'
                add('Connectivity', True, detail)
            except Exception as e:
                add('Connectivity', False, 'No response from %s: %s' % (target, e))

        # Ledger mappings spot check
        sample_mappings = self.env['tally.mapping'].search([('company_id', '=', self.id)], limit=1)
        add('Ledger Mappings', bool(sample_mappings), 'At least one mapping exists' if sample_mappings else 'No tally.mapping found; add customer/product/tax/uom mappings')

        return result

    def action_test_tally_connection(self):
        self.ensure_one()
        res = self.validate_tally_configuration()
        # Build human readable result
        lines = []
        for c in res['checks']:
            icon = '✓' if c['ok'] else '✗'
            lines.append('%s %s: %s' % (icon, c['check'], c['message']))
        msg = '\n'.join(lines)
        # Never log password
        _logger.info("Tally config test for %s: %s", self.name, msg)
        if not res['valid']:
            raise UserError(_('Tally configuration issues:\n%s', msg))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Tally Configuration OK'),
                'message': msg,
                'type': 'success',
                'sticky': False,
            }
        }
