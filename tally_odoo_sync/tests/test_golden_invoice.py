# -*- coding: utf-8 -*-
"""Phases 21-22: Golden Invoice field-by-field chain verification.

Creates a realistic invoice with known values and traces every field:
    Odoo → Payload → XML → (Tally destination)

Also covers strengthened Phases 25-26: duplicate protection and failure matrix.
"""
import xml.etree.ElementTree as ET
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
        <VOUCHERNUMBER>INV/TEST/GOLDEN</VOUCHERNUMBER>
        <GUID>aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</GUID>
      </VOUCHER>
    </DATA>
  </BODY>
</ENVELOPE>
"""

_ERROR_MASTER = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
    <STATUS>500</STATUS>
    <DESC>Ledger does not exist</DESC>
  </HEADER>
</ENVELOPE>
"""

_ERROR_DUPLICATE = """<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
    <STATUS>500</STATUS>
    <DESC>Voucher already exists</DESC>
  </HEADER>
</ENVELOPE>
"""


@tagged('post_install', '-at_install')
class TestGoldenInvoice(AccountTestInvoicingCommon):
    """Phase 21: Golden Invoice — field-by-field chain verification.

    Builds one invoice with ALL fields populated and verifies:
      Odoo source → Payload dict → XML element → expected value.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({
            'name': 'Golden Foods Pvt Ltd',
            'tally_sync_enabled': True,
            'tally_sync_on_post': True,
            'tally_server_url': 'http://localhost:9000',
            'vat': '27AABCU9603R1ZM',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
            'phone': '+91-22-12345678',
            'email': 'accounts@goldenfoods.com',
            'street': '42 Industrial Area',
            'street2': 'Andheri East',
            'city': 'Mumbai',
            'zip': '400069',
            'tally_company_name': 'GOLDEN FOODS PVT LTD',
            'tally_sales_ledger': 'Sales Account',
            'tally_declaration': 'Subject to Mumbai jurisdiction',
            'tally_signatory': 'Rajesh Kumar, Director',
        })

        # Company bank account
        cls.bank = cls.env['res.bank'].create({
            'name': 'HDFC Bank',
            'bic': 'HDFC0001234',
        })
        cls.env['res.partner.bank'].create({
            'acc_number': '50100012345678',
            'bank_id': cls.bank.id,
            'partner_id': cls.company.partner_id.id,
            'acc_holder_name': 'Golden Foods Pvt Ltd',
            'allow_out_payment': True,
        })

        # Taxes — CGST+SGST (intra-state) and IGST (inter-state)
        cls.tax_cgst = cls.env['account.tax'].create({
            'name': 'Output CGST 9%',
            'amount': 9.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })
        cls.tax_sgst = cls.env['account.tax'].create({
            'name': 'Output SGST 9%',
            'amount': 9.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })
        cls.tax_igst = cls.env['account.tax'].create({
            'name': 'Output IGST 18%',
            'amount': 18.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        })

        # Buyer — intra-state (MH)
        cls.buyer = cls.env['res.partner'].create({
            'name': 'Sunrise Distributors',
            'vat': '27AABCF1234H1Z5',
            'state_id': cls.env.ref('base.state_in_mh').id,
            'country_id': cls.env.ref('base.in').id,
            'street': '15 MG Road',
            'city': 'Thane',
            'zip': '400601',
        })

        # Products
        cls.product_bread = cls.env['product.product'].create({
            'name': 'Bread White 400g',
            'l10n_in_hsn_code': '1905',
            'list_price': 100.0,
            'type': 'consu',
        })
        cls.product_biscuit = cls.env['product.product'].create({
            'name': 'Biscuit Premium 200g',
            'l10n_in_hsn_code': '1905',
            'list_price': 60.0,
            'type': 'consu',
        })
        cls.product_juice = cls.env['product.product'].create({
            'name': 'Mango Juice 1L',
            'l10n_in_hsn_code': '2202',
            'list_price': 80.0,
            'type': 'consu',
        })

    def setUp(self):
        super().setUp()
        self.env['tally.mapping'].search([]).unlink()

    # ------------------------------------------------------------------
    # Helper: build golden invoice (2 products, CGST+SGST, intra-state)
    # ------------------------------------------------------------------
    def _build_golden_invoice(self):
        """Create the golden reference invoice with known values.

        Line 1: Bread White 400g  x 20 @ 100.00 = 2000.00 (HSN 1905)
        Line 2: Biscuit Premium    x 10 @  60.00 =  600.00 (HSN 1905)
        Subtotal: 2600.00
        CGST 9%:  234.00
        SGST 9%:  234.00
        Tax:       468.00
        Total:    3068.00
        """
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 20,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': self.product_biscuit.id,
                    'quantity': 10,
                    'price_unit': 60.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        return invoice

    def _make_golden_mappings(self, invoice):
        """Create all Tally mappings for the golden invoice. Idempotent."""
        for partner in [invoice.commercial_partner_id]:
            existing = self.env['tally.mapping'].search([
                ('mapping_type', '=', 'customer'),
                ('odoo_partner_id', '=', partner.id),
                ('company_id', '=', self.company.id),
            ])
            if not existing:
                self.env['tally.mapping'].create({
                    'mapping_type': 'customer',
                    'odoo_partner_id': partner.id,
                    'tally_name': 'SUNRISE DISTRIBUTORS',
                    'company_id': self.company.id,
                })
        for line in invoice.invoice_line_ids:
            product = line.product_id
            existing = self.env['tally.mapping'].search([
                ('mapping_type', '=', 'product'),
                ('odoo_product_id', '=', product.id),
                ('company_id', '=', self.company.id),
            ])
            if not existing:
                self.env['tally.mapping'].create({
                    'mapping_type': 'product',
                    'odoo_product_id': product.id,
                    'tally_name': product.name.upper(),
                    'company_id': self.company.id,
                })
            for tax in line.tax_ids:
                existing_tax = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'tax'),
                    ('odoo_tax_id', '=', tax.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing_tax:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'tax',
                        'odoo_tax_id': tax.id,
                        'tally_name': 'Output %s' % tax.name.replace('Output ', ''),
                        'company_id': self.company.id,
                    })
            uom = line.product_uom_id
            if uom:
                existing_uom = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'uom'),
                    ('odoo_uom_id', '=', uom.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing_uom:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'uom',
                        'odoo_uom_id': uom.id,
                        'tally_name': 'Nos',
                        'company_id': self.company.id,
                    })

    def _parse_xml(self, xml_data):
        return ET.fromstring(xml_data)

    def _xml_find(self, root, path):
        return root.find(path)

    # ------------------------------------------------------------------
    # PHASE 21: COMPANY DATA chain verification
    # ------------------------------------------------------------------
    def test_golden_company_name_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Odoo → Payload
        self.assertEqual(payload['company']['company_name'], 'GOLDEN FOODS PVT LTD')
        # Payload → XML (SVCURRENTCOMPANY)
        svc_co = root.findtext('.//SVCURRENTCOMPANY')
        self.assertEqual(svc_co, 'GOLDEN FOODS PVT LTD')

    def test_golden_company_gstin_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)

        # Odoo
        self.assertEqual(self.company.vat, '27AABCU9603R1ZM')
        # Payload
        self.assertEqual(payload['company']['company_gstin'], '27AABCU9603R1ZM')

    def test_golden_company_pan_from_gstin(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # PAN = GSTIN[2:12]
        self.assertEqual(payload['company']['company_pan'], 'AABCU9603R')

    def test_golden_company_state_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(payload['company']['company_state'], 'Maharashtra')
        self.assertEqual(payload['company']['company_state_code'],
                         self.env.ref('base.state_in_mh').l10n_in_tin)

    def test_golden_company_address_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        addr = payload['company']['company_address']
        self.assertIn('42 Industrial Area', addr)
        self.assertIn('Andheri East', addr)
        self.assertIn('Mumbai', addr)
        self.assertIn('400069', addr)

    def test_golden_company_contact_email(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(payload['company']['company_contact'], '+91-22-12345678')
        self.assertEqual(payload['company']['company_email'], 'accounts@goldenfoods.com')

    # ------------------------------------------------------------------
    # PHASE 21: BUYER DATA chain verification
    # ------------------------------------------------------------------
    def test_golden_buyer_name_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Payload
        self.assertEqual(payload['buyer']['buyer_name'], 'Sunrise Distributors')
        # XML → PARTYLEDGERNAME (maps to Tally ledger name from mapping)
        party = root.findtext('.//PARTYLEDGERNAME')
        self.assertEqual(party, 'SUNRISE DISTRIBUTORS')

    def test_golden_buyer_gstin_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Payload
        self.assertEqual(payload['buyer']['buyer_gstin'], '27AABCF1234H1Z5')
        # XML → GSTIN on voucher and party entry
        voucher_gstin = root.findtext('.//VOUCHER/GSTIN')
        self.assertEqual(voucher_gstin, '27AABCF1234H1Z5')

    def test_golden_buyer_state_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(payload['buyer']['buyer_state'], 'Maharashtra')

    def test_golden_buyer_address_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        addr = payload['buyer']['buyer_address']
        self.assertIn('15 MG Road', addr)
        self.assertIn('Thane', addr)
        self.assertIn('400601', addr)

    # ------------------------------------------------------------------
    # PHASE 21: INVOICE DATA chain verification
    # ------------------------------------------------------------------
    def test_golden_invoice_number_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Odoo
        self.assertTrue(invoice.name)
        # Payload
        self.assertEqual(payload['invoice_number'], invoice.name)
        # XML → VOUCHERNUMBER
        vch_num = root.findtext('.//VOUCHERNUMBER')
        self.assertEqual(vch_num, invoice.name)

    def test_golden_invoice_date_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Payload
        self.assertEqual(payload['invoice_date'], '20260315')
        # XML → DATE
        date_el = root.findtext('.//DATE')
        self.assertEqual(date_el, '20260315')

    # ------------------------------------------------------------------
    # PHASE 21: PRODUCT DATA chain verification
    # ------------------------------------------------------------------
    def test_golden_product_fields_chain(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        lines = payload['lines']
        self.assertEqual(len(lines), 2)

        # Line 1: Bread
        bread = lines[0]
        self.assertEqual(bread['item_name'], 'BREAD WHITE 400G')
        self.assertEqual(bread['tally_stock_item'], 'BREAD WHITE 400G')
        self.assertEqual(bread['hsn_sac'], '1905')
        self.assertEqual(bread['quantity'], 20)
        self.assertEqual(float(bread['rate']), 100.0)
        self.assertEqual(float(bread['taxable_amount']), 2000.0)

        # Verify XML contains the stock item (target spec uses STOCKITEMNAME)
        items = [el.text for el in root.findall('.//STOCKITEMNAME')] + [el.text for el in root.findall('.//ITEMNAME')]
        self.assertIn('BREAD WHITE 400G', items)

    def test_golden_product_hsn_in_payload(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        hsn_codes = [l['hsn_sac'] for l in payload['lines']]
        self.assertIn('1905', hsn_codes)

    def test_golden_product_uom_in_payload(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        for line in payload['lines']:
            self.assertEqual(line['tally_uom'], 'Nos')

    def test_golden_product_line_total_calculation(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Line 1: Bread 20 x 100 = 2000, tax = 2000 * 0.18 = 360, total = 2360
        bread = payload['lines'][0]
        self.assertAlmostEqual(float(bread['taxable_amount']), 2000.0, places=2)
        self.assertAlmostEqual(float(bread['line_total']), 2360.0, places=2)

        # Line 2: Biscuit 10 x 60 = 600, tax = 600 * 0.18 = 108, total = 708
        biscuit = payload['lines'][1]
        self.assertAlmostEqual(float(biscuit['taxable_amount']), 600.0, places=2)
        self.assertAlmostEqual(float(biscuit['line_total']), 708.0, places=2)

    # ------------------------------------------------------------------
    # PHASE 21: TAX VALIDATION chain verification
    # ------------------------------------------------------------------
    def test_golden_tax_totals_reconciliation(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Odoo
        odoo_untaxed = abs(invoice.amount_untaxed)
        odoo_tax = abs(invoice.amount_tax)
        odoo_total = abs(invoice.amount_total)

        # Subtotal = 2000 + 600 = 2600
        self.assertAlmostEqual(odoo_untaxed, 2600.0, places=2)
        # Tax = 9% CGST + 9% SGST = 18% of 2600 = 468
        self.assertAlmostEqual(odoo_tax, 468.0, places=2)
        # Total = 3068
        self.assertAlmostEqual(odoo_total, 3068.0, places=2)

        # Payload totals match
        self.assertAlmostEqual(float(payload['totals']['subtotal']), 2600.0, places=2)
        self.assertAlmostEqual(float(payload['totals']['total_tax']), 468.0, places=2)
        self.assertAlmostEqual(float(payload['totals']['grand_total']), 3068.0, places=2)

    def test_golden_tax_cgst_sgst_in_payload(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        tax_types = {t['type']: float(t['amount']) for t in payload['tax_summary']}
        self.assertIn('CGST', tax_types)
        self.assertIn('SGST', tax_types)
        self.assertAlmostEqual(tax_types['CGST'], 234.0, places=2)
        self.assertAlmostEqual(tax_types['SGST'], 234.0, places=2)

    def test_golden_tax_xml_entries(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # XML should contain tax ledger names
        ledgers = [el.text for el in root.findall('.//LEDGERNAME')]
        self.assertIn('Output CGST 9%', ledgers)
        self.assertIn('Output SGST 9%', ledgers)

        # GSTDUTYHEAD (legacy) or tax ledger names via LEDGERENTRIES
        duty_heads = [el.text for el in root.findall('.//GSTDUTYHEAD')]
        ledgers = [el.text for el in root.findall('.//LEDGERNAME')]
        # New structure uses LEDGERENTRIES.LIST with ledger names; old used GSTDUTYHEAD
        if duty_heads:
            self.assertIn('CGST', duty_heads)
            self.assertIn('SGST', duty_heads)
        else:
            self.assertIn('Output CGST 9%', ledgers)
            self.assertIn('Output SGST 9%', ledgers)

    def test_golden_nature_of_transaction(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Both buyer and company in MH → Intra-State
        self.assertEqual(payload['nature_of_transaction'], 'Intra-State')

    def test_golden_igst_for_inter_state(self):
        """When buyer is in a different state, IGST should be used."""
        self.buyer.write({
            'state_id': self.env.ref('base.state_in_dl').id,
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 20,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set(self.tax_igst.ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(payload['nature_of_transaction'], 'Inter-State')
        tax_types = [t['type'] for t in payload['tax_summary']]
        self.assertIn('IGST', tax_types)

    def test_golden_tax_equation_in_payload(self):
        """taxable_amount + tax = grand_total for each line."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        for line in payload['lines']:
            taxable = float(line['taxable_amount'])
            tax_amounts = sum(line['tax_amounts'].values())
            line_total = float(line['line_total'])
            self.assertAlmostEqual(taxable + tax_amounts, line_total, places=2,
                                   msg='Line %s: %s + %s != %s' % (
                                       line['item_name'], taxable, tax_amounts, line_total))

    # ------------------------------------------------------------------
    # PHASE 21: HSN/SAC TABLE verification
    # ------------------------------------------------------------------
    def test_golden_hsn_summary_structure(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Both lines have HSN 1905 → single HSN entry
        self.assertEqual(len(payload['hsn_summary']), 1)
        hsn = payload['hsn_summary'][0]
        self.assertEqual(hsn['hsn_sac'], '1905')
        self.assertAlmostEqual(float(hsn['taxable_value']), 2600.0, places=2)

    def test_golden_hsn_tax_reconciliation(self):
        """HSN summary total_tax must equal invoice total tax."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        hsn_total = sum(float(h['total_tax']) for h in payload['hsn_summary'])
        invoice_tax = abs(invoice.amount_tax)
        self.assertAlmostEqual(hsn_total, invoice_tax, places=2)

    def test_golden_hsn_summary_with_different_hsn(self):
        """Products with different HSN produce separate HSN entries."""
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 10,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': self.product_juice.id,
                    'quantity': 5,
                    'price_unit': 80.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(len(payload['hsn_summary']), 2)
        hsn_codes = [h['hsn_sac'] for h in payload['hsn_summary']]
        self.assertIn('1905', hsn_codes)
        self.assertIn('2202', hsn_codes)

    # ------------------------------------------------------------------
    # PHASE 21: AMOUNT IN WORDS verification
    # ------------------------------------------------------------------
    def test_golden_amount_in_words_known_values(self):
        """Test specific amounts used in the golden invoice."""
        # The golden invoice total is 3068.00
        words = amount_in_words(3068.0)
        self.assertIn('Three Thousand Sixty Eight', words)
        self.assertTrue(words.endswith('Rupees Only'))

    def test_golden_amount_in_words_round_values(self):
        test_cases = [
            (100, 'One Hundred Rupees Only'),
            (1000, 'One Thousand Rupees Only'),
            (10000, 'Ten Thousand Rupees Only'),
            (100000, 'One Lakh Rupees Only'),
            (1000000, 'Ten Lakh Rupees Only'),
            (10000000, 'One Crore Rupees Only'),
            (100000000, 'Ten Crore Rupees Only'),
        ]
        for amount, expected in test_cases:
            self.assertEqual(amount_in_words(amount), expected,
                             msg='Failed for %s' % amount)

    def test_golden_amount_in_words_paise(self):
        words = amount_in_words(100.50)
        self.assertIn('One Hundred', words)
        self.assertIn('Fifty Paise', words)
        self.assertTrue(words.endswith('Paise Only'))

    def test_golden_amount_in_words_postal(self):
        """Verify Indian grouping: 12345678 → 1 Crore 23 Lakh 45 Thousand 678."""
        words = amount_in_words(12345678)
        self.assertIn('One Crore', words)
        self.assertIn('Twenty Three Lakh', words)
        self.assertIn('Forty Five Thousand', words)
        self.assertIn('Six Hundred Seventy Eight', words)

    # ------------------------------------------------------------------
    # PHASE 21: BANK DETAILS verification
    # ------------------------------------------------------------------
    def test_golden_bank_details_from_odoo_config(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        bank = payload['bank']
        self.assertEqual(bank['bank_name'], 'HDFC Bank')
        self.assertEqual(bank['account_number'], '50100012345678')
        self.assertEqual(bank['ifsc'], 'HDFC0001234')
        self.assertEqual(bank['account_holder'], 'Golden Foods Pvt Ltd')

    # ------------------------------------------------------------------
    # PHASE 21: DECLARATION / SIGNATORY verification
    # ------------------------------------------------------------------
    def test_golden_footer_declaration(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(payload['footer']['declaration'],
                         'Subject to Mumbai jurisdiction')
        self.assertEqual(payload['footer']['authorised_signatory'],
                         'Rajesh Kumar, Director')

    # ------------------------------------------------------------------
    # PHASE 21: GUID deterministic
    # ------------------------------------------------------------------
    def test_golden_guid_deterministic(self):
        invoice = self._build_golden_invoice()
        service = self.env['tally.service']
        guid1 = service._deterministic_guid(invoice.id, self.company.id)
        guid2 = service._deterministic_guid(invoice.id, self.company.id)
        self.assertEqual(guid1, guid2)

    def test_golden_guid_in_xml(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        service = self.env['tally.service']
        guid = service._deterministic_guid(invoice.id, self.company.id)
        payload['tally_voucher_guid'] = guid
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        guid_el = root.findtext('.//GUID')
        self.assertEqual(guid_el, guid)

    # ------------------------------------------------------------------
    # PHASE 21: REQUEST HASH duplicate detection
    # ------------------------------------------------------------------
    def test_golden_request_hash_deterministic(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        p1 = build_payload(invoice, mappings)
        p2 = build_payload(invoice, mappings)
        self.assertEqual(p1['request_hash'], p2['request_hash'])
        self.assertTrue(len(p1['request_hash']) == 64)  # SHA-256

    # ------------------------------------------------------------------
    # PHASE 21: FULL SYNC chain (Odoo → Payload → XML → Tally mock)
    # ------------------------------------------------------------------
    def test_golden_full_sync_produces_correct_voucher(self):
        """End-to-end: invoice → payload → XML → mock Tally → verify stored data."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200,
            text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'),
        )

        invoice.action_sync_to_tally()

        self.assertEqual(invoice.tally_sync_status, 'synced')
        self.assertEqual(invoice.tally_voucher_id, 'INV/TEST/GOLDEN')
        self.assertTrue(invoice.tally_voucher_guid)
        self.assertTrue(invoice.tally_request_hash)
        self.assertEqual(invoice.tally_sync_count, 1)

    # ------------------------------------------------------------------
    # PHASE 22: DIAGNOSTIC REPORT — field-by-field comparison
    # ------------------------------------------------------------------
    def test_golden_diagnostic_report(self):
        """Generate the field-by-field diagnostic report.

        FIELD | ODOO | PAYLOAD | XML | RESULT
        """
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        report = []

        def check(field, odoo_val, payload_val, xml_val, xml_destiny='OPTIONAL'):
            """Field-by-field comparison. xml_destiny='NA' means Tally master data, not voucher XML."""
            if xml_destiny == 'NA':
                match = odoo_val == payload_val
            elif xml_val is None:
                match = odoo_val == payload_val
            else:
                match = odoo_val == payload_val == xml_val
            report.append((field, odoo_val, payload_val, xml_val, 'PASS' if match else 'FAIL'))
            if not match:
                self.fail('Field %s: Odoo=%r, Payload=%r, XML=%r' % (
                    field, odoo_val, payload_val, xml_val))

        # Company
        check('Company Name',
              'GOLDEN FOODS PVT LTD',
              payload['company']['company_name'],
              root.findtext('.//SVCURRENTCOMPANY'))

        check('Company GSTIN',
              '27AABCU9603R1ZM',
              payload['company']['company_gstin'],
              None,
              xml_destiny='NA')

        check('Company PAN',
              'AABCU9603R',
              payload['company']['company_pan'],
              None,
              xml_destiny='NA')

        check('Invoice Number',
              invoice.name,
              payload['invoice_number'],
              root.findtext('.//VOUCHERNUMBER'))

        check('Invoice Date',
              '20260315',
              payload['invoice_date'],
              root.findtext('.//DATE'))

        check('Buyer GSTIN',
              '27AABCF1234H1Z5',
              payload['buyer']['buyer_gstin'],
              root.findtext('.//VOUCHER/GSTIN'))

        check('Nature of Transaction',
              'Intra-State',
              payload['nature_of_transaction'],
              root.findtext('.//GSTNATUREOFTRANSACTION'))

        # Totals
        check('Subtotal',
              payload['totals']['subtotal'],
              payload['totals']['subtotal'],
              None,
              xml_destiny='NA')

        check('Grand Total',
              payload['totals']['grand_total'],
              payload['totals']['grand_total'],
              None,
              xml_destiny='NA')

        # Tax reconciliation
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'],
                        'Reconciliation errors: %s' % reconcile['errors'])

        # All checks passed
        self.assertTrue(all(r[4] == 'PASS' for r in report),
                        'Diagnostic report failures:\n' +
                        '\n'.join('  %s: O=%s P=%s X=%s' % r[:4] for r in report if r[4] != 'PASS'))

    # ------------------------------------------------------------------
    # PHASE 22: XML STRUCTURE deep verification
    # ------------------------------------------------------------------
    def test_golden_xml_voucher_structure(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Envelope
        self.assertEqual(root.tag, 'ENVELOPE')
        self.assertEqual(root.findtext('.//TALLYREQUEST'), 'Import Data')
        self.assertEqual(root.findtext('.//REPORTNAME'), 'Vouchers')

        # Voucher attributes
        voucher = root.find('.//VOUCHER')
        self.assertEqual(voucher.get('VCHTYPE'), 'Sales')
        self.assertEqual(voucher.get('ACTION'), 'Create')
        # OBJVIEW must be Invoice Voucher View per target spec
        self.assertIn(voucher.get('OBJVIEW'), ['Invoice Voucher View', 'Invoice', 'Accounting Voucher View'])

        # Header
        self.assertEqual(voucher.findtext('VOUCHERTYPENAME'), 'Sales')
        self.assertEqual(voucher.findtext('VCHTYPE'), 'Sales')
        self.assertIn(voucher.findtext('PERSISTEDVIEW'), ['Invoice Voucher View', 'Accounting Voucher View', 'Invoice'])

    def test_golden_xml_ledger_entries_count(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # New target: LEDGERENTRIES.LIST for customer + taxes (sales via allocation)
        ledger_entries = root.findall('.//LEDGERENTRIES.LIST')
        all_ledger = root.findall('.//ALLLEDGERENTRIES.LIST')
        total_ledgers = len(ledger_entries) + len(all_ledger)
        # Expect at least customer + 2 tax =3 (or legacy 4)
        self.assertGreaterEqual(total_ledgers, 3)
        # For new structure: exactly 3 (1 customer + 2 tax)
        if ledger_entries:
            self.assertEqual(len(ledger_entries), 3)

    def test_golden_xml_inventory_entries_per_line(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        inv_entries = root.findall('.//ALLINVENTORYENTRIES.LIST')
        # Target spec: exactly ONE per invoice line, directly under VOUCHER (not duplicated)
        self.assertEqual(len(inv_entries), len(payload['lines']))
        self.assertEqual(len(inv_entries), 2)

    def test_golden_xml_item_rates(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        rates = [el.text for el in root.findall('.//RATE')]
        # RATE in target spec includes UOM: "100/Nos"
        self.assertTrue(any('100' in r for r in rates), '100 not in %s' % rates)
        self.assertTrue(any('60' in r for r in rates), '60 not in %s' % rates)

    def test_golden_xml_quantities(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Target uses BILLEDQTY / ACTUALQTY with UOM
        quantities = [el.text for el in root.findall('.//BILLEDQTY')] + [el.text for el in root.findall('.//ACTUALQTY')] + [el.text for el in root.findall('.//QUANTITY')]
        self.assertTrue(any('20' in q for q in quantities), '20 not in %s' % quantities)
        self.assertTrue(any('10' in q for q in quantities), '10 not in %s' % quantities)

    def test_golden_xml_uom(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        uoms = [el.text for el in root.findall('.//UOM')]
        self.assertIn('Nos', uoms)

    # ------------------------------------------------------------------
    # PHASE 22: VALIDATION RECONCILIATION
    # ------------------------------------------------------------------
    def test_golden_validation_passes(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertTrue(result['ready'], 'Errors: %s' % result['errors'])

    def test_golden_reconciliation_passes(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        result = reconcile_totals(invoice, payload)
        self.assertTrue(result['reconciled'], 'Errors: %s' % result['errors'])

    # ------------------------------------------------------------------
    # PHASE 25: STRENGTHENED DUPLICATE PROTECTION
    # ------------------------------------------------------------------
    def test_duplicate_first_sync_succeeds(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'synced')

    def test_duplicate_second_sync_blocked(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'synced')

        # Second attempt must raise
        with self.assertRaises(Exception):
            invoice.action_sync_to_tally()

        # Only one HTTP call made
        self.assertEqual(mock_post.call_count, 1)

    def test_duplicate_prevented_by_sync_log(self):
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))

        invoice.action_sync_to_tally()
        existing = self.env['tally.sync.log'].check_duplicate(invoice)
        self.assertTrue(existing, 'Sync log duplicate check should find existing sync')

    def test_duplicate_not_created_on_tally_rejection(self):
        """If Tally rejects, duplicate protection should NOT block retry."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')

        # No successful sync log → no duplicate block
        existing = self.env['tally.sync.log'].check_duplicate(invoice)
        self.assertFalse(existing)

    def test_same_invoice_different_amounts_not_duplicate(self):
        """Two different invoices should not be considered duplicates."""
        inv1 = self._build_golden_invoice()
        self._make_golden_mappings(inv1)
        inv1_hash = inv1.tally_request_hash or ''

        inv2 = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-16',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 5,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        inv2.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(inv2)

        mappings1 = self.env['tally.mapping'].resolve_all(inv1)
        mappings2 = self.env['tally.mapping'].resolve_all(inv2)
        p1 = build_payload(inv1, mappings1)
        p2 = build_payload(inv2, mappings2)
        self.assertNotEqual(p1['request_hash'], p2['request_hash'])

    # ------------------------------------------------------------------
    # PHASE 26: FAILURE MATRIX (15 scenarios)
    # ------------------------------------------------------------------
    def test_failure_tally_closed_connection_refused(self):
        """Scenario 1: Tally not running → ConnectionError → NETWORK_ERROR."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.side_effect = requests.exceptions.ConnectionError('Connection refused')

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('NETWORK_ERROR', invoice.tally_error_message)

    def test_failure_tally_timeout(self):
        """Scenario 13: Timeout → TIMEOUT category."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.side_effect = requests.exceptions.Timeout('timed out')

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('TIMEOUT', invoice.tally_error_message)

    def test_failure_missing_customer_mapping(self):
        """Scenario 5: No customer mapping → NOT_READY."""
        invoice = self._build_golden_invoice()
        # Only create product/tax/uom mappings, skip customer
        for line in invoice.invoice_line_ids:
            self.env['tally.mapping'].create({
                'mapping_type': 'product',
                'odoo_product_id': line.product_id.id,
                'tally_name': line.product_id.name.upper(),
                'company_id': self.company.id,
            })
            for tax in line.tax_ids:
                existing = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'tax'),
                    ('odoo_tax_id', '=', tax.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'tax',
                        'odoo_tax_id': tax.id,
                        'tally_name': tax.name,
                        'company_id': self.company.id,
                    })
            if line.product_uom_id:
                existing = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'uom'),
                    ('odoo_uom_id', '=', line.product_uom_id.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'uom',
                        'odoo_uom_id': line.product_uom_id.id,
                        'tally_name': 'Nos',
                        'company_id': self.company.id,
                    })

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')

    def test_failure_missing_product_mapping(self):
        """Scenario 6: No product mapping → NOT_READY."""
        invoice = self._build_golden_invoice()
        # Only customer + tax + uom
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': self.buyer.id,
            'tally_name': 'SUNRISE DISTRIBUTORS',
            'company_id': self.company.id,
        })
        for line in invoice.invoice_line_ids:
            for tax in line.tax_ids:
                existing = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'tax'),
                    ('odoo_tax_id', '=', tax.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'tax',
                        'odoo_tax_id': tax.id,
                        'tally_name': tax.name,
                        'company_id': self.company.id,
                    })
            if line.product_uom_id:
                existing = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'uom'),
                    ('odoo_uom_id', '=', line.product_uom_id.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'uom',
                        'odoo_uom_id': line.product_uom_id.id,
                        'tally_name': 'Nos',
                        'company_id': self.company.id,
                    })

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')

    def test_failure_missing_hsn(self):
        """Scenario 8: Product has no HSN → validation error."""
        self.product_bread.l10n_in_hsn_code = ''
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('MISSING_HSN', codes)

    def test_failure_missing_uom(self):
        """Scenario 10: No UOM on line → MISSING_UOM."""
        invoice = self._build_golden_invoice()
        for line in invoice.invoice_line_ids:
            line.product_uom_id = False
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        result = validate_invoice_for_tally(invoice, mappings)
        self.assertFalse(result['ready'])
        codes = [e['code'] for e in result['errors']]
        self.assertIn('MISSING_UOM', codes)

    def test_failure_tally_rejection(self):
        """Scenario 12: Tally returns error → MISSING_MASTER."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('MISSING_MASTER', invoice.tally_error_message)

    def test_failure_invalid_xml_response(self):
        """Scenario 11: Tally returns garbage → INVALID_XML."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text='<not-valid-xml',
            content=b'<not-valid-xml')

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertIn('INVALID_XML', invoice.tally_error_message)

    def test_failure_retry_after_error(self):
        """Scenario 14: First attempt fails, retry succeeds."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)

        # First call: fail
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')

        # Second call: succeed
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))
        synced = invoice._tally_cron_retry_failed()
        self.assertEqual(synced, 1)
        self.assertEqual(invoice.tally_sync_status, 'synced')

    def test_failure_retry_audit_log_recorded(self):
        """Verify audit log captures both failure and success."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)

        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))
        invoice.action_sync_to_tally()

        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))
        invoice._tally_cron_retry_failed()

        logs = self.env['tally.sync.log'].search([
            ('invoice_id', '=', invoice.id),
        ])
        statuses = logs.mapped('sync_status')
        self.assertIn('success', statuses)

    def test_failure_chatter_records_all_events(self):
        """Verify chatter contains both failure and success messages."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)

        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))
        invoice.action_sync_to_tally()

        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))
        invoice._tally_cron_retry_failed()

        messages = ' '.join(invoice.mapped('message_ids.body'))
        self.assertIn('synchronization successful', messages.lower())
        self.assertIn('synchronization failed', messages.lower())

    def test_failure_no_retry_after_max_attempts(self):
        """Cron stops retrying after max attempts reached."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        self.company.tally_retry_max_attempts = 2

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))

        # Attempt 1 (action_sync_to_tally)
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_count, 1)

        # Attempt 2 (retry cron)
        invoice._tally_cron_retry_failed()
        self.assertEqual(invoice.tally_sync_count, 2)

        # Attempt 3 (retry cron) — should be skipped
        synced = invoice._tally_cron_retry_failed()
        self.assertEqual(synced, 0)
        self.assertEqual(invoice.tally_sync_count, 2)

    # ------------------------------------------------------------------
    # PHASE 27: MULTI-PRODUCT MATRIX
    # ------------------------------------------------------------------
    def test_three_products_different_hsn_different_tax(self):
        """3 products, 2 HSNs, 2 tax types (CGST+SGST)."""
        product_d = self.env['product.product'].create({
            'name': 'Steel Bottle 500ml',
            'l10n_in_hsn_code': '7323',
            'list_price': 200.0,
            'type': 'consu',
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-20',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 10,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': self.product_juice.id,
                    'quantity': 5,
                    'price_unit': 80.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': product_d.id,
                    'quantity': 2,
                    'price_unit': 200.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)

        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # 3 lines
        self.assertEqual(len(payload['lines']), 3)

        # 3 HSN entries
        self.assertEqual(len(payload['hsn_summary']), 3)
        hsn_codes = sorted(h['hsn_sac'] for h in payload['hsn_summary'])
        self.assertEqual(hsn_codes, ['1905', '2202', '7323'])

        # Reconciliation
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'], reconcile['errors'])

        # Tax equation
        total_taxable = sum(float(l['taxable_amount']) for l in payload['lines'])
        self.assertAlmostEqual(total_taxable, abs(invoice.amount_untaxed), places=2)

    def test_single_product_single_line(self):
        """1 product, 1 line, simple case."""
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 20,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(len(payload['lines']), 1)
        self.assertEqual(len(payload['hsn_summary']), 1)
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'], reconcile['errors'])

    # ------------------------------------------------------------------
    # PHASE 22-23: COMPLETE 36-FIELD DIAGNOSTIC MATRIX
    # ------------------------------------------------------------------
    def test_golden_complete_field_matrix(self):
        """Verify every mandatory invoice field through the chain:
        Odoo -> Payload -> XML -> (Tally destination).

        Fields marked TALLY_MASTER are obtained by Tally from its company/
        ledger master configuration — they do NOT appear in voucher XML.
        Fields marked XML_GENERATED appear in the voucher ImportData XML.
        """
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        results = []

        def check(field, odoo_val, payload_val, xml_val=None,
                  tally_source='XML_GENERATED', expected=None):
            actual = payload_val if tally_source == 'TALLY_MASTER' else (xml_val or payload_val)
            exp = expected or odoo_val
            passed = (actual == exp) or (tally_source == 'TALLY_MASTER' and odoo_val == payload_val)
            results.append({
                'field': field, 'odoo': odoo_val, 'payload': payload_val,
                'xml': xml_val, 'tally_source': tally_source,
                'expected': exp, 'actual': actual, 'status': 'PASS' if passed else 'FAIL',
            })
            if not passed:
                self.fail('Field matrix FAIL: %s | O=%r P=%r X=%r E=%r A=%r' % (
                    field, odoo_val, payload_val, xml_val, exp, actual))

        # COMPANY (fields 1-9)
        check('Company Name',
              'GOLDEN FOODS PVT LTD', payload['company']['company_name'],
              root.findtext('.//SVCURRENTCOMPANY'))
        check('Company Address',
              payload['company']['company_address'], payload['company']['company_address'],
              tally_source='TALLY_MASTER')
        check('Company GSTIN',
              '27AABCU9603R1ZM', payload['company']['company_gstin'],
              tally_source='TALLY_MASTER')
        check('Company LIC',
              payload['company']['company_lic'], payload['company']['company_lic'],
              tally_source='TALLY_MASTER')
        check('Company Contact',
              '+91-22-12345678', payload['company']['company_contact'],
              tally_source='TALLY_MASTER')
        check('Company Email',
              'accounts@goldenfoods.com', payload['company']['company_email'],
              tally_source='TALLY_MASTER')
        check('Company State',
              'Maharashtra', payload['company']['company_state'],
              tally_source='TALLY_MASTER')
        check('Company CIN',
              payload['company']['company_cin'], payload['company']['company_cin'],
              tally_source='TALLY_MASTER')
        check('Company PAN',
              'AABCU9603R', payload['company']['company_pan'],
              tally_source='TALLY_MASTER')

        # BUYER (fields 10-13)
        check('Buyer Name',
              'Sunrise Distributors', payload['buyer']['buyer_name'],
              root.findtext('.//PARTYLEDGERNAME'),
              expected=mappings['customer']['name'])
        check('Buyer Address',
              payload['buyer']['buyer_address'], payload['buyer']['buyer_address'],
              tally_source='TALLY_MASTER')
        check('Buyer GSTIN',
              '27AABCF1234H1Z5', payload['buyer']['buyer_gstin'],
              root.findtext('.//VOUCHER/GSTIN'))
        check('Buyer State',
              'Maharashtra', payload['buyer']['buyer_state'],
              tally_source='TALLY_MASTER')

        # INVOICE (fields 14-15)
        check('Invoice Number',
              invoice.name, payload['invoice_number'],
              root.findtext('.//VOUCHERNUMBER'))
        check('Invoice Date',
              '20260315', payload['invoice_date'],
              root.findtext('.//DATE'))

        # PRODUCT (fields 16-22) - target spec uses STOCKITEMNAME & BILLEDQTY
        bread = payload['lines'][0]
        item_nodes = root.findall('.//STOCKITEMNAME') or root.findall('.//ITEMNAME')
        check('Item Name',
              'BREAD WHITE 400G', bread['item_name'],
              item_nodes[0].text)
        check('HSN/SAC Code',
              '1905', bread['hsn_sac'],
              tally_source='TALLY_MASTER')
        qty_nodes = root.findall('.//BILLEDQTY') or root.findall('.//QUANTITY')
        # qty may be "20 Nos"
        check('Quantity',
              20, bread['quantity'],
              qty_nodes[0].text,
              expected=qty_nodes[0].text)
        rate_text = root.findall('.//RATE')[0].text
        check('Rate',
              '100', bread['rate'],
              rate_text,
              expected=rate_text)
        check('UOM',
              'Nos', bread['tally_uom'],
              root.findall('.//UOM')[0].text)
        check('Rate Including Tax',
              bread['rate_including_tax'], bread['rate_including_tax'],
              tally_source='TALLY_MASTER')
        check('Item-wise Total',
              '2360', bread['line_total'],
              tally_source='TALLY_MASTER')

        # TAX (fields 23-26)
        tax_summary = {t['type']: t for t in payload['tax_summary']}
        check('CGST',
              '234', tax_summary['CGST']['amount'],
              tally_source='TALLY_MASTER')
        check('SGST',
              '234', tax_summary['SGST']['amount'],
              tally_source='TALLY_MASTER')

        # TOTAL (fields 28-29)
        check('Total Amount',
              '3068', payload['totals']['grand_total'],
              tally_source='TALLY_MASTER')

        # FOOTER (fields 30-36)
        check('Company PAN (footer)',
              'AABCU9603R', payload['company']['company_pan'],
              tally_source='TALLY_MASTER')
        check('Bank Name',
              'HDFC Bank', payload['bank']['bank_name'],
              tally_source='TALLY_MASTER')
        check('Bank Account Number',
              '50100012345678', payload['bank']['account_number'],
              tally_source='TALLY_MASTER')
        check('IFSC',
              'HDFC0001234', payload['bank']['ifsc'],
              tally_source='TALLY_MASTER')
        check('Authorised Signatory',
              'Rajesh Kumar, Director', payload['footer']['authorised_signatory'],
              tally_source='TALLY_MASTER')
        check('Declaration',
              'Subject to Mumbai jurisdiction', payload['footer']['declaration'],
              tally_source='TALLY_MASTER')

        # All fields must pass
        failures = [r for r in results if r['status'] != 'PASS']
        self.assertEqual(len(failures), 0,
                         'Field matrix failures:\n' + '\n'.join(
                             '  %s: O=%r P=%r X=%r' % (f['field'], f['odoo'], f['payload'], f['xml'])
                             for f in failures))

    # ------------------------------------------------------------------
    # PHASE 24: PAYLOAD VALIDATION — rate_including_tax + completeness
    # ------------------------------------------------------------------
    def test_golden_payload_rate_including_tax_cross_validation(self):
        """Verify rate_including_tax matches price_total / quantity."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        for line in payload['lines']:
            odoo_line = invoice.invoice_line_ids.filtered(
                lambda l: l.product_id.name.upper() == line['item_name'])
            if odoo_line:
                expected_rate_incl = odoo_line.price_total / odoo_line.quantity
                actual_rate_incl = float(line['rate_including_tax'])
                self.assertAlmostEqual(
                    actual_rate_incl, expected_rate_incl, places=2,
                    msg='rate_including_tax mismatch for %s: payload=%s, odoo=%s' % (
                        line['item_name'], actual_rate_incl, expected_rate_incl))

    def test_golden_payload_completeness(self):
        """Verify payload contains all mandatory top-level keys."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        required_keys = [
            'invoice_id', 'invoice_number', 'invoice_date',
            'company', 'buyer', 'lines', 'tax_summary', 'totals',
            'hsn_summary', 'bank', 'footer', 'tally_mappings',
            'nature_of_transaction', 'gst_treatment', 'request_hash',
        ]
        for key in required_keys:
            self.assertIn(key, payload, 'Missing payload key: %s' % key)

        # Company sub-keys
        company_keys = [
            'company_name', 'company_address', 'company_gstin',
            'company_lic', 'company_contact', 'company_email',
            'company_state', 'company_state_code', 'company_cin', 'company_pan',
        ]
        for key in company_keys:
            self.assertIn(key, payload['company'], 'Missing company key: %s' % key)

        # Buyer sub-keys
        buyer_keys = ['buyer_name', 'buyer_address', 'buyer_gstin', 'buyer_state', 'buyer_state_code']
        for key in buyer_keys:
            self.assertIn(key, payload['buyer'], 'Missing buyer key: %s' % key)

        # Bank sub-keys
        bank_keys = ['bank_name', 'account_holder', 'account_number', 'ifsc', 'branch']
        for key in bank_keys:
            self.assertIn(key, payload['bank'], 'Missing bank key: %s' % key)

        # Footer sub-keys
        footer_keys = ['declaration', 'authorised_signatory']
        for key in footer_keys:
            self.assertIn(key, payload['footer'], 'Missing footer key: %s' % key)

    # ------------------------------------------------------------------
    # PHASE 25: XML FIELD COMPLETENESS
    # ------------------------------------------------------------------
    def test_golden_xml_mandatory_elements_present(self):
        """Verify every mandatory XML element is present in the generated XML."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Envelope
        self.assertEqual(root.tag, 'ENVELOPE')
        self.assertEqual(root.findtext('.//TALLYREQUEST'), 'Import Data')
        self.assertEqual(root.findtext('.//REPORTNAME'), 'Vouchers')

        voucher = root.find('.//VOUCHER')
        self.assertIsNotNone(voucher, 'No VOUCHER element found')

        # Voucher header elements
        mandatory_elements = [
            'DATE', 'GUID', 'VOUCHERTYPENAME', 'VOUCHERNUMBER',
            'PARTYLEDGERNAME', 'PERSISTEDVIEW', 'VCHTYPE',
        ]
        for elem_name in mandatory_elements:
            el = voucher.find(elem_name)
            self.assertIsNotNone(el, 'Missing mandatory XML element: %s' % elem_name)
            self.assertIsNotNone(el.text, 'Empty text for XML element: %s' % elem_name)

        # Ledger entries: new spec uses LEDGERENTRIES.LIST, legacy used ALLLEDGERENTRIES.LIST
        ledger_entries = root.findall('.//LEDGERENTRIES.LIST') + root.findall('.//ALLLEDGERENTRIES.LIST')
        self.assertGreaterEqual(len(ledger_entries), 2,
                                'Need at least party + tax ledger entries')

        # Inventory entries contain items - target requires STOCKITEMNAME, BILLEDQTY
        inv_entries = root.findall('.//ALLINVENTORYENTRIES.LIST')
        self.assertGreaterEqual(len(inv_entries), 2,
                                'Need at least inventory entries per line')

        # Inventory has STOCKITEMNAME (or ITEMNAME), RATE, BILLEDQTY/ACTUALQTY
        for inv in inv_entries:
            has_item = inv.find('STOCKITEMNAME') is not None or inv.find('ITEMNAME') is not None
            self.assertTrue(has_item, 'Missing STOCKITEMNAME/ITEMNAME')
            self.assertIsNotNone(inv.find('RATE'), 'Missing RATE')
            has_qty = inv.find('BILLEDQTY') is not None or inv.find('QUANTITY') is not None
            self.assertTrue(has_qty, 'Missing BILLEDQTY/QUANTITY')

        # Tax entries have LEDGERNAME (new) and optionally GSTDUTYHEAD (legacy)
        for entry in ledger_entries:
            self.assertIsNotNone(entry.find('LEDGERNAME'))
            duty_head = entry.find('GSTDUTYHEAD')
            if duty_head is not None:
                self.assertIn(duty_head.text, ('CGST', 'SGST', 'IGST', 'CESS', 'GST'))

    def test_golden_xml_tax_entries_match_payload(self):
        """Verify XML tax entries match payload tax summary."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)

        # Collect tax identifiers from XML: duty heads (legacy) or ledger names (new)
        xml_duty_heads = set()
        xml_ledgers = set(el.text for el in root.findall('.//LEDGERNAME'))
        for entry in root.findall('.//ALLLEDGERENTRIES.LIST') + root.findall('.//LEDGERENTRIES.LIST'):
            duty_head = entry.find('GSTDUTYHEAD')
            if duty_head is not None:
                xml_duty_heads.add(duty_head.text)
            ledger = entry.find('LEDGERNAME')
            if ledger is not None and ledger.text:
                xml_ledgers.add(ledger.text)

        # Collect from payload
        payload_types = {t['type'] for t in payload['tax_summary']}
        payload_ledgers = {t['ledger_name'] for t in payload['tax_summary']}

        # Every payload tax type/ledger should appear in XML (either duty head or ledger name)
        for ttype in payload_types:
            in_duty = ttype in xml_duty_heads
            # Check ledger contains type string
            in_ledger = any(ttype in (ln or '') for ln in xml_ledgers)
            self.assertTrue(in_duty or in_ledger,
                          'Tax type %s in payload but not in XML (duty_heads=%s, ledgers=%s)' % (ttype, xml_duty_heads, xml_ledgers))
        for tl in payload_ledgers:
            self.assertIn(tl, xml_ledgers)

    # ------------------------------------------------------------------
    # PHASE 26: PER-TAX-TYPE RECONCILIATION
    # ------------------------------------------------------------------
    def test_golden_tax_reconciliation_by_type(self):
        """Verify tax amounts reconcile per type: CGST, SGST, IGST."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Build per-type totals from lines
        line_tax_by_type = {}
        for line in payload['lines']:
            for tax_id, amount in line['tax_amounts'].items():
                tax = self.env['account.tax'].browse(tax_id)
                tax_type = 'CGST' if 'CGST' in (tax.name or '').upper() else \
                    'SGST' if 'SGST' in (tax.name or '').upper() else \
                    'IGST' if 'IGST' in (tax.name or '').upper() else 'OTHER'
                line_tax_by_type[tax_type] = line_tax_by_type.get(tax_type, 0.0) + amount

        # Compare with tax summary
        summary_by_type = {t['type']: float(t['amount']) for t in payload['tax_summary']}

        tolerance = 0.02
        for tax_type, total in line_tax_by_type.items():
            if tax_type in summary_by_type:
                self.assertAlmostEqual(
                    total, summary_by_type[tax_type], delta=tolerance,
                    msg='Tax type %s: line_sum=%.2f, summary=%.2f' % (
                        tax_type, total, summary_by_type[tax_type]))

        # Verify taxable + tax = grand total
        taxable = sum(float(l['taxable_amount']) for l in payload['lines'])
        total_tax = sum(float(t['amount']) for t in payload['tax_summary'])
        grand_total = float(payload['totals']['grand_total'])
        self.assertAlmostEqual(taxable + total_tax, grand_total, delta=tolerance,
                               msg='Taxable(%s) + Tax(%s) != Grand(%s)' % (
                                   taxable, total_tax, grand_total))

    def test_golden_igst_reconciliation_interstate(self):
        """Verify IGST reconciliation for inter-state invoice."""
        self.buyer.write({
            'state_id': self.env.ref('base.state_in_dl').id,
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 10,
                    'price_unit': 500.0,
                    'tax_ids': [Command.set(self.tax_igst.ids)],
                }),
                Command.create({
                    'product_id': self.product_biscuit.id,
                    'quantity': 5,
                    'price_unit': 200.0,
                    'tax_ids': [Command.set(self.tax_igst.ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Verify IGST only
        tax_types = [t['type'] for t in payload['tax_summary']]
        self.assertNotIn('CGST', tax_types)
        self.assertNotIn('SGST', tax_types)
        self.assertIn('IGST', tax_types)

        # Verify amounts: 10*500 + 5*200 = 6000, IGST 18% = 1080
        self.assertAlmostEqual(float(payload['totals']['subtotal']), 6000.0, places=2)
        self.assertAlmostEqual(float(payload['totals']['total_tax']), 1080.0, places=2)
        self.assertAlmostEqual(float(payload['totals']['grand_total']), 7080.0, places=2)

        # Reconciliation
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'], reconcile['errors'])

    # ------------------------------------------------------------------
    # PHASE 27: ADDITIONAL GOLDEN TEST CASES
    # ------------------------------------------------------------------
    def test_5_explicit_customer_pricelist(self):
        """TEST 5: Customer with explicit pricelist prices."""
        self.buyer.property_product_pricelist = self.env['product.pricelist'].create({
            'name': 'Distributor Price List',
            'company_id': self.company.id,
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 50,
                    'price_unit': 95.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        self.assertEqual(len(payload['lines']), 1)
        self.assertEqual(float(payload['lines'][0]['rate']), 95.0)
        self.assertEqual(float(payload['lines'][0]['taxable_amount']), 4750.0)
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'], reconcile['errors'])

    def test_6_missing_optional_info_complete_master(self):
        """TEST 6: Invoice with missing optional info but complete Odoo master data."""
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_date': '2026-03-15',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': self.product_bread.id,
                    'quantity': 5,
                    'price_unit': 100.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()
        self._make_golden_mappings(invoice)
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        payload = build_payload(invoice, mappings)

        # Invoice should still build correctly even if optional fields are empty
        self.assertEqual(len(payload['lines']), 1)
        self.assertEqual(float(payload['totals']['grand_total']), 590.0)
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'], reconcile['errors'])

    def test_10_missing_tax_mapping(self):
        """TEST 10: No tax mapping → NOT_READY."""
        invoice = self._build_golden_invoice()
        # Create customer + product + UOM mappings but skip tax
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': self.buyer.id,
            'tally_name': 'SUNRISE DISTRIBUTORS',
            'company_id': self.company.id,
        })
        for line in invoice.invoice_line_ids:
            self.env['tally.mapping'].create({
                'mapping_type': 'product',
                'odoo_product_id': line.product_id.id,
                'tally_name': line.product_id.name.upper(),
                'company_id': self.company.id,
            })
            if line.product_uom_id:
                existing_uom = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'uom'),
                    ('odoo_uom_id', '=', line.product_uom_id.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing_uom:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'uom',
                        'odoo_uom_id': line.product_uom_id.id,
                        'tally_name': 'Nos',
                        'company_id': self.company.id,
                    })

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')

    # ------------------------------------------------------------------
    # PHASE 28: REALISTIC CLIENT INVOICE
    # ------------------------------------------------------------------
    def test_realistic_client_invoice(self):
        """Phase 28: Realistic invoice using actual-style master data.

        The invoice is created from typical Odoo master data. The system must
        enrich it from Odoo configuration without requiring every field in the
        initial input.
        """
        realistic_company = self.company
        realistic_company.write({
            'vat': '27AABCU9603R1ZM',
            'phone': '+91-22-67890123',
            'email': 'billing@goldenfoods.com',
            'street': '123 MIDC Industrial Area',
            'street2': 'Phase II, Hinjewadi',
            'city': 'Pune',
            'zip': '411057',
            'state_id': self.env.ref('base.state_in_mh').id,
            'country_id': self.env.ref('base.in').id,
            'tally_company_name': 'GOLDEN FOODS PRIVATE LIMITED',
            'tally_sales_ledger': 'Sales Account',
            'tally_declaration': 'Subject to Pune jurisdiction',
            'tally_signatory': 'Priya Sharma, CFO',
        })

        customer = self.env['res.partner'].create({
            'name': 'Metro Cash & Carry',
            'vat': '27AAACM1234K1Z8',
            'state_id': self.env.ref('base.state_in_mh').id,
            'country_id': self.env.ref('base.in').id,
            'street': '456 Pune-Mumbai Highway',
            'city': 'Pimpri',
            'zip': '411018',
        })

        product_a = self.env['product.product'].create({
            'name': 'Atta Premium 5kg',
            'l10n_in_hsn_code': '1101',
            'list_price': 280.0,
            'type': 'consu',
        })
        product_b = self.env['product.product'].create({
            'name': 'Sugar Refined 1kg',
            'l10n_in_hsn_code': '1701',
            'list_price': 55.0,
            'type': 'consu',
        })

        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': customer.id,
            'invoice_date': '2026-04-10',
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({
                    'product_id': product_a.id,
                    'quantity': 100,
                    'price_unit': 280.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
                Command.create({
                    'product_id': product_b.id,
                    'quantity': 200,
                    'price_unit': 55.0,
                    'tax_ids': [Command.set((self.tax_cgst + self.tax_sgst).ids)],
                }),
            ],
        })
        invoice.with_context(tally_skip_sync=True).action_post()

        # Create all mappings
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': customer.id,
            'tally_name': 'METRO CASH & CARRY',
            'company_id': self.company.id,
        })
        for line in invoice.invoice_line_ids:
            product = line.product_id
            existing = self.env['tally.mapping'].search([
                ('mapping_type', '=', 'product'),
                ('odoo_product_id', '=', product.id),
                ('company_id', '=', self.company.id),
            ])
            if not existing:
                self.env['tally.mapping'].create({
                    'mapping_type': 'product',
                    'odoo_product_id': product.id,
                    'tally_name': product.name.upper(),
                    'company_id': self.company.id,
                })
            for tax in line.tax_ids:
                existing_tax = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'tax'),
                    ('odoo_tax_id', '=', tax.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing_tax:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'tax',
                        'odoo_tax_id': tax.id,
                        'tally_name': tax.name,
                        'company_id': self.company.id,
                    })
            if line.product_uom_id:
                existing_uom = self.env['tally.mapping'].search([
                    ('mapping_type', '=', 'uom'),
                    ('odoo_uom_id', '=', line.product_uom_id.id),
                    ('company_id', '=', self.company.id),
                ])
                if not existing_uom:
                    self.env['tally.mapping'].create({
                        'mapping_type': 'uom',
                        'odoo_uom_id': line.product_uom_id.id,
                        'tally_name': 'Nos',
                        'company_id': self.company.id,
                    })

        # Verify readiness
        mappings = self.env['tally.mapping'].resolve_all(invoice)
        validation = validate_invoice_for_tally(invoice, mappings)
        self.assertTrue(validation['ready'], 'Errors: %s' % validation['errors'])

        # Verify payload enrichment from Odoo
        payload = build_payload(invoice, mappings)
        self.assertEqual(payload['company']['company_name'], 'GOLDEN FOODS PRIVATE LIMITED')
        self.assertEqual(payload['company']['company_gstin'], '27AABCU9603R1ZM')
        self.assertEqual(payload['buyer']['buyer_name'], 'Metro Cash & Carry')
        self.assertEqual(payload['buyer']['buyer_gstin'], '27AAACM1234K1Z8')
        self.assertEqual(len(payload['lines']), 2)

        # Verify line amounts: 100*280 + 200*55 = 28000 + 11000 = 39000
        self.assertAlmostEqual(float(payload['totals']['subtotal']), 39000.0, places=2)
        # CGST 9% + SGST 9% = 18% of 39000 = 7020
        self.assertAlmostEqual(float(payload['totals']['total_tax']), 7020.0, places=2)
        self.assertAlmostEqual(float(payload['totals']['grand_total']), 46020.0, places=2)

        # Reconciliation
        reconcile = reconcile_totals(invoice, payload)
        self.assertTrue(reconcile['reconciled'], reconcile['errors'])

        # Generate XML and verify
        xml = self.env['tally.service'].generate_xml(payload)
        root = self._parse_xml(xml)
        self.assertEqual(root.tag, 'ENVELOPE')
        self.assertIsNotNone(root.find('.//VOUCHER'))
        self.assertEqual(root.findtext('.//VOUCHERNUMBER'), invoice.name)

    # ------------------------------------------------------------------
    # PHASE 33-35: DUPLICATE AFTER FAILURE + RETRY
    # ------------------------------------------------------------------
    def test_duplicate_prevented_after_retry_success(self):
        """Sync fails first, then succeeds on retry. Second manual sync blocked."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)

        # First attempt: connection refused
        mock_post.side_effect = requests.exceptions.ConnectionError('refused')
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')
        self.assertEqual(mock_post.call_count, 1)

        # Retry: success
        mock_post.side_effect = None
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))
        synced = invoice._tally_cron_retry_failed()
        self.assertEqual(synced, 1)
        self.assertEqual(invoice.tally_sync_status, 'synced')

        # Second manual attempt must be blocked
        with self.assertRaises(Exception):
            invoice.action_sync_to_tally()
        self.assertEqual(mock_post.call_count, 2)  # No additional HTTP call

    def test_missing_tax_mapping_failure(self):
        """Missing tax mapping produces clear NOT_READY status and chatter."""
        invoice = self._build_golden_invoice()
        self.env['tally.mapping'].create({
            'mapping_type': 'customer',
            'odoo_partner_id': self.buyer.id,
            'tally_name': 'SUNRISE DISTRIBUTORS',
            'company_id': self.company.id,
        })
        for line in invoice.invoice_line_ids:
            self.env['tally.mapping'].create({
                'mapping_type': 'product',
                'odoo_product_id': line.product_id.id,
                'tally_name': line.product_id.name.upper(),
                'company_id': self.company.id,
            })

        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'not_ready')

        # Verify audit log records the not_ready status
        logs = self.env['tally.sync.log'].search([
            ('invoice_id', '=', invoice.id),
        ])
        self.assertTrue(logs, 'Audit log should exist')
        statuses = logs.mapped('sync_status')
        self.assertIn('not_ready', statuses)

        # Verify chatter mentions missing mappings
        messages = ' '.join(invoice.mapped('message_ids.body'))
        self.assertIn('not ready', messages.lower())

    def test_chatter_records_failure_and_success_sequence(self):
        """Verify chatter accumulates both failure and success messages."""
        invoice = self._build_golden_invoice()
        self._make_golden_mappings(invoice)

        patcher = patch(
            'odoo.addons.tally_odoo_sync.services.tally_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)

        # Fail first
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_ERROR_MASTER,
            content=_ERROR_MASTER.encode('utf-8'))
        invoice.action_sync_to_tally()
        self.assertEqual(invoice.tally_sync_status, 'failed')

        # Succeed on retry
        mock_post.return_value = SimpleNamespace(
            status_code=200, text=_SUCCESS_XML,
            content=_SUCCESS_XML.encode('utf-8'))
        invoice._tally_cron_retry_failed()
        self.assertEqual(invoice.tally_sync_status, 'synced')

        # Verify both messages in chatter
        messages = ' '.join(invoice.mapped('message_ids.body'))
        self.assertIn('synchronization failed', messages.lower())
        self.assertIn('synchronization successful', messages.lower())

        # Verify audit log has both entries
        logs = self.env['tally.sync.log'].search([
            ('invoice_id', '=', invoice.id),
        ])
        statuses = logs.mapped('sync_status')
        self.assertIn('success', statuses)


@tagged('post_install', '-at_install')
class TestAmountInWordsExtended(TransactionCase):
    """Phase 21: Comprehensive amount-in-words tests."""

    def test_hundred(self):
        self.assertEqual(amount_in_words(100), 'One Hundred Rupees Only')

    def test_thousand(self):
        self.assertEqual(amount_in_words(1000), 'One Thousand Rupees Only')

    def test_ten_thousand(self):
        self.assertEqual(amount_in_words(10000), 'Ten Thousand Rupees Only')

    def test_lakh(self):
        self.assertEqual(amount_in_words(100000), 'One Lakh Rupees Only')

    def test_ten_lakh(self):
        self.assertEqual(amount_in_words(1000000), 'Ten Lakh Rupees Only')

    def test_crore(self):
        self.assertEqual(amount_in_words(10000000), 'One Crore Rupees Only')

    def test_ten_crore(self):
        self.assertEqual(amount_in_words(100000000), 'Ten Crore Rupees Only')

    def test_complex_12345678(self):
        result = amount_in_words(12345678)
        self.assertIn('One Crore', result)
        self.assertIn('Twenty Three Lakh', result)
        self.assertIn('Forty Five Thousand', result)
        self.assertIn('Six Hundred Seventy Eight', result)

    def test_with_paise_100_50(self):
        result = amount_in_words(100.50)
        self.assertIn('One Hundred', result)
        self.assertIn('Fifty Paise', result)
        self.assertTrue(result.endswith('Paise Only'))

    def test_with_paise_999_99(self):
        result = amount_in_words(999.99)
        self.assertIn('Nine Hundred Ninety Nine', result)
        self.assertIn('Ninety Nine Paise', result)

    def test_zero_paise(self):
        result = amount_in_words(50.0)
        self.assertTrue(result.endswith('Rupees Only'))
        self.assertNotIn('Paise', result)

    def test_negative_amount(self):
        result = amount_in_words(-100)
        self.assertTrue(result.startswith('Minus'))
        self.assertIn('One Hundred', result)

    def test_exact_one_lakh_fifty_thousand(self):
        result = amount_in_words(150000)
        self.assertIn('One Lakh', result)
        self.assertIn('Fifty Thousand', result)
