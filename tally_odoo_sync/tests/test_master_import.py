# -*- coding: utf-8 -*-
"""Tests for the Tally master-data importer subsystem."""
import base64
import json

from odoo.tests import TransactionCase, tagged

from odoo.addons.tally_odoo_sync.services import tally_master_parser as parser
from odoo.addons.tally_odoo_sync.services import tally_master_utils as utils


def _sample_payload(**overrides):
    msgs = [
        {'Ledger': {
            'NAME': 'ABC FOODS PVT. LTD.',
            'GUID': 'guid-abc-001',
            'ALTERID': '1001',
            'PARENT': 'Sundry Debtors',
            'PARTYGSTIN': '27AABCF1234H1Z5',
            'STATE': 'Maharashtra',
            'LEDGERPHONE': '022-12345678',
            'EMAIL': 'accts@abcfoods.example',
            'MAILINGADDRESS': 'Shop 1, Market Road',
            'PINCODE': '400001',
        }},
        {'Ledger': {
            'NAME': 'XYZ SUPPLIERS',
            'GUID': 'guid-xyz-002',
            'ALTERID': '1002',
            'PARENT': 'Sundry Creditors',
            'PARTYGSTIN': '27AAAXS5678M1Z2',
            'STATE': 'Maharashtra',
        }},
        {'Ledger': {
            'NAME': 'HDFC BANK CURRENT',
            'GUID': 'guid-bank-003',
            'PARENT': 'Bank Accounts',
        }},
        {'Group': {'NAME': 'Sundry Debtors', 'GUID': 'grp-001', 'PARENT': 'Current Assets'}},
        {'Currency': {'NAME': 'INR', 'GUID': 'cur-001'}},
    ]
    for key, val in overrides.items():
        pass
    return {'tallymessage': msgs}


def _stock_payload():
    return {'tallymessage': [
        {'metadata': {'type': 'Unit', 'name': 'BOX', 'reservedname': ''},
         'GUID': 'u-box-001', 'ALTERID': ' 86566', 'PARENT': '',
         'ISSIMPLEUNIT': True},
        {'metadata': {'type': 'Stock Item', 'name': '1 KG CAKE BASE', 'reservedname': ''},
         'GUID': 's-cake-001', 'ALTERID': ' 86600', 'PARENT': 'CONSUMABLE PROD',
         'BASEUNITS': 'BOX',
         'HSNDETAILS': [{'applicablefrom': '20180401', 'HSNCODE': '4819'}],
         'GSTDETAILS': [{'applicablefrom': '20180401', 'TAXABILITY': 'Taxable',
                         'STATEWISEDETAILS': [{'STATENAME': 'Any', 'RATEDETAILS': [
                             {'GSTRATEDUTYHEAD': 'CGST', 'GSTRATE': ' 9'},
                             {'GSTRATEDUTYHEAD': 'SGST/UTGST', 'GSTRATE': ' 9'},
                             {'GSTRATEDUTYHEAD': 'IGST', 'GSTRATE': ' 18'}]}]}]},
        {'metadata': {'type': 'Stock Item', 'name': 'OLD ITEM', 'reservedname': ''},
         'GUID': 's-old-002', 'PARENT': 'CONSUMABLE PROD',
         'BASEUNITS': 'BOX', 'ISDELETED': True},
        {'metadata': {'type': 'Stock Group', 'name': 'CONSUMABLE PROD', 'reservedname': ''},
         'GUID': 'sg-001', 'PARENT': 'Stock Groups'},
    ]}


def _upload_batch(env, payload=None, company=None):
    payload = payload or _sample_payload()
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-16')
    batch = env['tally.import.batch'].create({
        'company_id': (company or env.company).id,
        'source_filename': 'Master.json',
        'source_file': base64.b64encode(raw),
    })
    return batch


@tagged('post_install', '-at_install')
class TestMasterParser(TransactionCase):

    def test_utf16_bom_supported(self):
        payload = _sample_payload()
        raw = b'\xff\xfe' + json.dumps(payload).encode('utf-16-le')
        root, enc = parser.parse_master_json(raw)
        self.assertIn('tallymessage', root)
        self.assertIn('utf-16', enc)

    def test_utf8_supported(self):
        payload = _sample_payload()
        root, _enc = parser.parse_master_json(json.dumps(payload).encode('utf-8'))
        self.assertIn('tallymessage', root)

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            parser.parse_master_json(b'{not valid json')

    def test_missing_root_raises(self):
        with self.assertRaises(ValueError):
            parser.analyse_root({'nothing': 'here'})

    def test_unknown_types_preserved_not_dropped(self):
        root = {'tallymessage': [{'MysteryThing': {'NAME': 'X', 'GUID': 'g1'}}]}
        summary = parser.analyse_root(root)
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['records'][0]['tally_name'], 'X')

    def test_flatten_extracts_identity(self):
        summary = parser.analyse_root(_sample_payload())
        by_name = {r['tally_name']: r for r in summary['records']}
        rec = by_name['ABC FOODS PVT. LTD.']
        self.assertEqual(rec['record_type'], 'Ledger')
        self.assertEqual(rec['tally_guid'], 'guid-abc-001')
        self.assertEqual(rec['tally_parent'], 'Sundry Debtors')
        self.assertFalse(summary['has_stock_items'])

    def test_stock_item_detection(self):
        root = {'tallymessage': [{'Stock Item': {'NAME': 'WIDGET', 'GUID': 's1'}}]}
        summary = parser.analyse_root(root)
        self.assertTrue(summary['has_stock_items'])

    def test_metadata_type_layout_real_tally_export(self):
        # Real Tally master exports nest type/name under metadata and keep
        # reference fields (currencyname) at top level.
        root = {'tallymessage': [
            {'metadata': {'type': 'Ledger', 'name': 'BOISSON KITCHEN AND BAR PRIVATE LIMITED'},
             'guid': 'tally-guid-1', 'alterid': ' 81363', 'parent': 'DEBTORS-NORTH',
             'currencyname': '₹', 'partygstin': '07AAICB0603R1ZJ'},
            {'metadata': {'type': 'Group', 'name': 'Bank Accounts'},
             'guid': 'tally-guid-2', 'parent': 'Current Assets'},
            {'metadata': {'type': 'Currency', 'name': 'Rupee'},
             'guid': 'tally-guid-3'},
        ]}
        summary = parser.analyse_root(root)
        by_type = {r['tally_guid']: r for r in summary['records']}
        self.assertEqual(by_type['tally-guid-1']['record_type'], 'Ledger')
        self.assertEqual(by_type['tally-guid-1']['tally_name'],
                         'BOISSON KITCHEN AND BAR PRIVATE LIMITED')
        self.assertEqual(by_type['tally-guid-1']['tally_parent'], 'DEBTORS-NORTH')
        self.assertEqual(by_type['tally-guid-2']['record_type'], 'Group')
        self.assertEqual(by_type['tally-guid-3']['record_type'], 'Currency')
        self.assertFalse(summary['has_stock_items'])

    def test_mailing_details_extraction(self):
        payload = {
            'ledmailingdetails': [{
                'address': [{'metadata': True, 'type': 'String'},
                            'THIRD FLOOR, D-21', 'NEW DELHI-110024'],
                'state': 'Delhi', 'country': 'India'}],
            'priorstatename': 'Delhi', 'oldcountryname': 'India',
            'email': 'Accts@Example.COM', 'ledgermobile': '9810124136',
            'ledgercontact': 'SUMAN KUMAR', 'partygstin': '07AAICB0603R1ZJ',
            'gstregistrationtype': 'Regular',
        }
        bits = utils.extract_address_bits(payload)
        self.assertIn('THIRD FLOOR', bits['street'])
        self.assertEqual(bits['state'], 'Delhi')
        self.assertEqual(bits['country'], 'India')
        self.assertEqual(bits['email'], 'Accts@Example.COM')
        self.assertEqual(utils.normalize_email(bits['email']), 'accts@example.com')
        self.assertEqual(bits['gstin'], '07AAICB0603R1ZJ')
        self.assertTrue(utils.is_valid_gstin(bits['gstin']))


@tagged('post_install', '-at_install')
class TestMasterUtils(TransactionCase):

    def test_normalize_name_variants_equal(self):
        self.assertEqual(utils.normalize_name('ABC FOODS PVT. LTD.'),
                         utils.normalize_name('ABC Foods Pvt Ltd'))

    def test_gstin_validation(self):
        self.assertTrue(utils.is_valid_gstin('27AABCF1234H1Z5'))
        self.assertFalse(utils.is_valid_gstin('BAD-GSTIN'))
        self.assertFalse(utils.is_valid_gstin(''))

    def test_pattern_match(self):
        self.assertTrue(utils.match_pattern('DEBTOR*', 'Debtors-North'))
        self.assertTrue(utils.match_pattern('Sundry Debtors', 'sundry debtors'))
        self.assertFalse(utils.match_pattern('Bank*', 'Cash-in-Hand'))


@tagged('post_install', '-at_install')
class TestMasterImportFlow(TransactionCase):

    def test_analyse_classify_match(self):
        batch = _upload_batch(self.env)
        batch.action_analyse()
        self.assertEqual(batch.state, 'analysed')
        self.assertEqual(batch.total_records, 5)
        self.assertEqual(batch.ledger_count, 3)
        self.assertEqual(batch.group_count, 1)
        self.assertEqual(batch.currency_count, 1)
        batch.action_classify()
        self.assertEqual(batch.customer_count, 1)
        self.assertEqual(batch.vendor_count, 1)
        batch.action_match()
        self.assertEqual(batch.state, 'ready')
        self.assertEqual(batch.new_count, 2)

    def test_dry_run_creates_no_partners(self):
        before = self.env['res.partner'].search_count([])
        batch = _upload_batch(self.env)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        batch.action_dry_run()
        self.assertEqual(self.env['res.partner'].search_count([]), before)
        self.assertTrue(all(r.import_status == 'dry_run'
                            for r in batch.record_ids.filtered(
                                lambda r: r.classification in ('customer', 'vendor'))))

    def test_import_creates_customer_vendor_and_mappings(self):
        batch = _upload_batch(self.env)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        self.assertEqual(batch.state, 'completed')
        customer = self.env['res.partner'].search([('tally_guid', '=', 'guid-abc-001')], limit=1)
        vendor = self.env['res.partner'].search([('tally_guid', '=', 'guid-xyz-002')], limit=1)
        self.assertTrue(customer)
        self.assertTrue(vendor)
        self.assertEqual(customer.vat, '27AABCF1234H1Z5')
        self.assertGreaterEqual(customer.customer_rank, 1)
        self.assertGreaterEqual(vendor.supplier_rank, 1)
        # Canonical mapping usable by invoice sync.
        resolved = self.env['tally.mapping'].resolve_customer(customer, batch.company_id)
        self.assertEqual(resolved, 'ABC FOODS PVT. LTD.')
        identity = self.env['tally.mapping'].resolve_tally_identity(customer, batch.company_id)
        self.assertEqual(identity['guid'], 'guid-abc-001')

    def test_idempotent_double_import(self):
        batch = _upload_batch(self.env)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        count_after_first = self.env['res.partner'].search_count(
            [('tally_guid', 'in', ['guid-abc-001', 'guid-xyz-002'])])
        # Re-run matching+import on the same batch: must update, not duplicate.
        batch.write({'state': 'ready'})
        batch.record_ids.write({'import_status': 'pending'})
        batch.action_import()
        count_after_second = self.env['res.partner'].search_count(
            [('tally_guid', 'in', ['guid-abc-001', 'guid-xyz-002'])])
        self.assertEqual(count_after_first, count_after_second)
        self.assertEqual(count_after_second, 2)

    def test_customer_vendor_same_partner_no_duplicate(self):
        payload = {'tallymessage': [
            {'Ledger': {'NAME': 'DUAL PARTY', 'GUID': 'g-cust',
                        'PARENT': 'Sundry Debtors', 'PARTYGSTIN': '27AABCD1234E1Z6'}},
            {'Ledger': {'NAME': 'DUAL PARTY', 'GUID': 'g-vend',
                        'PARENT': 'Sundry Creditors', 'PARTYGSTIN': '27AABCD1234E1Z6'}},
        ]}
        batch = _upload_batch(self.env, payload)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        partners = self.env['res.partner'].search([('vat', '=', '27AABCD1234E1Z6')])
        self.assertEqual(len(partners), 1)
        self.assertGreaterEqual(partners.customer_rank, 1)
        self.assertGreaterEqual(partners.supplier_rank, 1)

    def test_failed_record_retry(self):
        batch = _upload_batch(self.env)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        rec = batch.record_ids.filtered(lambda r: r.tally_guid == 'guid-abc-001')
        rec.write({'import_status': 'failed', 'error_message': 'simulated'})
        batch.action_retry_failed()
        self.assertEqual(rec.import_status, 'pending')
        self.assertEqual(batch.state, 'ready')

    def test_reconciliation_numbers_reconcile(self):
        batch = _upload_batch(self.env)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        stats = batch.get_reconciliation()
        self.assertEqual(stats['customer']['tally_source'], 1)
        self.assertEqual(stats['customer']['odoo_mapped'], 1)
        self.assertEqual(stats['groups']['tally_source'], 1)
        self.assertIn('No Stock Item', stats['product_note'])

    def test_opening_balance_staged_not_posted(self):
        payload = {'tallymessage': [
            {'Ledger': {'NAME': 'BAL PARTY', 'GUID': 'g-bal',
                        'PARENT': 'Sundry Debtors', 'OPENINGBALANCE': '5000 Dr'}},
        ]}
        batch = _upload_batch(self.env, payload)
        batch.action_analyse()
        rec = batch.record_ids[0]
        self.assertIn('5000', rec.opening_balance or '')
        # No journal entries may be created by master import.
        moves = self.env['account.move'].search(
            [('ref', 'ilike', 'BAL PARTY')])
        self.assertFalse(moves)

    def test_existing_guid_matched_not_duplicated(self):
        existing = self.env['res.partner'].create({
            'name': 'ABC FOODS PVT. LTD.', 'tally_guid': 'guid-abc-001',
        })
        batch = _upload_batch(self.env)
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        rec = batch.record_ids.filtered(lambda r: r.tally_guid == 'guid-abc-001')
        self.assertEqual(rec.match_status, 'matched')
        self.assertEqual(rec.match_method, 'guid')
        self.assertEqual(rec.odoo_record_id, existing.id)

    def test_duplicate_guid_in_source_merged_not_failed(self):
        # Real Tally exports repeat altered masters: same GUID twice.
        # Analyse must merge them into one staged row, never raise.
        payload = {'tallymessage': [
            {'Ledger': {'NAME': 'DUP PARTY', 'GUID': 'g-dup-1',
                        'PARENT': 'Sundry Debtors', 'PARTYGSTIN': '27AABCD1234E1Z6'}},
            {'Ledger': {'NAME': 'DUP PARTY', 'GUID': 'g-dup-1',
                        'PARENT': 'Sundry Debtors', 'PARTYGSTIN': '27AABCD1234E1Z6'}},
            {'Ledger': {'NAME': 'OTHER PARTY', 'GUID': 'g-other-2',
                        'PARENT': 'Sundry Debtors'}},
        ]}
        batch = _upload_batch(self.env, payload)
        batch.action_analyse()  # must not raise UNIQUE violation
        self.assertEqual(batch.state, 'analysed')
        dups = batch.record_ids.filtered(lambda r: r.tally_guid == 'g-dup-1')
        self.assertEqual(len(dups), 1)
        self.assertIn('Duplicate source entry merged', dups.warning_message or '')
        self.assertIn('Merged 1 duplicate', batch.log or '')
        batch.action_classify()
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        self.assertEqual(batch.state, 'completed')
        partners = self.env['res.partner'].search([('tally_guid', '=', 'g-dup-1')])
        self.assertEqual(len(partners), 1)

    def test_stock_bits_extraction(self):
        payload = _stock_payload()['tallymessage'][1]
        bits = utils.extract_stock_bits(payload)
        self.assertEqual(bits['base_uom'], 'BOX')
        self.assertEqual(bits['hsn'], '4819')
        self.assertEqual(bits['gst_rate'], 18.0)
        self.assertEqual(bits['taxability'], 'Taxable')
        self.assertFalse(bits['is_deleted'])
        deleted = utils.extract_stock_bits(
            _stock_payload()['tallymessage'][2])
        self.assertTrue(deleted['is_deleted'])

    def test_stock_import_creates_uom_and_products(self):
        batch = _upload_batch(self.env, _stock_payload())
        batch.action_analyse()
        self.assertEqual(batch.stock_item_count, 2)
        batch.action_classify()
        self.assertEqual(batch.product_count, 2)
        self.assertEqual(batch.uom_count, 1)
        batch.action_match()
        self.assertEqual(batch.state, 'ready')
        batch.action_dry_run()
        self.assertEqual(self.env['product.template'].search_count(
            [('name', '=', '1 KG CAKE BASE')]), 0)
        batch.write({'dry_run': False, 'state': 'ready'})
        batch.record_ids.write({'import_status': 'pending'})
        batch.action_import()
        self.assertEqual(batch.state, 'completed')
        # UOM imported (or identically named Odoo unit adopted) and reused.
        uom = self.env['uom.uom'].search([('name', '=ilike', 'BOX')], limit=1)
        self.assertTrue(uom)
        template = self.env['product.template'].search(
            [('name', '=', '1 KG CAKE BASE')], limit=1)
        self.assertTrue(template)
        self.assertEqual(template.uom_id.id, uom.id)
        self.assertEqual(template.l10n_in_hsn_code, '4819')
        self.assertEqual(template.categ_id.name, 'CONSUMABLE PROD')
        # Canonical mapping usable by invoice sync.
        resolved = self.env['tally.mapping'].resolve_product(
            template.product_variant_id, batch.company_id)
        self.assertEqual(resolved, '1 KG CAKE BASE')
        # Deleted item never becomes a product.
        self.assertFalse(self.env['product.template'].search(
            [('name', '=', 'OLD ITEM')]))
        skipped = batch.record_ids.filtered(
            lambda r: r.tally_guid == 's-old-002')
        self.assertEqual(skipped.import_status, 'skipped')

    def test_stock_import_idempotent(self):
        batch = _upload_batch(self.env, _stock_payload())
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        first_ids = self.env['product.template'].search(
            [('name', '=', '1 KG CAKE BASE')]).ids
        batch.write({'state': 'ready'})
        batch.record_ids.write({'import_status': 'pending'})
        batch.action_import()
        second_ids = self.env['product.template'].search(
            [('name', '=', '1 KG CAKE BASE')]).ids
        self.assertEqual(first_ids, second_ids)

    def test_stock_group_is_trace_not_product(self):
        batch = _upload_batch(self.env, _stock_payload())
        batch.action_analyse()
        batch.action_classify()
        group_rows = batch.record_ids.filtered(
            lambda r: r.record_type == 'Stock Group')
        self.assertEqual(len(group_rows), 1)
        self.assertEqual(group_rows.classification, 'group')
        batch.action_match()
        batch.write({'dry_run': False})
        batch.action_import()
        self.assertFalse(self.env['product.template'].search(
            [('name', '=', 'CONSUMABLE PROD')]))

    def test_tally_tax_kind_detection(self):
        Batch = self.env['tally.import.batch']
        self.assertEqual(Batch._tally_tax_kind('CGST'), 'CGST')
        self.assertEqual(Batch._tally_tax_kind('SGST'), 'SGST')
        self.assertEqual(Batch._tally_tax_kind('IGST'), 'IGST')
        self.assertEqual(Batch._tally_tax_kind('CESS'), 'CESS')
        self.assertEqual(Batch._tally_tax_kind('CGST RCM'), 'RCM')
        self.assertEqual(Batch._tally_tax_kind('TDS ON SALARY'), 'OTHER')
        self.assertEqual(Batch._tally_tax_kind('TCS ON SALES'), 'OTHER')
        self.assertEqual(Batch._tally_tax_kind(''), 'OTHER')

    def _tax_payload(self):
        return {'tallymessage': [
            {'Ledger': {'NAME': 'CGST', 'GUID': 'tax-cgst-1', 'PARENT': 'Duties & Taxes'}},
            {'Ledger': {'NAME': 'SGST', 'GUID': 'tax-sgst-1', 'PARENT': 'Duties & Taxes'}},
            {'Ledger': {'NAME': 'IGST', 'GUID': 'tax-igst-1', 'PARENT': 'Duties & Taxes'}},
            {'Ledger': {'NAME': 'TDS ON SALARY', 'GUID': 'tax-tds-1',
                        'PARENT': 'Duties & Taxes'}},
        ]}

    def _make_sale_tax(self, name, amount):
        return self.env['account.tax'].create({
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.env.company.id,
        })

    def test_tax_import_wires_gst_mappings(self):
        cgst = self._make_sale_tax('CGST 9%', 9.0)
        sgst = self._make_sale_tax('SGST 9%', 9.0)
        taxes_before = self.env['account.tax'].search_count([])
        batch = _upload_batch(self.env, self._tax_payload())
        batch.action_analyse()
        batch.action_classify()
        self.assertEqual(batch.tax_count, 4)
        batch.action_match()
        batch.action_validate()
        batch.write({'dry_run': False})
        batch.action_import()
        self.assertEqual(batch.state, 'completed')
        # No Odoo tax may be fabricated from Tally ledgers.
        self.assertEqual(self.env['account.tax'].search_count([]), taxes_before)
        # GST ledgers wired to the single-kind sale taxes.
        self.assertEqual(
            self.env['tally.mapping'].resolve_tax(cgst, batch.company_id), 'CGST')
        self.assertEqual(
            self.env['tally.mapping'].resolve_tax(sgst, batch.company_id), 'SGST')
        mapping = self.env['tally.mapping'].search([
            ('mapping_type', '=', 'tax'), ('odoo_tax_id', '=', cgst.id)], limit=1)
        self.assertEqual(mapping.tally_guid, 'tax-cgst-1')
        # IGST has no Odoo tax of its kind: surfaced, not mapped.
        igst_row = batch.record_ids.filtered(lambda r: r.tally_guid == 'tax-igst-1')
        self.assertEqual(igst_row.import_status, 'review')
        self.assertFalse(self.env['tally.mapping'].search([
            ('mapping_type', '=', 'tax'), ('tally_guid', '=', 'tax-igst-1')]))
        # TDS is parked as trace, never mapped.
        tds_row = batch.record_ids.filtered(lambda r: r.tally_guid == 'tax-tds-1')
        self.assertEqual(tds_row.import_status, 'skipped')
        self.assertFalse(self.env['tally.mapping'].search([
            ('mapping_type', '=', 'tax'), ('tally_guid', '=', 'tax-tds-1')]))

    def test_tax_multi_candidate_goes_to_review(self):
        self._make_sale_tax('CGST 2.5%', 2.5)
        self._make_sale_tax('CGST 9%', 9.0)
        batch = _upload_batch(self.env, self._tax_payload())
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        cgst_row = batch.record_ids.filtered(lambda r: r.tally_guid == 'tax-cgst-1')
        self.assertEqual(cgst_row.match_status, 'manual_review')
        import json as _json
        candidates = _json.loads(cgst_row.candidate_json or '[]')
        self.assertEqual(len(candidates), 2)
        batch.write({'dry_run': False})
        batch.action_import()
        self.assertFalse(self.env['tally.mapping'].search([
            ('mapping_type', '=', 'tax'), ('tally_guid', '=', 'tax-cgst-1')]))

    def test_tax_shared_ledger_multi_tax(self):
        # Several Odoo taxes may share one Tally ledger: GUID lives on the
        # primary row, the rest are name-bound. Both must resolve for sync.
        low = self._make_sale_tax('CGST 2.5%', 2.5)
        high = self._make_sale_tax('CGST 9%', 9.0)
        company = self.env.company
        self.env['tally.mapping'].create({
            'mapping_type': 'tax', 'odoo_tax_id': low.id,
            'tally_name': 'CGST', 'tally_guid': 'tax-cgst-1',
            'company_id': company.id,
        })
        self.env['tally.mapping'].create({
            'mapping_type': 'tax', 'odoo_tax_id': high.id,
            'tally_name': 'CGST', 'company_id': company.id,
        })
        self.assertEqual(
            self.env['tally.mapping'].resolve_tax(low, company), 'CGST')
        self.assertEqual(
            self.env['tally.mapping'].resolve_tax(high, company), 'CGST')
        # Re-match links the staging row via GUID without disturbing either mapping.
        batch = _upload_batch(self.env, self._tax_payload())
        batch.action_analyse()
        batch.action_classify()
        batch.action_match()
        row = batch.record_ids.filtered(lambda r: r.tally_guid == 'tax-cgst-1')
        self.assertEqual(row.match_status, 'matched')
        self.assertEqual(row.odoo_record_id, low.id)
        batch.write({'dry_run': False})
        batch.action_import()
        self.assertEqual(
            self.env['tally.mapping'].search_count([
                ('mapping_type', '=', 'tax'), ('tally_guid', '=', 'tax-cgst-1')]), 1)
        self.assertEqual(
            self.env['tally.mapping'].resolve_tax(high, company), 'CGST')

    def test_uom_shared_tally_name_allowed(self):
        # Two Odoo UOMs may share one Tally unit (explicit equivalence);
        # uniqueness is per Odoo record, and both must resolve for sync.
        company = self.env.company
        nos = self.env['uom.uom'].create({'name': 'Nos.', 'relative_factor': 1.0})
        units = self.env.ref('uom.product_uom_unit')
        self.env['tally.mapping'].create({
            'mapping_type': 'uom', 'odoo_uom_id': nos.id,
            'tally_name': 'Nos.', 'tally_guid': 'u-nos-1',
            'company_id': company.id,
        })
        # Second Odoo UOM, same Tally name, no GUID (primary row keeps it).
        self.env['tally.mapping'].create({
            'mapping_type': 'uom', 'odoo_uom_id': units.id,
            'tally_name': 'Nos.', 'company_id': company.id,
            'match_method': 'manual', 'confidence': 70.0,
            'notes': 'Explicit equivalence configured by user.',
        })
        self.assertEqual(
            self.env['tally.mapping'].resolve_uom(nos, company), 'Nos.')
        self.assertEqual(
            self.env['tally.mapping'].resolve_uom(units, company), 'Nos.')
