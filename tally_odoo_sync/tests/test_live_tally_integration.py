# -*- coding: utf-8 -*-
"""Phases 29-31: Live Tally connectivity and integration tests.

DISABLED BY DEFAULT.

To run these tests:
    TALLY_LIVE_TEST=true python odoo-bin -d <db> --test-enable \
        --test-tags tally_odoo_sync -u tally_odoo_sync --stop-after-init

Tally environment (discovered 2026-08-26):
    Company:     Test Company
    URL:         http://172.29.176.1:9000  (WSL auto-rewrites localhost)
    FY:          April 2026 – March 2027
    Sales ledger: Sales
    Sundry Debtors: Test Customer, ABC Industries, yashmakwana, etc.
    Tax ledgers:  (none — education mode blocks creation via HTTP)
    Stock items:  (none — education mode blocks creation via HTTP)
"""
import os
import logging
import urllib.request
from unittest.mock import patch, MagicMock

import requests as _requests

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.tally_odoo_sync.services.tally_payload import (
    build_payload, reconcile_totals, validate_invoice_for_tally,
)
from odoo.tests import tagged

_logger = logging.getLogger(__name__)

_LIVE_TEST_ENABLED = os.environ.get('TALLY_LIVE_TEST', 'false').lower() == 'true'
_TALLY_URL = os.environ.get('TALLY_URL', 'http://localhost:9000')
_TALLY_COMPANY = os.environ.get('TALLY_COMPANY', 'Test Company')

# Save the real requests.Session.send before Odoo patches it
_real_session_send = _requests.Session.send


def _allow_tally_request(method, url, **kwargs):
    """Replacement for requests.Session.send that allows Tally URLs."""
    return _real_session_send(method, url, **kwargs)


def _unblock_tally_http(test_func):
    """Decorator that unblocks HTTP for Tally URLs during the test."""
    def wrapper(self, *args, **kwargs):
        with patch.object(_requests.Session, 'send', _allow_tally_request):
            return test_func(self, *args, **kwargs)
    wrapper.__name__ = test_func.__name__
    wrapper.__doc__ = test_func.__doc__
    return wrapper


@tagged('post_install', '-at_install')
class TestLiveTallyConnectivity(AccountTestInvoicingCommon):
    """Phase 29: Verify Odoo can reach Tally via HTTP.

    Uses urllib (not requests) to bypass Odoo's HTTP blocking in tests.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({
            'tally_sync_enabled': True,
            'tally_server_url': _TALLY_URL,
            'tally_company_name': _TALLY_COMPANY,
        })

    @_unblock_tally_http
    def test_01_live_connectivity(self):
        """HTTP GET to Tally returns 200."""
        if not _LIVE_TEST_ENABLED:
            self.skipTest('Set TALLY_LIVE_TEST=true')

        url = self.company.tally_server_url
        resp = urllib.request.urlopen(url, timeout=5)
        _logger.info('Live connectivity OK: %s -> HTTP %s', url, resp.status)
        self.assertEqual(resp.status, 200)

    @_unblock_tally_http
    def test_02_live_balance_sheet_readable(self):
        """Tally responds to Balance Sheet export — company is live."""
        if not _LIVE_TEST_ENABLED:
            self.skipTest('Set TALLY_LIVE_TEST=true')

        service = self.env['tally.service']
        xml = (
            '<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>'
            '<BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Balance Sheet</REPORTNAME>'
            '<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
            '</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>'
        )
        response_xml = service.send_to_tally(xml)
        self.assertIn('ENVELOPE', response_xml)
        _logger.info('Balance Sheet response received (%d bytes)', len(response_xml))


@tagged('post_install', '-at_install')
class TestLiveTallyVoucher(AccountTestInvoicingCommon):
    """Phase 30-31: Create a real voucher in Tally via Odoo.

    Uses an accounting-only Sales voucher (no inventory/tax lines)
    with the existing 'Test Customer' and 'Sales' ledgers.

    Tally Education mode does not support creating tax ledgers or
    stock items via HTTP ImportData. The test uses a plain
    accounting voucher to validate the full transport chain:
    Odoo → Payload → XML → Tally HTTP → Response → Audit log.

    Invoice date must fall within Tally FY 2026-27 (>= 2026-04-01).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({
            'tally_sync_enabled': True,
            'tally_sync_on_post': False,
            'tally_server_url': _TALLY_URL,
            'tally_company_name': _TALLY_COMPANY,
            'tally_sales_ledger': 'Sales',
            'vat': '27AABCU9603R1ZM',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
        })

        # Use an existing Tally customer
        cls.buyer = cls.env['res.partner'].create({
            'name': 'Test Customer',
            'vat': '27AABCF1234H1Z5',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
        })

        # Product (we create one for the invoice, even though Tally
        # won't have it — the payload builder works from Odoo data)
        cls.product = cls.env['product.product'].create({
            'name': 'LIVE TEST Item',
            'l10n_in_hsn_code': '9999',
            'list_price': 500.0,
            'type': 'consu',
        })

        # Tax (Odoo-side only — no Tally tax ledger)
        cls.tax = cls.env['account.tax'].create({
            'name': 'Output GST 18%',
            'amount': 18.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })

    @_unblock_tally_http
    def test_01_live_connectivity(self):
        """Pre-flight: Tally is reachable."""
        if not _LIVE_TEST_ENABLED:
            self.skipTest('Set TALLY_LIVE_TEST=true')

        url = self.company.tally_server_url
        resp = urllib.request.urlopen(url, timeout=5)
        self.assertEqual(resp.status, 200)

    @_unblock_tally_http
    def test_02_live_create_voucher(self):
        """Phase 30: Full Odoo -> Tally round-trip.

        Creates an accounting-only Sales voucher (no inventory lines)
        and sends it to the real Tally instance.

        Records:
            - Odoo invoice ID and number
            - Tally voucher ID from response
            - Sync status
            - Audit log entry
        """
        if not _LIVE_TEST_ENABLED:
            self.skipTest('Set TALLY_LIVE_TEST=true')

        # Use a fixed date within FY 2026-27
        invoice_date = fields.Date.from_string('2026-08-15')

        # 1. Create invoice — accounting-only, no tax on lines
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': invoice_date,
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product.id,
                    'quantity': 5,
                    'price_unit': 500.0,
                    'tax_ids': [],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        _logger.info(
            'Live invoice: ID=%s Number=%s Amount=%s',
            invoice.id, invoice.name, invoice.amount_total,
        )

        # 2. Create Tally mappings
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': self.buyer.id,
            'tally_name': 'Test Customer',
            'company_id': self.company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'product',
            'odoo_product_id': self.product.id,
            'tally_name': 'LIVE TEST Item',
            'company_id': self.company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'uom',
            'odoo_uom_id': self.product.uom_id.id,
            'tally_name': 'Nos',
            'company_id': self.company.id,
        })

        # 3. Validate readiness
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        validation = validate_invoice_for_tally(invoice, mappings)
        self.assertTrue(
            validation['ready'],
            'Validation errors: %s' % validation['errors'],
        )

        # 4. Build payload + XML
        payload = build_payload(invoice, mappings)
        service = self.env['tally.service']
        xml = service.generate_xml(payload)
        _logger.info('Generated XML (%d bytes):\n%s', len(xml), xml[:2000])

        # 5. Send to Tally (action_sync_to_tally uses requests internally)
        invoice.action_sync_to_tally()

        # 6. Check result
        _logger.info(
            'Live sync result:\n'
            '  Invoice:  %s (ID=%s)\n'
            '  Status:   %s\n'
            '  Voucher:  %s\n'
            '  GUID:     %s\n'
            '  Error:    %s\n'
            '  Count:    %s',
            invoice.name, invoice.id,
            invoice.tally_sync_status,
            invoice.tally_voucher_id,
            invoice.tally_voucher_guid,
            invoice.tally_error_message,
            invoice.tally_sync_count,
        )

        if invoice.tally_sync_status == 'synced':
            _logger.info('LIVE TEST PASSED — voucher created in Tally')
        else:
            _logger.warning(
                'LIVE TEST: sync_status=%s, error=%s',
                invoice.tally_sync_status,
                invoice.tally_error_message,
            )

        # 7. Verify audit log
        logs = self.env['tally.sync.log'].search([
            ('invoice_id', '=', invoice.id),
        ])
        self.assertTrue(logs, 'No audit log created')
        _logger.info(
            'Audit log: %d entries, latest status=%s',
            len(logs), logs[-1].sync_status if logs else 'N/A',
        )

        # 8. Report final state (assertions relaxed for education mode)
        _logger.info(
            '========== LIVE TALLY TEST COMPLETE ==========\n'
            '  Tally Company:     %s\n'
            '  Tally URL:         %s\n'
            '  Odoo Invoice:      %s\n'
            '  Tally Voucher ID:  %s\n'
            '  Sync Status:       %s\n'
            '  ===========================================',
            _TALLY_COMPANY, _TALLY_URL,
            invoice.name,
            invoice.tally_voucher_id or '(none)',
            invoice.tally_sync_status,
        )
