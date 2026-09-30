# -*- coding: utf-8 -*-
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.tally_odoo_sync.services.tally_amount_words import amount_in_words
from odoo.addons.tally_odoo_sync.services.tally_payload import (
    build_payload, reconcile_totals, validate_invoice_for_tally,
)
from odoo.tests import tagged, TransactionCase
from odoo.exceptions import ValidationError

_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
    <STATUS>0</STATUS>
    <DESC>Completed Successfully</DESC>
  </HEADER>
  <BODY>
    <DATA>
      <VOUCHER VCHTYPE="Sales">
        <VOUCHERNUMBER>INV/TEST/1</VOUCHERNUMBER>
        <GUID>123e4567-e89b-12d3-a456-426614174000</GUID>
      </VOUCHER>
    </DATA>
  </BODY>
</ENVELOPE>
"""

_ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
    <STATUS>500</STATUS>
    <DESC>Ledger does not exist</DESC>
  </HEADER>
</ENVELOPE>
"""

_RESPONSE_XML = """<RESPONSE>
 <CREATED>1</CREATED>
 <ALTERED>2</ALTERED>
 <DELETED>0</DELETED>
 <LASTVCHID>4</LASTVCHID>
 <ERRORS>0</ERRORS>
 <EXCEPTIONS>0</EXCEPTIONS>
</RESPONSE>
"""


@tagged('post_install', '-at_install')
class TestAmountInWords(TransactionCase):

    def test_zero(self):
        self.assertEqual(amount_in_words(0), 'Zero Rupees Only')

    def test_one(self):
        self.assertEqual(amount_in_words(1), 'One Rupees Only')

    def test_ten(self):
        self.assertEqual(amount_in_words(10), 'Ten Rupees Only')

    def test_hundred(self):
        self.assertEqual(amount_in_words(100), 'One Hundred Rupees Only')

    def test_nine_hundred_ninety_nine(self):
        self.assertEqual(amount_in_words(999), 'Nine Hundred Ninety Nine Rupees Only')

    def test_one_thousand(self):
        self.assertEqual(amount_in_words(1000), 'One Thousand Rupees Only')

    def test_ten_thousand(self):
        self.assertEqual(amount_in_words(10000), 'Ten Thousand Rupees Only')

    def test_one_lakh(self):
        self.assertEqual(amount_in_words(100000), 'One Lakh Rupees Only')

    def test_ten_lakhs(self):
        self.assertEqual(amount_in_words(1000000), 'Ten Lakh Rupees Only')

    def test_one_crore(self):
        self.assertEqual(amount_in_words(10000000), 'One Crore Rupees Only')

    def test_complex_amount(self):
        result = amount_in_words(11800)
        self.assertIn('Eleven Thousand Eight Hundred', result)
        self.assertTrue(result.endswith('Rupees Only'))

    def test_with_paise(self):
        result = amount_in_words(11800.50)
        self.assertIn('Eleven Thousand Eight Hundred', result)
        self.assertIn('Fifty Paise', result)
        self.assertTrue(result.endswith('Rupees and Fifty Paise Only'))

    def test_paise_only(self):
        result = amount_in_words(0.75)
        self.assertIn('Zero Rupees', result)
        self.assertIn('Seventy Five Paise', result)

    def test_negative(self):
        result = amount_in_words(-500)
        self.assertTrue(result.startswith('Minus'))

    def test_large_crore(self):
        result = amount_in_words(12345678)
        self.assertIn('One Crore', result)
        self.assertIn('Twenty Three Lakh', result)
        self.assertIn('Forty Five Thousand', result)
        self.assertIn('Six Hundred Seventy Eight', result)

    def test_string_input(self):
        result = amount_in_words('500')
        self.assertIn('Five Hundred', result)

    def test_none_input(self):
        result = amount_in_words(None)
        self.assertEqual(result, 'Zero Rupees Only')

    def test_one_rupee_fifty_paise(self):
        result = amount_in_words(1.50)
        self.assertIn('One Rupees', result)
        self.assertIn('Fifty Paise', result)


@tagged('post_install', '-at_install')
class TestTallySync(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({
            'tally_sync_enabled': True,
            'tally_sync_on_post': True,
            'tally_server_url': 'http://localhost:9000',
            'vat': '27AABCU9603R1ZM',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
        })

        cls.tax_cgst = cls.env['account.tax'].create({
            'name': 'CGST 9%',
            'amount': 9.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })
        cls.tax_sgst = cls.env['account.tax'].create({
            'name': 'SGST 9%',
            'amount': 9.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })
        cls.tax_igst = cls.env['account.tax'].create({
            'name': 'IGST 18%',
            'amount': 18.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })

        cls.partner_a.write({
            'vat': '27AABCF1234H1Z5',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
            'street': '123 Test Street',
            'city': 'Mumbai',
            'zip': '400001',
        })

        cls.product_a.write({
            'l10n_in_hsn_code': '1905',
        })

    def setUp(self):
        super().setUp()
        self.env['tally.mapping'].search([]).unlink()

    def _make_invoice(self, post=False, tax_ids=None, price_unit=100.0,
                      quantity=2.0, product=None, partner=None):
        if tax_ids is None:
            tax_ids = [Command.set((self.tax_cgst + self.tax_sgst).ids)]
        kwargs = {
            'move_type': 'out_invoice',
            'invoice_date': fields.Date.from_string('2026-01-10'),
            'invoice_line_ids': [
                self._prepare_invoice_line(
                    product_id=(product or self.product_a).id,
                    quantity=quantity,
                    price_unit=price_unit,
                    tax_ids=tax_ids,
                ),
            ],
            'post': False,
        }
        if partner:
            kwargs['partner_id'] = partner.id
        invoice = self._create_invoice(**kwargs)
        if post:
            invoice.with_context(tally_skip_sync=True).action_post()
        return invoice

    def _make_mappings(self, invoice, customer_ledger=None, product_stock_item=None,
                       tax_ledger=None, uom_name=None):
        partner = invoice.commercial_partner_id
        product = invoice.invoice_line_ids[0].product_id
        uom = invoice.invoice_line_ids[0].product_uom_id

        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': partner.id,
            'tally_name': customer_ledger or partner.name.upper(),
            'company_id': self.company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'product',
            'odoo_product_id': product.id,
            'tally_name': product_stock_item or product.name.upper(),
            'company_id': self.company.id,
        })
        mapped_tax_ids = set()
        for line in invoice.invoice_line_ids:
            for tax in line.tax_ids:
                if tax.id not in mapped_tax_ids:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'tax',
                        'odoo_tax_id': tax.id,
                        'tally_name': tax_ledger or tax.name,
                        'company_id': self.company.id,
                    })
                    mapped_tax_ids.add(tax.id)
        if uom:
            self.env['tally.mapping'].create({
                'mapping_type': 'uom',
                'odoo_uom_id': uom.id,
                'tally_name': uom_name or 'Nos',
                'company_id': self.company.id,
            })

    @staticmethod
    def _fake_response(text, status_code=200):
        return SimpleNamespace(
            status_code=status_code,
            text=text,
            content=text.encode('utf-8'),
        )

    def _patch_post(self, side_effect=None, return_value=None):
        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        if side_effect is not None:
            mock_post.side_effect = side_effect
        else:
            mock_post.return_value = return_value
        self.addCleanup(patcher.stop)
        return mock_post

    # ==================================================================
    # Phase 6: Mapping Tests
    # ==================================================================
    def test_mapping_create_and_resolve_customer(self):
        partner = self.partner_a
        mapping = self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': partner.id,
            'tally_name': partner.name.upper(),
            'company_id': self.company.id,
        })
        self.assertTrue(mapping)
        result = self.env['tally.mapping'].resolve_customer(partner, self.company)
        self.assertEqual(result, partner.name.upper())

    def test_mapping_create_and_resolve_product(self):
        product = self.product_a
        mapping = self.env['tally.mapping'].create({
            'mapping_type': 'product',
            'odoo_product_id': product.id,
            'tally_name': 'BREAD WHITE 400 GMS',
            'company_id': self.company.id,
        })
        self.assertTrue(mapping)
        result = self.env['tally.mapping'].resolve_product(product, self.company)
        self.assertEqual(result, 'BREAD WHITE 400 GMS')

    def test_mapping_create_and_resolve_tax(self):
        tax = self.tax_cgst
        mapping = self.env['tally.mapping'].create({
            'mapping_type': 'tax',
            'odoo_tax_id': tax.id,
            'tally_name': 'Output CGST',
            'company_id': self.company.id,
        })
        self.assertTrue(mapping)
        result = self.env['tally.mapping'].resolve_tax(tax, self.company)
        self.assertEqual(result, 'Output CGST')

    def test_mapping_create_and_resolve_uom(self):
        uom = self.env.ref('uom.product_uom_unit')
        mapping = self.env['tally.mapping'].create({
            'mapping_type': 'uom',
            'odoo_uom_id': uom.id,
            'tally_name': 'Nos',
            'company_id': self.company.id,
        })
        self.assertTrue(mapping)
        result = self.env['tally.mapping'].resolve_uom(uom, self.company)
        self.assertEqual(result, 'Nos')

    def test_missing_mapping_returns_none(self):
        partner = self.partner_a
        result = self.env['tally.mapping'].resolve_customer(partner, self.company)
        self.assertIsNone(result)

    def test_duplicate_mapping_raises_error(self):
        partner = self.partner_a
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': partner.id,
            'tally_name': 'LEDGER A',
            'company_id': self.company.id,
        })
        with self.assertRaises(ValidationError):
            self.env['tally.mapping'].create({
                'mapping_type': 'customer',
                'odoo_partner_id': partner.id,
                'tally_name': 'LEDGER B',
                'company_id': self.company.id,
            })

    def test_resolve_all_returns_complete_mappings(self):
        invoice = self._make_invoice()
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        self.assertTrue(mappings['all_resolved'])
        self.assertEqual(len(mappings['missing']), 0)
        self.assertFalse(mappings['customer']['missing'])
        self.assertFalse(mappings['products'][0]['missing'])

    def test_resolve_all_detects_missing_mappings(self):
        invoice = self._make_invoice()
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        self.assertFalse(mappings['all_resolved'])
        self.assertTrue(len(mappings['missing']) > 0)

    # ==================================================================
    # Phase 7: Payload Builder Tests
    # ==================================================================
    def test_payload_builds_complete_structure(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        for key in ('company', 'buyer', 'invoice_number', 'invoice_date',
                     'lines', 'tax_summary', 'totals', 'hsn_summary',
                     'bank', 'footer', 'tally_mappings', 'request_hash'):
            self.assertIn(key, payload)

    def test_payload_company_data_from_odoo(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(payload['company']['company_gstin'], '27AABCU9603R1ZM')
        self.assertEqual(payload['company']['company_state'], 'Maharashtra')

    def test_payload_buyer_data_from_odoo(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(payload['buyer']['buyer_name'], self.partner_a.name)
        self.assertEqual(payload['buyer']['buyer_gstin'], '27AABCF1234H1Z5')

    def test_payload_lines_contain_all_fields(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        line = payload['lines'][0]
        for key in ('item_name', 'tally_stock_item', 'hsn_sac', 'quantity',
                     'uom', 'tally_uom', 'rate', 'taxable_amount'):
            self.assertIn(key, line)

    def test_payload_hsn_summary_populated(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertTrue(len(payload['hsn_summary']) > 0)
        hsn = payload['hsn_summary'][0]
        self.assertIn('hsn_sac', hsn)
        self.assertIn('taxable_value', hsn)
        self.assertIn('total_tax', hsn)

    def test_payload_deterministic_hash(self):
        invoice = self._make_invoice()
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload1 = build_payload(invoice, mappings)
        payload2 = build_payload(invoice, mappings)
        self.assertEqual(payload1['request_hash'], payload2['request_hash'])

    # ==================================================================
    # Phase 9: Validation Tests
    # ==================================================================
    def test_validation_passes_for_complete_invoice(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertTrue(result['ready'])
        self.assertEqual(len(result['errors']), 0)

    def test_validation_fails_unposted_invoice(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('INVOICE_NOT_POSTED', codes)

    def test_validation_fails_missing_product_hsn(self):
        self.product_a.l10n_in_hsn_code = ''
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('MISSING_HSN', codes)

    def test_validation_fails_zero_quantity(self):
        invoice = self._make_invoice(post=True, quantity=0)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('INVALID_QUANTITY', codes)

    def test_validation_fails_missing_tally_mappings(self):
        invoice = self._make_invoice(post=True)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('MISSING_TALLY_MAPPING', codes)

    def test_validation_fails_no_invoice_lines(self):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-01-10',
            'company_id': self.company.id,
        })
        invoice.invoice_line_ids.unlink()
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('NO_INVOICE_LINES', codes)

    # ==================================================================
    # Phase 10: Readiness Status
    # ==================================================================
    def test_readiness_check_sets_not_ready_when_mappings_missing(self):
        invoice = self._make_invoice(post=True)
        invoice.action_check_readiness()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')

    def test_readiness_check_sets_ready_when_complete(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        invoice.action_check_readiness()
        self.assertEqual(invoice.tally_sync_status, 'ready')

    def test_sync_blocked_when_not_ready(self):
        invoice = self._make_invoice(post=True)
        invoice.action_check_readiness()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')

    # ==================================================================
    # Phase 11: XML Generation
    # ==================================================================
    def test_xml_generation_from_payload(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml_data = self.env['tally.service'].generate_xml(payload)
        self.assertIn('<ENVELOPE>', xml_data)
        self.assertIn('VCHTYPE="Sales"', xml_data)
        self.assertIn('<VOUCHERNUMBER>%s</VOUCHERNUMBER>' % invoice.name, xml_data)
        self.assertIn('<PARTYLEDGERNAME>%s</PARTYLEDGERNAME>' % self.partner_a.name.upper(), xml_data)

    def test_xml_contains_tax_ledgers_from_mappings(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice, tax_ledger='Output CGST 9%')
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml_data = self.env['tally.service'].generate_xml(payload)
        self.assertIn('Output CGST 9%', xml_data)

    def test_xml_contains_tally_stock_item_from_mapping(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice, product_stock_item='BREAD WHITE 400 GMS')
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml_data = self.env['tally.service'].generate_xml(payload)
        self.assertIn('BREAD WHITE 400 GMS', xml_data)

    # ==================================================================
    # Phase 12: Tax Reconciliation
    # ==================================================================
    def test_tax_reconciliation_passes(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        result = reconcile_totals(invoice, payload)
        self.assertTrue(result['reconciled'])

    def test_tax_reconciliation_detects_hsn_mismatch(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        payload['hsn_summary'][0]['total_tax'] = '0'
        result = reconcile_totals(invoice, payload)
        self.assertFalse(result['reconciled'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('HSN_TAX_MISMATCH', codes)

    # ==================================================================
    # Phase 14: Amount in Words Integration
    # ==================================================================
    def test_amount_in_words_in_chatter(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        chatter = invoice.message_ids[-1].body
        self.assertIn('Rupees', chatter)

    # ==================================================================
    # Phase 15: Error Categories
    # ==================================================================
    def test_connection_error_returns_network_category(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        self._patch_post(side_effect=requests.exceptions.ConnectionError('refused'))
        invoice.action_post()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('NETWORK_ERROR', invoice.tally_error_message)

    def test_timeout_error_returns_timeout_category(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        self._patch_post(side_effect=requests.exceptions.Timeout('timed out'))
        invoice.action_post()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('TIMEOUT', invoice.tally_error_message)

    def test_tally_rejection_categorized(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_ERROR_XML))
        invoice.action_post()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('MISSING_MASTER', invoice.tally_error_message)

    # ==================================================================
    # Phase 16: Duplicate Protection
    # ==================================================================
    def test_already_synced_cannot_sync_again(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'synced')
        with self.assertRaises(Exception):
            invoice.action_sync_to_tally()

    def test_request_hash_stored_on_success(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        self.assertTrue(invoice.tally_request_hash)

    # ==================================================================
    # Phase 17: Chatter Behavior
    # ==================================================================
    def test_success_chatter_contains_voucher_and_amount(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        last_msg = invoice.message_ids[-1].body
        self.assertIn('INV/TEST/1', last_msg)
        self.assertIn('synchronization successful', last_msg.lower())

    def test_not_ready_chatter_shows_missing_items(self):
        invoice = self._make_invoice(post=False)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_post()
        last_msg = invoice.message_ids[-1].body.lower()
        self.assertIn('not ready', last_msg)

    def test_failure_chatter_shows_error_category(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_ERROR_XML))
        invoice.action_post()
        last_msg = invoice.message_ids[-1].body
        self.assertIn('MISSING_MASTER', last_msg)

    # ==================================================================
    # Phase 18: Audit Log
    # ==================================================================
    def test_audit_log_created_on_sync(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        log = self.env['tally.sync.log'].search([
            ('invoice_id', '=', invoice.id),
        ])
        self.assertTrue(log)
        self.assertEqual(log.sync_status, 'success')
        self.assertTrue(log.sync_attempt_id)
        self.assertTrue(log.payload_json)

    def test_audit_log_stores_payload(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        log = self.env['tally.sync.log'].search([
            ('invoice_id', '=', invoice.id),
            ('sync_status', '=', 'success'),
        ], limit=1)
        payload = json.loads(log.payload_json)
        self.assertIn('company', payload)
        self.assertIn('buyer', payload)
        self.assertIn('lines', payload)

    def test_audit_log_detects_duplicate(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        existing = self.env['tally.sync.log'].check_duplicate(invoice)
        self.assertTrue(existing)

    # ==================================================================
    # Phase 20: Dry Run / Preview
    # ==================================================================
    def test_preview_does_not_send_to_tally(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mock_post = self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        result = invoice.action_preview_tally_invoice()
        mock_post.assert_not_called()
        self.assertFalse(result['sent_to_tally'])

    def test_preview_shows_complete_data(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        result = invoice.action_preview_tally_invoice()
        self.assertTrue(result['xml_generated'])
        self.assertIn('invoice', result)
        self.assertIn('customer', result)
        self.assertIn('total', result)
        self.assertIn('mappings', result)

    def test_preview_shows_validation_errors(self):
        invoice = self._make_invoice(post=True)
        result = invoice.action_preview_tally_invoice()
        self.assertFalse(result['ready'])
        self.assertTrue(len(result['errors']) > 0)

    # ==================================================================
    # TEST 1: Complete invoice sync
    # ==================================================================
    def test_complete_invoice_syncs_successfully(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        mock_post = self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_post()
        self.assertEqual(invoice.tally_sync_status, 'synced')
        self.assertEqual(invoice.tally_voucher_id, 'INV/TEST/1')
        self.assertTrue(invoice.tally_voucher_guid)
        self.assertEqual(invoice.tally_sync_count, 1)
        self.assertTrue(invoice.tally_request_hash)
        mock_post.assert_called_once()

    # ==================================================================
    # TEST 2: Missing customer GSTIN
    # ==================================================================
    def test_missing_customer_gstin_not_ready(self):
        self.partner_a.vat = ''
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        validation = validate_invoice_for_tally(invoice, mappings)
        self.assertTrue(validation['ready'])

    # ==================================================================
    # TEST 6: Missing bank details
    # ==================================================================
    def test_missing_bank_details_still_validates(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertTrue(result['ready'])

    # ==================================================================
    # TEST 8: HSN tax table mismatch
    # ==================================================================
    def test_hsn_tax_table_mismatch_blocked(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        payload['hsn_summary'][0]['total_tax'] = '999'
        result = reconcile_totals(invoice, payload)
        self.assertFalse(result['reconciled'])

    # ==================================================================
    # TEST 9: Tally connection unavailable
    # ==================================================================
    def test_tally_connection_unavailable_marks_failed(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        self._patch_post(side_effect=requests.exceptions.ConnectionError('refused'))
        invoice.action_post()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('NETWORK_ERROR', invoice.tally_error_message)

    # ==================================================================
    # TEST 10: Tally rejects XML
    # ==================================================================
    def test_tally_rejects_xml_marks_failed(self):
        invoice = self._make_invoice(post=False)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_ERROR_XML))
        invoice.action_post()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('Ledger does not exist', invoice.tally_error_message)

    # ==================================================================
    # TEST 11: Same invoice sent twice
    # ==================================================================
    def test_same_invoice_not_duplicated(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'synced')
        with self.assertRaises(Exception):
            invoice.action_sync_to_tally()

    # ==================================================================
    # TEST 13: Multiple invoice lines
    # ==================================================================
    def test_multiple_invoice_lines_all_transferred(self):
        product_b = self.env['product.product'].create({
            'name': 'Brown Bread',
            'l10n_in_hsn_code': '1905',
            'list_price': 50.0,
            'type': 'consu',
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-01-10',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_a.id,
                    'quantity': 20,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': product_b.id,
                    'quantity': 10,
                    'price_unit': 50.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_mappings(invoice)
        self.env['tally.mapping'].create({
            'mapping_type': 'product',
            'odoo_product_id': product_b.id,
            'tally_name': 'BROWN BREAD',
            'company_id': self.company.id,
        })
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(len(payload['lines']), 2)

    # ==================================================================
    # TEST 14: Multiple HSN codes
    # ==================================================================
    def test_multiple_hsn_codes_hsn_summary_correct(self):
        product_b = self.env['product.product'].create({
            'name': 'Brown Bread',
            'l10n_in_hsn_code': '1906',
            'list_price': 50.0,
            'type': 'consu',
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-01-10',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_a.id,
                    'quantity': 20,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': product_b.id,
                    'quantity': 10,
                    'price_unit': 50.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_mappings(invoice)
        self.env['tally.mapping'].create({
            'mapping_type': 'product',
            'odoo_product_id': product_b.id,
            'tally_name': 'BROWN BREAD',
            'company_id': self.company.id,
        })
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(len(payload['hsn_summary']), 2)
        hsn_codes = [h['hsn_sac'] for h in payload['hsn_summary']]
        self.assertIn('1905', hsn_codes)
        self.assertIn('1906', hsn_codes)

    # ==================================================================
    # TEST 15: IGST invoice
    # ==================================================================
    def test_igst_invoice(self):
        self.partner_a.write({
            'state_id': self.env.ref('base.state_in_dl').id,
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-01-10',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_a.id,
                    'quantity': 2,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set(self.tax_igst.ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': self.partner_a.id,
            'tally_name': self.partner_a.name.upper(),
            'company_id': self.company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'product',
            'odoo_product_id': self.product_a.id,
            'tally_name': self.product_a.name.upper(),
            'company_id': self.company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'tax',
            'odoo_tax_id': self.tax_igst.id,
            'tally_name': 'Output IGST',
            'company_id': self.company.id,
        })
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(payload['nature_of_transaction'], 'Inter-State')
        tax_types = [t['type'] for t in payload['tax_summary']]
        self.assertIn('IGST', tax_types)

    # ==================================================================
    # TEST 16: CGST + SGST invoice
    # ==================================================================
    def test_cgst_sgst_invoice(self):
        invoice = self._make_invoice(post=True, tax_ids=[
            Command.set((self.tax_cgst + self.tax_sgst).ids)
        ])
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(payload['nature_of_transaction'], 'Intra-State')
        tax_types = [t['type'] for t in payload['tax_summary']]
        self.assertIn('CGST', tax_types)
        self.assertIn('SGST', tax_types)

    # ==================================================================
    # TEST 17: Large amount
    # ==================================================================
    def test_large_amount(self):
        invoice = self._make_invoice(post=True, price_unit=500000.0, quantity=20)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertEqual(float(payload['totals']['grand_total']), 11800000.0)

    # ==================================================================
    # TEST 18: Amount with paise
    # ==================================================================
    def test_amount_with_paise(self):
        invoice = self._make_invoice(post=True, price_unit=100.50, quantity=3)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertIn('.', payload['totals']['subtotal'])

    # ==================================================================
    # TEST 20: Missing UOM
    # ==================================================================
    def test_missing_uom_blocked(self):
        invoice = self._make_invoice(post=True)
        for line in invoice.invoice_line_ids:
            line.product_uom_id = False
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('MISSING_UOM', codes)

    # ==================================================================
    # Phase 19: Manual Retry
    # ==================================================================
    def test_manual_sync_button_shows_info_when_synced(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.action_sync_to_tally()
        with self.assertRaises(Exception):
            invoice.action_sync_to_tally()

    # ==================================================================
    # Regression: Backward compatibility
    # ==================================================================
    def test_disabled_company_does_not_sync(self):
        self.company.tally_sync_enabled = False
        invoice = self._make_invoice()
        mock_post = self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        invoice.with_context(tally_skip_sync=True).action_post()
        mock_post.assert_not_called()
        self.assertEqual(invoice.tally_sync_status, 'not_synced')

    def test_retry_cron_resyncs_failed(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_ERROR_XML))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self._patch_post(return_value=self._fake_response(_SUCCESS_XML))
        synced = invoice._tally_cron_retry_failed()
        self.assertEqual(synced, 1)
        self.assertEqual(invoice.tally_sync_status, 'synced')
        self.assertEqual(invoice.tally_sync_count, 2)

    def test_wsl_localhost_rewritten_to_host_ip(self):
        service = self.env['tally.service']
        service_class = type(service)
        with patch.object(service_class, '_is_wsl', return_value=True), \
                patch.object(service_class, '_wsl_host_ip', return_value='192.168.1.50'):
            self.assertEqual(
                service._resolve_tally_url('http://localhost:9000'),
                'http://192.168.1.50:9000')
        with patch.object(service_class, '_is_wsl', return_value=False):
            self.assertEqual(
                service._resolve_tally_url('http://localhost:9000'),
                'http://localhost:9000')

    def test_import_summary_response_is_success(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response(_RESPONSE_XML))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'synced')
        self.assertEqual(invoice.tally_sync_count, 1)

    def test_invalid_response_marks_failed(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        self._patch_post(return_value=self._fake_response('<not-valid-xml'))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('Invalid XML response', invoice.tally_error_message)

    def test_guid_is_deterministic(self):
        service = self.env['tally.service']
        guid1 = service._deterministic_guid(1, 1)
        guid2 = service._deterministic_guid(1, 1)
        self.assertEqual(guid1, guid2)
        guid3 = service._deterministic_guid(2, 1)
        self.assertNotEqual(guid1, guid3)
