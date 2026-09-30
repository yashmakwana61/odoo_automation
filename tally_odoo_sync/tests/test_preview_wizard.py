# -*- coding: utf-8 -*-
import json
import xml.etree.ElementTree as ET
from unittest.mock import patch
from types import SimpleNamespace

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.tally_odoo_sync.services.tally_amount_words import amount_in_words
from odoo.addons.tally_odoo_sync.services.tally_payload import (
    build_payload, reconcile_totals, validate_invoice_for_tally,
)
from odoo.tests import tagged

_SUCCESS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST><STATUS>0</STATUS><DESC>Completed Successfully</DESC></HEADER>
  <BODY><DATA><VOUCHER VCHTYPE="Sales"><VOUCHERNUMBER>INV/TEST/1</VOUCHERNUMBER><GUID>123e4567-e89b-12d3-a456-426614174000</GUID></VOUCHER></DATA></BODY>
</ENVELOPE>
"""


@tagged('post_install', '-at_install')
class TestPreviewWizard(AccountTestInvoicingCommon):

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
            'tally_company_name': 'Test Preview Co',
            'tally_declaration': 'Subject to Mumbai jurisdiction',
            'tally_signatory': 'CEO',
            'company_cin': 'U12345MH2020PTC123456',
            'company_pan': 'AABCU9603R',
            'company_lic': 'LIC123456',
        })
        cls.tax_cgst = cls.env['account.tax'].create({
            'name': 'CGST 9%', 'amount': 9.0, 'amount_type': 'percent', 'type_tax_use': 'sale', 'company_id': cls.company.id,
        })
        cls.tax_sgst = cls.env['account.tax'].create({
            'name': 'SGST 9%', 'amount': 9.0, 'amount_type': 'percent', 'type_tax_use': 'sale', 'company_id': cls.company.id,
        })
        cls.partner_a.write({
            'vat': '27AABCF1234H1Z5',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
            'street': '123 Test Street', 'city': 'Mumbai', 'zip': '400001',
        })
        cls.product_a.write({'l10n_in_hsn_code': '1905'})

    def setUp(self):
        super().setUp()
        self.env['tally.mapping'].search([]).unlink()

    def _make_invoice(self, post=True, quantity=2.0, price_unit=100.0):
        invoice = self._create_invoice(
            move_type='out_invoice',
            invoice_date=fields.Date.from_string('2026-01-10'),
            invoice_line_ids=[
                self._prepare_invoice_line(
                    product_id=self.product_a.id,
                    quantity=quantity, price_unit=price_unit,
                    tax_ids=[Command.set((self.tax_cgst + self.tax_sgst).ids)],
                ),
            ],
            post=False,
        )
        if post:
            invoice.with_context(tally_skip_sync=True).action_post()
        return invoice

    def _make_mappings(self, invoice):
        partner = invoice.commercial_partner_id
        product = invoice.invoice_line_ids[0].product_id
        uom = invoice.invoice_line_ids[0].product_uom_id
        self.env['tally.mapping'].create({
            'mapping_type': 'customer', 'odoo_partner_id': partner.id,
            'tally_name': partner.name.upper(), 'company_id': self.company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'product', 'odoo_product_id': product.id,
            'tally_name': product.name.upper(), 'company_id': self.company.id,
        })
        for tax in invoice.invoice_line_ids[0].tax_ids:
            self.env['tally.mapping'].create({
                'mapping_type': 'tax', 'odoo_tax_id': tax.id,
                'tally_name': tax.name, 'company_id': self.company.id,
            })
        if uom:
            self.env['tally.mapping'].create({
                'mapping_type': 'uom', 'odoo_uom_id': uom.id,
                'tally_name': 'Nos', 'company_id': self.company.id,
            })

    @staticmethod
    def _fake_response(text, status_code=200):
        return SimpleNamespace(status_code=status_code, text=text, content=text.encode('utf-8'))

    # 1. Preview button action returns wizard
    def test_preview_wizard_action_returns_act_window(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'tally.invoice.preview.wizard')
        self.assertEqual(action['target'], 'new')
        self.assertTrue(action['res_id'])

    # 2. Wizard opens with correct invoice
    def test_preview_wizard_opens_with_invoice(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.invoice_id.id, invoice.id)
        self.assertEqual(wizard.invoice_name, invoice.name)

    # 3. Preview does not send to Tally
    def test_preview_wizard_does_not_send_to_tally(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        with patch('odoo.addons.tally_odoo_sync.services.tally_service.requests.post') as mock_post:
            mock_post.return_value = self._fake_response(_SUCCESS_XML)
            wizard_action = invoice.action_preview_tally_invoice_wizard()
            mock_post.assert_not_called()
            # Also ensure sync state unchanged
            self.assertNotEqual(invoice.tally_sync_status, 'synced')
            self.assertNotEqual(invoice.tally_sync_status, 'syncing')

    # 4. Preview shows validation failures when not ready
    def test_preview_wizard_shows_validation_failures(self):
        invoice = self._make_invoice(post=True)
        # Do NOT create mappings -> not ready
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.readiness_status, 'not_ready')
        self.assertFalse(wizard.is_ready)
        self.assertIn('NOT READY', wizard.validation_html)
        self.assertIn('Customer ledger mapping', wizard.error_message or wizard.validation_html)

    # 5. Preview displays payload when ready
    def test_preview_wizard_displays_payload(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        self.assertTrue(wizard.has_payload)
        self.assertTrue(wizard.payload_json)
        payload = json.loads(wizard.payload_json)
        self.assertIn('company', payload)
        self.assertIn('buyer', payload)
        self.assertIn('lines', payload)
        self.assertIn('totals', payload)

    # 6. Preview displays XML when ready
    def test_preview_wizard_displays_xml(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        self.assertTrue(wizard.has_xml)
        self.assertIn('<ENVELOPE>', wizard.xml_content)
        self.assertIn('VCHTYPE="Sales"', wizard.xml_content)
        self.assertIn(invoice.name, wizard.xml_content)

    # 7. Payload contains required golden fields
    def test_payload_contains_required_fields(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        for key in ('company', 'buyer', 'invoice_number', 'invoice_date', 'lines', 'tax_summary', 'totals', 'hsn_summary', 'bank', 'footer', 'tally_mappings', 'amount_words'):
            self.assertIn(key, payload, msg='Missing payload key: %s' % key)
        # Company subfields
        for ck in ('company_name', 'company_gstin', 'company_address', 'company_state', 'company_pan'):
            self.assertIn(ck, payload['company'])
        # Buyer subfields
        for bk in ('buyer_name', 'buyer_gstin', 'buyer_state'):
            self.assertIn(bk, payload['buyer'])
        # Line subfields
        line = payload['lines'][0]
        for lk in ('item_name', 'hsn_sac', 'quantity', 'uom', 'rate', 'rate_including_tax', 'taxable_amount', 'line_total'):
            self.assertIn(lk, line)
        # Amount words present and correct
        self.assertTrue(payload['amount_words'])
        self.assertIn('Rupees', payload['amount_words'])

    # 8. XML contains required mapped fields
    def test_xml_contains_required_mapped_fields(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml_data = self.env['tally.service'].generate_xml(payload)
        root = ET.fromstring(xml_data)
        self.assertEqual(root.findtext('.//SVCURRENTCOMPANY'), self.company.tally_company_name)
        self.assertEqual(root.findtext('.//VOUCHERNUMBER'), invoice.name)
        self.assertIsNotNone(root.findtext('.//DATE'))
        self.assertIsNotNone(root.findtext('.//PARTYLEDGERNAME'))
        self.assertIsNotNone(root.findtext('.//LEDGERNAME'))

    # 9. Missing XML destination is detected (company PAN not in XML but in payload)
    def test_missing_xml_destination_detected(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml_data = self.env['tally.service'].generate_xml(payload)
        # Company PAN is in payload but NOT in XML (by design — Tally Company Master)
        self.assertEqual(payload['company']['company_pan'], 'AABCU9603R')
        self.assertNotIn('AABCU9603R', xml_data)
        # Document this as expected: PAN comes from Tally Company Master, not voucher XML
        # Test passes if payload has PAN but XML does not contain it

    # 10. Preview does not mark invoice synced
    def test_preview_does_not_mark_synced(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        prev_status = invoice.tally_sync_status
        prev_count = invoice.tally_sync_count
        invoice.action_preview_tally_invoice_wizard()
        invoice.invalidate_recordset()
        invoice2 = self.env['account.move'].browse(invoice.id)
        self.assertEqual(invoice2.tally_sync_status, prev_status)
        self.assertEqual(invoice2.tally_sync_count, prev_count)

    # 11. Preview does not create sync log
    def test_preview_does_not_create_sync_log(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        before = self.env['tally.sync.log'].search_count([('invoice_id', '=', invoice.id)])
        invoice.action_preview_tally_invoice_wizard()
        after = self.env['tally.sync.log'].search_count([('invoice_id', '=', invoice.id)])
        self.assertEqual(before, after)

    # 12. Preview shows readiness PASS/FAIL categories
    def test_preview_shows_readiness_categories(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        # Validation HTML should contain category table
        self.assertIn('Company', wizard.validation_html)
        self.assertIn('Customer', wizard.validation_html)
        self.assertIn('Product', wizard.validation_html)

    # 13. Amount words reconciliation
    def test_amount_words_matches_grand_total(self):
        invoice = self._make_invoice(post=True, price_unit=100.0, quantity=2)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        expected = amount_in_words(abs(invoice.amount_total))
        self.assertEqual(payload['amount_words'], expected)

    # 14. HSN summary reconciliation
    def test_hsn_summary_reconciles(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        result = reconcile_totals(invoice, payload)
        self.assertTrue(result['reconciled'])

    # 15. Golden field validation — all required fields present
    def test_golden_field_matrix(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        # Company
        self.assertEqual(payload['company']['company_name'], 'Test Preview Co')
        self.assertEqual(payload['company']['company_gstin'], '27AABCU9603R1ZM')
        self.assertEqual(payload['company']['company_pan'], 'AABCU9603R')
        self.assertEqual(payload['company']['company_cin'], 'U12345MH2020PTC123456')
        # Bank
        self.assertIn('bank_name', payload['bank'])
        # Totals
        self.assertIn('subtotal', payload['totals'])
        self.assertIn('grand_total', payload['totals'])
        # Footer
        self.assertEqual(payload['footer']['declaration'], 'Subject to Mumbai jurisdiction')

    # 16. Duplicate protection still works after preview
    def test_duplicate_protection_after_preview(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        with patch('odoo.addons.tally_odoo_sync.services.tally_service.requests.post') as mock_post:
            mock_post.return_value = self._fake_response(_SUCCESS_XML)
            # Preview first
            invoice.action_preview_tally_invoice_wizard()
            # Then sync
            invoice.action_sync_to_tally()
            self.assertEqual(invoice.tally_sync_status, 'synced')
            # Second sync should be blocked
            with self.assertRaises(Exception):
                invoice.action_sync_to_tally()

    # 17. Preview wizard readonly behavior (no write to invoice)
    def test_preview_wizard_no_side_effect_on_chatter(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        before_msgs = len(invoice.message_ids)
        invoice.action_preview_tally_invoice_wizard()
        after_msgs = len(invoice.message_ids)
        self.assertEqual(before_msgs, after_msgs)

    # 18. Preview handles not-ready with specific missing items
    def test_preview_not_ready_lists_missing_hsn(self):
        self.product_a.l10n_in_hsn_code = ''
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.readiness_status, 'not_ready')
        self.assertIn('HSN', wizard.validation_html or wizard.error_message)

    # 19. Tax reconciliation displayed in wizard
    def test_preview_wizard_tax_html_present(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        action = invoice.action_preview_tally_invoice_wizard()
        wizard = self.env['tally.invoice.preview.wizard'].browse(action['res_id'])
        self.assertIn('CGST', wizard.tax_html)
        self.assertIn('SGST', wizard.tax_html)

    # 20. Bank details in payload
    def test_bank_details_in_payload(self):
        invoice = self._make_invoice(post=True)
        self._make_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        self.assertIn('bank', payload)
        self.assertIn('bank_name', payload['bank'])
        self.assertIn('ifsc', payload['bank'])
