# -*- coding: utf-8 -*-
"""Tally master-data import batch: upload to reconcile.

Lifecycle:
    draft -> analysing -> analysed -> classifying -> matching
          -> ready -> importing -> completed | failed
                            -> cancelled

Dry run NEVER creates business data (only staging rows).
Import is idempotent on Tally GUID and never triggers invoice sync.
"""
import base64
import hashlib
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.tally_odoo_sync.services import tally_master_parser as parser
from odoo.addons.tally_odoo_sync.services import tally_master_utils as utils

_logger = logging.getLogger(__name__)

STATES = [
    ('draft', 'Draft'),
    ('analysing', 'Analysing'),
    ('analysed', 'Analysed'),
    ('classifying', 'Classifying'),
    ('matching', 'Matching'),
    ('ready', 'Ready'),
    ('importing', 'Importing'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
]

# Classifications that become res.partner contacts.
_PARTNER_CLASSIFICATIONS = ('customer', 'vendor')
# Ledger classifications kept as accounting trace (no partner created).
_ACCOUNTING_CLASSIFICATIONS = (
    'bank', 'cash', 'tax', 'income', 'expense',
    'asset', 'liability', 'equity', 'accounting', 'other',
)


class TallyImportBatch(models.Model):
    _name = 'tally.import.batch'
    _description = 'Tally Master Import Batch'
    _order = 'create_date desc'
    _rec_name = 'name'

    name = fields.Char(string='Batch', required=True, default='New')
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, index=True,
        default=lambda self: self.env.company,
    )
    source_filename = fields.Char(string='Source File')
    source_file = fields.Binary(string='Master File (JSON)', attachment=True)
    source_hash = fields.Char(string='SHA-256', readonly=True, index=True)
    source_size = fields.Integer(string='Size (bytes)', readonly=True)
    source_format = fields.Char(string='Format', readonly=True)
    encoding_used = fields.Char(string='Encoding Detected', readonly=True)
    tally_company_name = fields.Char(string='Tally Company')

    state = fields.Selection(selection=STATES, default='draft', index=True)
    import_date = fields.Datetime(string='Import Date', default=fields.Datetime.now)
    started_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)
    created_by = fields.Many2one(
        'res.users', default=lambda self: self.env.user, readonly=True,
    )
    dry_run = fields.Boolean(
        string='Dry Run Mode',
        help="When set, Import executes analysis only and creates no business data.",
    )

    # -- Source counters ------------------------------------------------------
    total_records = fields.Integer(readonly=True)
    ledger_count = fields.Integer(readonly=True)
    group_count = fields.Integer(readonly=True)
    currency_count = fields.Integer(readonly=True)
    stock_item_count = fields.Integer(readonly=True)
    other_count = fields.Integer(readonly=True)

    # -- Classification counters -----------------------------------------------
    customer_count = fields.Integer(readonly=True)
    vendor_count = fields.Integer(readonly=True)
    account_count = fields.Integer(readonly=True)
    tax_count = fields.Integer(readonly=True)
    uom_count = fields.Integer(readonly=True)
    product_count = fields.Integer(readonly=True)

    # -- Result counters --------------------------------------------------------
    new_count = fields.Integer(readonly=True)
    updated_count = fields.Integer(readonly=True)
    matched_count = fields.Integer(readonly=True)
    review_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)
    skipped_count = fields.Integer(readonly=True)
    created_count = fields.Integer(string='Created', readonly=True)
    has_stock_items = fields.Boolean(
        readonly=True,
        help="Whether the source file contained Stock Item records.",
    )
    product_warning = fields.Text(readonly=True)
    record_ids = fields.One2many('tally.import.record', 'batch_id', string='Records')
    log = fields.Text(string='Run Log', readonly=True)

    _sql_constraints = [
        ('source_hash_company_uniq', 'UNIQUE(source_hash, company_id)',
         'This file was already imported for this company (same content hash). '
         'Re-run is blocked to protect idempotency; create a new batch only for new content.'),
    ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, message):
        for rec in self:
            rec.log = '%s\n[%s] %s' % (
                rec.log or '', fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message)
        _logger.info('tally master import: %s', message)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'tally.import.batch') or _('Tally Import')
        return super().create(vals_list)

    def _get_file_bytes(self):
        self.ensure_one()
        if not self.source_file:
            raise UserError(_('Please upload a Tally master file first.'))
        try:
            return base64.b64decode(self.source_file)
        except Exception as exc:
            raise UserError(_('Could not decode uploaded file: %s', exc)) from exc

    # ------------------------------------------------------------------
    # Step 1: ANALYSE — decode, validate, stage raw records
    # ------------------------------------------------------------------
    def action_analyse(self):
        for batch in self:
            if batch.state in ('analysing', 'classifying', 'matching', 'importing'):
                raise UserError(_('Analyse is blocked while a run is in progress (%s).', batch.state))
            # Re-analysis rebuilds staging from the stored file, so reset
            # all downstream counters (import itself stays idempotent via GUID).
            batch.write({
                'state': 'analysing', 'started_at': fields.Datetime.now(),
                'completed_at': False,
                'customer_count': 0, 'vendor_count': 0, 'account_count': 0,
                'tax_count': 0, 'uom_count': 0, 'product_count': 0,
                'new_count': 0, 'updated_count': 0, 'matched_count': 0,
                'review_count': 0, 'error_count': 0, 'skipped_count': 0,
                'created_count': 0,
            })
            try:
                raw = batch._get_file_bytes()
                batch.source_size = len(raw)
                batch.source_hash = hashlib.sha256(raw).hexdigest()
                root, encoding = parser.parse_master_json(raw)
                batch.encoding_used = encoding
                summary = parser.analyse_root(root)
                batch._stage_records(summary['records'])
                batch._write_source_counters(summary)
                batch.source_format = 'Tally JSON (tallymessage)'
                batch.state = 'analysed'
                batch._log('Analysed %s: %d records (%s). Encoding: %s.' % (
                    batch.source_filename or 'file', summary['total'],
                    summary['by_type'], encoding))
            except ValueError as exc:
                batch.write({'state': 'failed'})
                batch._log('ANALYSE FAILED: %s' % exc)
                raise UserError(_('File validation failed: %s', exc)) from exc
        return True

    def _stage_records(self, flat_records):
        self.ensure_one()
        Record = self.env['tally.import.record']
        # Idempotent staging: drop previous staging rows for this batch only.
        self.record_ids.unlink()
        # Dedupe: real Tally exports repeat the same GUID (altered masters
        # appear in multiple messages). Merging here keeps the DB-level
        # UNIQUE(batch_id, tally_guid) safety net from aborting the import,
        # while every duplicate is disclosed on the kept row + batch log
        # (never silently discarded).
        kept = {}   # guid -> vals dict (insertion ordered)
        order = []  # guids in first-seen order + None placeholders for guid-less rows
        merged = 0
        for rec in flat_records:
            rtype = rec['record_type']
            if rtype not in dict(Record._fields['record_type'].selection):
                rtype = 'Other' if rtype != 'Unknown' else 'Unknown'
            bits = utils.extract_address_bits(rec.get('raw_payload') or {})
            guid = (rec.get('tally_guid') or '')[:255] or False
            vals = {
                'batch_id': self.id,
                'source_index': rec.get('source_index', 0),
                'record_type': rtype,
                'tally_name': (rec.get('tally_name') or '')[:255],
                'tally_guid': guid,
                'tally_alter_id': (rec.get('tally_alter_id') or '')[:64] or False,
                'tally_parent': (rec.get('tally_parent') or '')[:255],
                'opening_balance': (bits.get('opening_balance') or '')[:255],
                'raw_payload': json.dumps(rec.get('raw_payload') or {}, default=str, ensure_ascii=False),
            }
            if guid and guid in kept:
                first = kept[guid]
                alt_name = vals['tally_name']
                note = 'Duplicate source entry merged (row %s' % vals['source_index']
                if alt_name and alt_name != first['tally_name']:
                    note += ', name variant %r' % alt_name
                note += ').'
                first['warning_message'] = ((first.get('warning_message') or '') + '\n' + note).strip()
                merged += 1
                continue
            if guid:
                kept[guid] = vals
                order.append(guid)
            else:
                # Guid-less rows carry NULL guids: no constraint conflict, keep all.
                order.append(vals)
        vals_list = [kept[g] if isinstance(g, str) else g for g in order]
        for i in range(0, len(vals_list), 500):
            Record.create(vals_list[i:i + 500])
        if merged:
            self._log('Merged %d duplicate-GUID source entries into staged rows.' % merged)

    def _write_source_counters(self, summary):
        by_type = summary.get('by_type', {})
        self.write({
            'total_records': summary.get('total', 0),
            'ledger_count': sum(v for k, v in by_type.items() if k == 'Ledger'),
            'group_count': sum(v for k, v in by_type.items() if k == 'Group'),
            'currency_count': sum(v for k, v in by_type.items() if k == 'Currency'),
            'stock_item_count': sum(v for k, v in by_type.items() if k == 'Stock Item'),
            'other_count': sum(v for k, v in by_type.items()
                               if k not in ('Ledger', 'Group', 'Currency', 'Stock Item')),
            'has_stock_items': summary.get('has_stock_items', False),
            'product_warning': (
                '' if summary.get('has_stock_items') else
                'Source file contains no Stock Item records: product/UOM/HSN import '
                'is unavailable for this file. Upload a Tally Stock Item export '
                'to enable the product path.'),
        })

    # ------------------------------------------------------------------
    # Step 2: CLASSIFY — configurable group rules
    # ------------------------------------------------------------------
    def action_classify(self):
        for batch in self:
            if batch.state not in ('analysed', 'failed'):
                raise UserError(_('Classify after a successful analysis.'))
            batch.write({'state': 'classifying'})
            self.env['tally.group.mapping'].ensure_defaults()
            GroupMap = self.env['tally.group.mapping']
            counters = {'customer': 0, 'vendor': 0, 'account': 0, 'tax': 0,
                        'uom': 0, 'product': 0}
            records = batch.record_ids
            # Cache rule evaluation per distinct parent (perf: 3k ledgers, few groups).
            class_cache = {}
            for rec in records:
                if rec.record_type == 'Ledger':
                    parent = rec.tally_parent or ''
                    if parent not in class_cache:
                        classification, _rule = GroupMap.classify(parent, batch.company_id)
                        class_cache[parent] = classification
                    classification = class_cache[parent]
                elif rec.record_type == 'Group':
                    classification = 'group'
                elif rec.record_type == 'Currency':
                    classification = 'currency'
                elif rec.record_type == 'Stock Item':
                    classification = 'product'
                elif rec.record_type == 'Stock Group':
                    # Stock groups become product.category rows during product
                    # import (get-or-create); the staging row itself is trace.
                    classification = 'group'
                elif rec.record_type == 'Unit':
                    classification = 'uom'
                elif 'tax' in (rec.tally_parent or '').lower():
                    classification = 'tax'
                else:
                    classification = 'unsupported'
                if classification not in dict(
                        self.env['tally.import.record']._fields['classification'].selection):
                    classification = 'other'
                rec.write({'classification': classification})
                if classification == 'customer':
                    counters['customer'] += 1
                elif classification == 'vendor':
                    counters['vendor'] += 1
                elif classification == 'tax':
                    counters['tax'] += 1
                elif classification == 'product':
                    counters['product'] += 1
                elif classification == 'uom':
                    counters['uom'] += 1
                elif classification in _ACCOUNTING_CLASSIFICATIONS + ('other', 'group', 'currency'):
                    counters['account'] += 1
            batch.write({
                'customer_count': counters['customer'],
                'vendor_count': counters['vendor'],
                'account_count': counters['account'],
                'tax_count': counters['tax'],
                'uom_count': counters['uom'],
                'product_count': counters['product'],
                'state': 'matching',
            })
            batch._log('Classified: customers=%d vendors=%d accounting=%d tax=%d '
                       'products=%d uom=%d.' % (
                           counters['customer'], counters['vendor'], counters['account'],
                           counters['tax'], counters['product'], counters['uom']))
        return True

    # ------------------------------------------------------------------
    # Step 3: MATCH — deterministic-first, preloaded indexes (no N+1)
    # ------------------------------------------------------------------
    def action_match(self):
        for batch in self:
            if batch.state not in ('matching', 'analysed', 'classifying', 'failed'):
                raise UserError(_('Run Classify before Matching.'))
            batch._run_matching()
        return True

    def _run_matching(self):
        self.ensure_one()
        Partner = self.env['res.partner']
        Mapping = self.env['tally.mapping']

        # --- Preload indexes (single queries) --------------------------------
        guid_partners = {}
        for row in Partner.search_read(
                [('tally_guid', '!=', False)], ['tally_guid']):
            if row.get('tally_guid'):
                guid_partners[str(row['tally_guid']).strip().upper()] = row['id']
        vat_partners = {}
        for row in Partner.search_read([('vat', '!=', False)], ['vat']):
            norm = utils.normalize_gstin(row.get('vat'))
            if norm and norm not in vat_partners:
                vat_partners[norm] = row['id']
        name_partners = {}
        for partner in Partner.search([]):
            norm = utils.normalize_name(partner.name)
            if norm and norm not in name_partners:
                name_partners[norm] = partner.id
        guid_mappings = {}
        for mapping in Mapping.search([('tally_guid', '!=', False)]):
            guid_mappings[str(mapping.tally_guid).strip().upper()] = mapping

        # --- Product / UOM preloads (single queries) ---------------------------
        Product = self.env['product.template']
        Uom = self.env['uom.uom']
        prod_guid_map, prod_name_map = {}, {}
        for mapping in Mapping.search([('mapping_type', '=', 'product')]):
            if mapping.tally_guid:
                prod_guid_map[str(mapping.tally_guid).strip().upper()] = mapping
            if mapping.tally_name:
                prod_name_map.setdefault(utils.normalize_name(mapping.tally_name), mapping)
        template_names = {}
        for template in Product.search([]):
            norm = utils.normalize_name(template.name)
            if norm and norm not in template_names:
                template_names[norm] = template.id
        uom_guid_map, uom_name_map = {}, {}
        for mapping in Mapping.search([('mapping_type', '=', 'uom')]):
            if mapping.tally_guid:
                uom_guid_map[str(mapping.tally_guid).strip().upper()] = mapping
            if mapping.tally_name:
                uom_name_map.setdefault(utils.normalize_name(mapping.tally_name), mapping)
        odoo_uom_names = {}
        for uom in Uom.search([]):
            norm = utils.normalize_name(uom.name)
            if norm and norm not in odoo_uom_names:
                odoo_uom_names[norm] = uom.id

        new, matched, review, error = 0, 0, 0, 0
        records = self.record_ids.filtered(
            lambda r: r.classification in _PARTNER_CLASSIFICATIONS)
        for rec in records:
            try:
                status, method, conf, partner_id, candidates = self._match_one(
                    rec, guid_partners, vat_partners, name_partners, guid_mappings)
                vals = {
                    'match_status': status,
                    'match_method': method,
                    'match_confidence': conf,
                    'candidate_json': json.dumps(candidates or [], default=str),
                }
                if partner_id:
                    vals.update({'odoo_model': 'res.partner', 'odoo_record_id': partner_id})
                else:
                    vals.update({'odoo_model': False, 'odoo_record_id': False})
                rec.write(vals)
                if status == 'new':
                    new += 1
                elif status == 'matched':
                    matched += 1
                else:
                    review += 1
            except Exception as exc:
                rec.write({'match_status': 'manual_review',
                           'error_message': 'Matching error: %s' % exc})
                error += 1
        # Product + UOM rows get real deterministic matching too.
        for rec in self.record_ids.filtered(lambda r: r.classification == 'product'):
            try:
                status, method, conf, target_id, candidates = self._match_product(
                    rec, prod_guid_map, prod_name_map, template_names)
                rec.write({
                    'match_status': status, 'match_method': method,
                    'match_confidence': conf,
                    'candidate_json': json.dumps(candidates or [], default=str),
                    'odoo_model': 'product.product' if target_id else False,
                    'odoo_record_id': target_id or False,
                })
                new, matched, review = self._tally_match(status, new, matched, review)
            except Exception as exc:
                rec.write({'match_status': 'manual_review',
                           'error_message': 'Matching error: %s' % exc})
                error += 1
        for rec in self.record_ids.filtered(lambda r: r.classification == 'uom'):
            try:
                status, method, conf, target_id, candidates = self._match_uom(
                    rec, uom_guid_map, uom_name_map, odoo_uom_names)
                rec.write({
                    'match_status': status, 'match_method': method,
                    'match_confidence': conf,
                    'candidate_json': json.dumps(candidates or [], default=str),
                    'odoo_model': 'uom.uom' if target_id else False,
                    'odoo_record_id': target_id or False,
                })
                new, matched, review = self._tally_match(status, new, matched, review)
            except Exception as exc:
                rec.write({'match_status': 'manual_review',
                           'error_message': 'Matching error: %s' % exc})
                error += 1
        # Tax rows: wire Odoo sale taxes to Tally duty ledgers via mappings.
        # Tally ledgers carry no rate, so Odoo taxes are never fabricated here.
        company = self.company_id
        sale_taxes = self.env['account.tax'].search([
            '|', ('company_id', '=', False), ('company_id', '=', company.id),
            ('type_tax_use', '=', 'sale'), ('active', '=', True),
        ])
        service = self.env['tally.service']
        taxes_by_kind = {}
        for tax in sale_taxes:
            try:
                kind = service._classify_tax_static(tax)
            except Exception:
                kind = 'OTHER'
            taxes_by_kind.setdefault(kind, []).append(tax)
        mapped_tax_ids = set(self.env['tally.mapping'].search([
            ('mapping_type', '=', 'tax'), ('company_id', '=', company.id),
            ('odoo_tax_id', '!=', False),
        ]).mapped('odoo_tax_id').ids)
        tax_guid_map, tax_name_map = {}, {}
        for mapping in Mapping.search([('mapping_type', '=', 'tax')]):
            if mapping.tally_guid:
                tax_guid_map[str(mapping.tally_guid).strip().upper()] = mapping
            if mapping.tally_name:
                tax_name_map.setdefault(utils.normalize_name(mapping.tally_name), mapping)
        for rec in self.record_ids.filtered(lambda r: r.classification == 'tax'):
            try:
                status, method, conf, target_id, candidates = self._match_tax(
                    rec, tax_guid_map, tax_name_map, taxes_by_kind, mapped_tax_ids)
                rec.write({
                    'match_status': status, 'match_method': method,
                    'match_confidence': conf,
                    'candidate_json': json.dumps(candidates or [], default=str),
                    'odoo_model': 'account.tax' if target_id else False,
                    'odoo_record_id': target_id or False,
                })
                new, matched, review = self._tally_match(status, new, matched, review)
            except Exception as exc:
                rec.write({'match_status': 'manual_review',
                           'error_message': 'Matching error: %s' % exc})
                error += 1
        # Remaining rows (groups, currencies, accounting trace): parked, never auto-created.
        others = self.record_ids.filtered(
            lambda r: r.classification not in _PARTNER_CLASSIFICATIONS + ('product', 'uom', 'tax'))
        others.write({'match_status': 'mapping_required',
                      'match_method': 'none', 'match_confidence': 0.0})
        self.write({
            'new_count': new,
            'matched_count': matched,
            'review_count': review,
            'error_count': error,
            'state': 'ready',
        })
        self._log('Matched %d rows: new=%d matched=%d review=%d errors=%d. '
                  '%d trace rows parked as mapping-required.' % (
                      len(records) + len(self.record_ids.filtered(
                          lambda r: r.classification in ('product', 'uom'))),
                      new, matched, review, error, len(others)))

    @staticmethod
    def _tally_match(status, new, matched, review):
        if status == 'new':
            return new + 1, matched, review
        if status == 'matched':
            return new, matched + 1, review
        return new, matched, review + 1

    def _match_product(self, rec, prod_guid_map, prod_name_map, template_names):
        """Return (status, method, confidence, product_variant_id, candidates)."""
        guid = (rec.tally_guid or '').strip().upper()
        norm_name = utils.normalize_name(rec.tally_name)
        # 1. Canonical mapping by Tally GUID.
        if guid and guid in prod_guid_map:
            mapping = prod_guid_map[guid]
            if mapping.odoo_product_id:
                return 'matched', 'guid', 100.0, mapping.odoo_product_id.id, []
        # 2. Existing product mapping by Tally name (invoice-sync setup).
        if norm_name and norm_name in prod_name_map:
            mapping = prod_name_map[norm_name]
            if mapping.odoo_product_id:
                return 'matched', 'mapping', 95.0, mapping.odoo_product_id.id, []
        # 3. Exact product template name in Odoo (adopt, don't duplicate).
        if norm_name and norm_name in template_names:
            template = self.env['product.template'].browse(template_names[norm_name])
            variant = template.product_variant_id
            return 'matched', 'exact_name', 90.0, variant.id if variant else False, []
        # 4. Fuzzy candidates -> MANUAL REVIEW only.
        candidates = []
        if norm_name:
            close = [n for n in template_names if n and n[:4] == norm_name[:4]]
            scored = sorted(
                [(utils.name_similarity(norm_name, n), n) for n in close], reverse=True)
            for score, cand_name in [s for s in scored if s[0] >= 75.0][:3]:
                template = self.env['product.template'].browse(template_names[cand_name])
                candidates.append({
                    'model': 'product.product',
                    'id': template.product_variant_id.id if template.product_variant_id else False,
                    'display': template.display_name,
                    'confidence': score, 'reason': 'Fuzzy name similarity'})
        if candidates:
            return 'possible_duplicate', 'fuzzy', candidates[0]['confidence'], False, candidates
        return 'new', 'none', 0.0, False, []

    def _match_uom(self, rec, uom_guid_map, uom_name_map, odoo_uom_names):
        """Return (status, method, confidence, uom_id, candidates)."""
        guid = (rec.tally_guid or '').strip().upper()
        norm_name = utils.normalize_name(rec.tally_name)
        if guid and guid in uom_guid_map:
            mapping = uom_guid_map[guid]
            if mapping.odoo_uom_id:
                return 'matched', 'guid', 100.0, mapping.odoo_uom_id.id, []
        if norm_name and norm_name in uom_name_map:
            mapping = uom_name_map[norm_name]
            if mapping.odoo_uom_id:
                return 'matched', 'mapping', 95.0, mapping.odoo_uom_id.id, []
        if norm_name and norm_name in odoo_uom_names:
            return 'matched', 'exact_name', 90.0, odoo_uom_names[norm_name], []
        return 'new', 'none', 0.0, False, []

    @staticmethod
    def _tally_tax_kind(tally_name):
        """Classify a Tally tax-ledger name to a GST kind.

        Returns one of CGST / SGST / IGST / CESS / RCM / OTHER.
        RCM (reverse charge, purchase-side) is detected first since names
        like 'CGST RCM' contain a GST keyword but are not invoice taxes.
        """
        upper = utils.normalize_name(tally_name or '')
        if 'RCM' in upper.split():
            return 'RCM'
        if 'CESS' in upper:
            return 'CESS'
        if 'CGST' in upper:
            return 'CGST'
        if 'SGST' in upper or 'UTGST' in upper:
            return 'SGST'
        if 'IGST' in upper:
            return 'IGST'
        return 'OTHER'

    def _match_tax(self, rec, tax_guid_map, tax_name_map, taxes_by_kind, mapped_tax_ids):
        """Return (status, method, confidence, account_tax_id, candidates).

        Tally duty ledgers carry no rate, so Odoo taxes are adopted — never
        created. Non-invoice ledgers (TDS/TCS/RCM/payroll) resolve to 'new'
        and are parked as trace at import.
        """
        guid = (rec.tally_guid or '').strip().upper()
        norm_name = utils.normalize_name(rec.tally_name)
        kind = self._tally_tax_kind(rec.tally_name)
        if kind in ('RCM', 'OTHER'):
            return 'new', 'none', 0.0, False, []
        # 1. Canonical mapping by Tally GUID.
        if guid and guid in tax_guid_map:
            mapping = tax_guid_map[guid]
            if mapping.odoo_tax_id:
                return 'matched', 'guid', 100.0, mapping.odoo_tax_id.id, []
        # 2. Existing tax mapping by Tally ledger name.
        if norm_name and norm_name in tax_name_map:
            mapping = tax_name_map[norm_name]
            if mapping.odoo_tax_id:
                return 'matched', 'mapping', 95.0, mapping.odoo_tax_id.id, []
        # 3. Unmapped Odoo sale taxes of the same GST kind.
        cands = [t for t in taxes_by_kind.get(kind, []) if t.id not in mapped_tax_ids]
        if len(cands) == 1:
            return 'matched', 'auto_kind', 85.0, cands[0].id, []
        if len(cands) > 1:
            return 'manual_review', 'none', 0.0, False, [{
                'model': 'account.tax', 'id': tax.id,
                'display': '%s (%s%%)' % (tax.name, tax.amount),
                'confidence': 60.0,
                'reason': 'Several Odoo sale taxes of kind %s — pick one in Entity Mappings' % kind,
            } for tax in cands]
        return 'new', 'none', 0.0, False, []

    def _match_one(self, rec, guid_partners, vat_partners, name_partners, guid_mappings):
        """Return (status, method, confidence, partner_id, candidates)."""
        payload = rec.get_payload_dict()
        bits = utils.extract_address_bits(payload)
        guid = (rec.tally_guid or '').strip().upper()
        gstin = utils.normalize_gstin(bits.get('gstin'))
        norm_name = utils.normalize_name(rec.tally_name)
        state = utils.normalize_state(bits.get('state'))

        # 1. Existing Tally GUID (partner field) — exact.
        if guid and guid in guid_partners:
            return 'matched', 'guid', 100.0, guid_partners[guid], []
        # 2. Existing canonical mapping (tally.mapping.tally_guid).
        if guid and guid in guid_mappings:
            mapping = guid_mappings[guid]
            pid = (mapping.odoo_partner_id.id
                   if mapping.mapping_type == 'customer' and mapping.odoo_partner_id else None)
            if pid:
                return 'matched', 'mapping', 100.0, pid, []
        # 3. GSTIN exact.
        if gstin and utils.is_valid_gstin(gstin) and gstin in vat_partners:
            return 'matched', 'gstin', 100.0, vat_partners[gstin], []
        # 5/6. Exact normalized name; name+state disambiguation.
        if norm_name and norm_name in name_partners:
            pid = name_partners[norm_name]
            if state:
                partner = self.env['res.partner'].browse(pid)
                pst = utils.normalize_state(
                    partner.state_id.name if partner.state_id else '')
                if pst and pst == state:
                    return 'matched', 'name_state', 95.0, pid, []
                return ('possible_duplicate', 'fuzzy', 70.0, False,
                        [{'model': 'res.partner', 'id': pid,
                          'display': partner.display_name,
                          'confidence': 70.0,
                          'reason': 'Same normalized name, different state'}])
            return 'matched', 'exact_name', 90.0, pid, []
        # 8. Fuzzy candidates -> MANUAL REVIEW only (never auto-overwrite).
        candidates = []
        if norm_name:
            close = [n for n in name_partners if n and n[:4] == norm_name[:4]]
            scored = [(utils.name_similarity(norm_name, n), n) for n in close]
            scored = sorted([s for s in scored if s[0] >= 75.0], reverse=True)[:3]
            for score, cand_name in scored:
                pid = name_partners[cand_name]
                partner = self.env['res.partner'].browse(pid)
                candidates.append({
                    'model': 'res.partner', 'id': pid,
                    'display': partner.display_name,
                    'confidence': score, 'reason': 'Fuzzy name similarity'})
        if candidates:
            return 'possible_duplicate', 'fuzzy', candidates[0]['confidence'], False, candidates
        return 'new', 'none', 0.0, False, []

    # ------------------------------------------------------------------
    # Step 4: VALIDATE
    # ------------------------------------------------------------------
    def action_validate(self):
        for batch in self:
            errors = warnings = 0
            for rec in batch.record_ids.filtered(
                    lambda r: r.classification in _PARTNER_CLASSIFICATIONS):
                payload = rec.get_payload_dict()
                bits = utils.extract_address_bits(payload)
                errs, warns = [], []
                if not (rec.tally_name or '').strip():
                    errs.append('Missing Tally name.')
                gstin = utils.normalize_gstin(bits.get('gstin'))
                if gstin and not utils.is_valid_gstin(gstin):
                    errs.append('Invalid GSTIN: %s' % bits.get('gstin'))
                if not gstin:
                    warns.append('Missing GSTIN.')
                if not bits.get('state'):
                    warns.append('Missing state.')
                if not rec.tally_guid:
                    warns.append('Missing Tally GUID (idempotency falls back to GSTIN/name).')
                if rec.record_type not in ('Ledger',):
                    warns.append('Record type %s staged for traceability.' % rec.record_type)
                rec.write({
                    'validation_status': 'error' if errs else ('warning' if warns else 'valid'),
                    'error_message': '\n'.join(errs) or False,
                    'warning_message': '\n'.join(warns) or False,
                })
                if errs:
                    errors += 1
                elif warns:
                    warnings += 1
            for rec in batch.record_ids.filtered(
                    lambda r: r.classification in ('product', 'uom')):
                errs, warns = [], []
                if not (rec.tally_name or '').strip():
                    errs.append('Missing Tally name.')
                if not rec.tally_guid:
                    warns.append('Missing Tally GUID (idempotency falls back to name).')
                if rec.classification == 'product':
                    payload = rec.get_payload_dict()
                    bits = utils.extract_stock_bits(payload)
                    if bits.get('is_deleted'):
                        warns.append('Marked deleted in Tally — will be skipped, not created.')
                    if not bits.get('hsn'):
                        warns.append('Missing HSN code.')
                    if bits.get('gst_rate') is None:
                        warns.append('Missing GST rate (stored as info only; no taxes auto-created).')
                    if not bits.get('base_uom'):
                        warns.append('Missing base UOM (product falls back to default unit).')
                rec.write({
                    'validation_status': 'error' if errs else ('warning' if warns else 'valid'),
                    'error_message': '\n'.join(errs) or False,
                    'warning_message': '\n'.join(warns) or False,
                })
                if errs:
                    errors += 1
                elif warns:
                    warnings += 1
            for rec in batch.record_ids.filtered(
                    lambda r: r.classification == 'tax'):
                errs, warns = [], []
                if not (rec.tally_name or '').strip():
                    errs.append('Missing Tally name.')
                if not rec.tally_guid:
                    warns.append('Missing Tally GUID (idempotency falls back to name).')
                kind = self._tally_tax_kind(rec.tally_name)
                if kind in ('RCM', 'OTHER'):
                    warns.append('Not an invoice tax (%s) — will be parked as trace.' % kind)
                rec.write({
                    'validation_status': 'error' if errs else ('warning' if warns else 'valid'),
                    'error_message': '\n'.join(errs) or False,
                    'warning_message': '\n'.join(warns) or False,
                })
                if errs:
                    errors += 1
                elif warns:
                    warnings += 1
            batch._log('Validation: %d errors, %d warnings.' % (errors, warnings))
        return True

    def get_preview(self):
        """Dynamic import preview numbers (never hard-coded)."""
        self.ensure_one()
        Rec = self.env['tally.import.record']
        preview = {'total': len(self.record_ids), 'customers': {}, 'vendors': {},
                   'products': {}, 'uoms': {},
                   'accounting': {}, 'groups': {}, 'currencies': {}}
        for cls, key in (('customer', 'customers'), ('vendor', 'vendors'),
                         ('product', 'products'), ('uom', 'uoms'),
                         ('tax', 'taxes')):
            domain = [('batch_id', '=', self.id), ('classification', '=', cls)]
            preview[key] = {
                'new': Rec.search_count(domain + [('match_status', '=', 'new')]),
                'existing': Rec.search_count(domain + [('match_status', '=', 'matched')]),
                'review': Rec.search_count(domain + [('match_status', 'in',
                                                      ('possible_duplicate', 'manual_review'))]),
                'errors': Rec.search_count(domain + [('validation_status', '=', 'error')]),
            }
        acc = Rec.search_count([('batch_id', '=', self.id),
                                ('classification', 'in', list(_ACCOUNTING_CLASSIFICATIONS))])
        preview['accounting'] = {'parked': acc}
        preview['groups'] = {
            'mapped': Rec.search_count([('batch_id', '=', self.id),
                                        ('record_type', '=', 'Group')]),
            'unmapped': 0,
        }
        preview['currencies'] = {
            'staged': Rec.search_count([('batch_id', '=', self.id),
                                        ('record_type', '=', 'Currency')]),
        }
        return preview

    # ------------------------------------------------------------------
    # Step 5: DRY RUN — analysis only, no business data touched
    # ------------------------------------------------------------------
    def action_dry_run(self):
        for batch in self:
            batch.action_validate()
            batch.record_ids.filtered(
                lambda r: r.import_status == 'pending').write({'import_status': 'dry_run'})
            batch._log('DRY RUN complete: no partners/accounts/taxes/products created. '
                       'Preview: %s' % json.dumps(batch.get_preview(), default=str))
        return True

    # ------------------------------------------------------------------
    # Step 6: IMPORT — idempotent, per-record isolation, retryable
    # ------------------------------------------------------------------
    def action_import(self):
        for batch in self:
            if batch.dry_run:
                raise UserError(_('Batch is in Dry-Run mode: uncheck Dry Run to import.'))
            if batch.state not in ('ready', 'matching', 'failed', 'importing'):
                raise UserError(_('Run Analyse → Classify → Match first (state is %s).', batch.state))
            batch.write({'state': 'importing'})
            created = updated = skipped = failed = 0
            targets = batch.record_ids.filtered(
                lambda r: r.classification in _PARTNER_CLASSIFICATIONS + ('product', 'uom', 'tax')
                and r.validation_status != 'error'
                and r.match_status not in ('skipped',)
                and r.import_status in ('pending', 'dry_run', 'failed'))
            # UOMs first (products resolve their base UOM via fresh mappings),
            # then products, then partners and taxes.
            ordered = targets.sorted(
                key=lambda r: {'uom': 0, 'product': 1}.get(r.classification, 2))
            for rec in ordered:
                try:
                    # Fresh savepoint per record: one bad row never kills the batch.
                    if rec.classification == 'uom':
                        result = batch._import_uom(rec)
                    elif rec.classification == 'product':
                        result = batch._import_product(rec)
                    elif rec.classification == 'tax':
                        result = batch._import_tax(rec)
                    else:
                        result = batch._import_one(rec)
                    if result == 'created':
                        created += 1
                    elif result == 'updated':
                        updated += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    _logger.exception('Master import failed for %s', rec.tally_name)
                    rec.write({'import_status': 'failed',
                               'error_message': '%s: %s' % (type(exc).__name__, exc)})
                    failed += 1
            review = batch.record_ids.search_count([
                ('batch_id', '=', batch.id), ('match_status', 'in',
                                              ('possible_duplicate', 'manual_review'))])
            batch.write({
                'created_count': (batch.created_count or 0) + created,
                'updated_count': (batch.updated_count or 0) + updated,
                'skipped_count': (batch.skipped_count or 0) + skipped,
                'error_count': failed,
                'review_count': review,
                'state': 'completed' if not failed else 'failed',
                'completed_at': fields.Datetime.now(),
            })
            batch._log('IMPORT finished: created=%d updated=%d skipped=%d failed=%d review=%d.' % (
                created, updated, skipped, failed, review))
            batch._write_reconciliation()
        return True

    def _import_one(self, rec):
        """Create/update a single partner + canonical mapping. Returns status."""
        Partner = self.env['res.partner']
        Mapping = self.env['tally.mapping']
        company = self.company_id
        payload = rec.get_payload_dict()
        bits = utils.extract_address_bits(payload)

        # Re-resolve by GUID first (idempotency across repeated imports).
        partner = None
        guid = (rec.tally_guid or '').strip()
        if guid:
            partner = Partner.search(
                [('tally_guid', '=', guid)], limit=1) or None
            if not partner:
                mapping = Mapping.search([('tally_guid', '=', guid)], limit=1)
                if mapping and mapping.mapping_type == 'customer' and mapping.odoo_partner_id:
                    partner = mapping.odoo_partner_id
        if not partner and rec.odoo_model == 'res.partner' and rec.odoo_record_id:
            partner = Partner.browse(rec.odoo_record_id).exists() or None
        # GSTIN fallback (normalized exact).
        gstin = utils.normalize_gstin(bits.get('gstin'))
        if not partner and gstin and utils.is_valid_gstin(gstin):
            partner = Partner.search([('vat', '=', gstin)], limit=1) or None

        vals = self._prepare_partner_vals(rec, bits)
        is_customer = rec.classification == 'customer'
        vals.update({
            'tally_guid': guid or False,
            'tally_alter_id': (rec.tally_alter_id or '') or False,
            'tally_parent': rec.tally_parent or False,
            'tally_classification': rec.classification,
            'tally_last_batch_id': self.id,
        })
        if is_customer:
            vals['customer_rank'] = max(vals.get('customer_rank', 0) or 0,
                                        (partner.customer_rank if partner else 0), 1)
        else:
            vals['supplier_rank'] = max(vals.get('supplier_rank', 0) or 0,
                                        (partner.supplier_rank if partner else 0), 1)

        if partner:
            # Update only safe fields; never blank out existing Odoo data.
            safe = {k: v for k, v in vals.items()
                    if v not in (False, None, '') or k.startswith('tally_')}
            partner.write(safe)
            # Merge customer+vendor ranks (same partner both sides, no duplicate).
            if is_customer and partner.supplier_rank:
                pass
            rec.write({'import_status': 'updated',
                       'odoo_model': 'res.partner', 'odoo_record_id': partner.id})
            self._ensure_mapping(rec, partner)
            return 'updated'
        # Manual-review rows are never auto-created.
        if rec.match_status in ('possible_duplicate', 'manual_review'):
            rec.write({'import_status': 'review'})
            return 'skipped'
        partner = Partner.create(vals)
        rec.write({'import_status': 'imported',
                   'odoo_model': 'res.partner', 'odoo_record_id': partner.id,
                   'match_status': 'matched',
                   'match_method': rec.match_method if rec.match_method != 'none' else 'none'})
        self._ensure_mapping(rec, partner)
        return 'created'

    def _prepare_partner_vals(self, rec, bits):
        """Map Tally ledger fields -> res.partner (only existing fields)."""
        Partner = self.env['res.partner']
        vals = {'name': (rec.tally_name or 'Unknown Tally Ledger').strip()}
        gstin = utils.normalize_gstin(bits.get('gstin'))
        if gstin and utils.is_valid_gstin(gstin):
            vals['vat'] = gstin
        if bits.get('street') and 'street' in Partner._fields:
            vals['street'] = bits['street'][:500]
        if bits.get('pin') and 'zip' in Partner._fields:
            vals['zip'] = utils.normalize_pin(bits['pin'])[:16]
        if bits.get('email') and 'email' in Partner._fields:
            email = utils.normalize_email(bits['email'])
            if '@' in email:
                vals['email'] = email
        if bits.get('phone') and 'phone' in Partner._fields:
            vals['phone'] = bits['phone'][:64]
        if bits.get('mobile') and 'mobile' in Partner._fields:
            vals['mobile'] = bits['mobile'][:64]
        if bits.get('website') and 'website' in Partner._fields:
            vals['website'] = bits['website'][:256]
        # State / country resolution (name lookup, India-first fallback).
        if bits.get('state'):
            state = self._resolve_state(bits['state'])
            if state:
                vals['state_id'] = state.id
                if state.country_id:
                    vals['country_id'] = state.country_id.id
        if bits.get('country') and 'country_id' not in vals:
            country = self.env['res.country'].search(
                [('name', '=ilike', bits['country'].strip())], limit=1)
            if country:
                vals['country_id'] = country.id
        if 'country_id' not in vals:
            india = self.env.ref('base.in', raise_if_not_found=False)
            if india:
                vals['country_id'] = india.id
        if rec.classification == 'customer':
            vals['customer_rank'] = 1
        else:
            vals['supplier_rank'] = 1
        if 'company_id' in Partner._fields and self.company_id:
            vals['company_id'] = False  # partners stay shared; identity via mapping
        return vals

    def _resolve_state(self, state_name):
        State = self.env['res.country.state']
        name = (state_name or '').strip()
        if not name:
            return None
        state = State.search([('name', '=ilike', name)], limit=1)
        if not state:
            state = State.search([('name', '=ilike', name.split()[0])], limit=1)
        if not state:
            india = self.env.ref('base.in', raise_if_not_found=False)
            if india:
                state = State.search(
                    [('country_id', '=', india.id), ('name', '=ilike', '%%{}%%'.format(name[:6]))],
                    limit=1)
        return state or None

    def _import_uom(self, rec):
        """Create/adopt a single UOM + canonical mapping. Returns status."""
        Uom = self.env['uom.uom']
        Mapping = self.env['tally.mapping']
        company = self.company_id
        guid = (rec.tally_guid or '').strip()
        norm_name = utils.normalize_name(rec.tally_name)

        uom = None
        if guid:
            mapping = Mapping.search([
                ('mapping_type', '=', 'uom'), ('tally_guid', '=', guid)], limit=1)
            if mapping and mapping.odoo_uom_id:
                uom = mapping.odoo_uom_id
        if not uom and norm_name:
            # Adopt a pre-existing invoice-sync mapping for this Tally name.
            mapping = Mapping.search([
                ('mapping_type', '=', 'uom'),
                ('company_id', '=', company.id)], limit=100)
            for cand in mapping:
                if utils.normalize_name(cand.tally_name) == norm_name and cand.odoo_uom_id:
                    uom = cand.odoo_uom_id
                    break
        if not uom and norm_name:
            # Adopt an identically named Odoo UOM instead of duplicating.
            for cand in Uom.search([]):
                if utils.normalize_name(cand.name) == norm_name:
                    uom = cand
                    break
        if not uom and rec.odoo_model == 'uom.uom' and rec.odoo_record_id:
            uom = Uom.browse(rec.odoo_record_id).exists() or None

        if uom:
            rec.write({'import_status': 'updated',
                       'odoo_model': 'uom.uom', 'odoo_record_id': uom.id})
            self._ensure_uom_mapping(rec, uom)
            return 'updated'
        if rec.match_status in ('possible_duplicate', 'manual_review'):
            rec.write({'import_status': 'review'})
            return 'skipped'
        # Odoo 19 UOMs are standalone references (no categories): one row
        # per Tally unit, no conversion guessing.
        uom = Uom.create({
            'name': (rec.tally_name or 'Unknown Unit').strip(),
            'relative_factor': 1.0,
        })
        rec.write({'import_status': 'imported', 'match_status': 'matched',
                   'odoo_model': 'uom.uom', 'odoo_record_id': uom.id})
        self._ensure_uom_mapping(rec, uom)
        return 'created'

    def _ensure_uom_mapping(self, rec, uom):
        Mapping = self.env['tally.mapping']
        company = self.company_id
        vals = {
            'mapping_type': 'uom',
            'odoo_uom_id': uom.id,
            'tally_name': rec.tally_name or uom.name,
            'tally_guid': rec.tally_guid or False,
            'tally_alter_id': rec.tally_alter_id or False,
            'tally_entity_type': 'unit',
            'company_id': company.id,
            'match_method': rec.match_method if rec.match_method != 'none' else 'guid',
            'confidence': rec.match_confidence or 100.0,
            'source_batch_id': self.id,
        }
        existing = Mapping.search([
            ('mapping_type', '=', 'uom'), ('odoo_uom_id', '=', uom.id),
            ('company_id', '=', company.id)], limit=1)
        if existing:
            existing.write(vals)
        elif rec.tally_guid:
            # Reuse only an unbound GUID row — never reassign another record's mapping.
            by_guid = Mapping.search([
                ('mapping_type', '=', 'uom'), ('tally_guid', '=', rec.tally_guid),
                ('odoo_uom_id', '=', False)], limit=1)
            if by_guid:
                by_guid.write(vals)
            else:
                Mapping.create(vals)
        else:
            Mapping.create(vals)

    def _import_product(self, rec):
        """Create/adopt a single product + canonical mapping. Returns status."""
        Template = self.env['product.template']
        Mapping = self.env['tally.mapping']
        company = self.company_id
        payload = rec.get_payload_dict()
        bits = utils.extract_stock_bits(payload)
        guid = (rec.tally_guid or '').strip()

        if bits.get('is_deleted'):
            rec.write({'import_status': 'skipped',
                       'warning_message': 'Marked deleted in Tally — skipped.'})
            return 'skipped'

        # Re-resolve by GUID first (idempotency across repeated imports).
        variant = None
        if guid:
            mapping = Mapping.search([
                ('mapping_type', '=', 'product'), ('tally_guid', '=', guid)], limit=1)
            if mapping and mapping.odoo_product_id:
                variant = mapping.odoo_product_id
        if not variant and rec.odoo_model == 'product.product' and rec.odoo_record_id:
            variant = self.env['product.product'].browse(rec.odoo_record_id).exists() or None
        if not variant and rec.tally_name:
            # Adopt an identically named template instead of duplicating.
            norm = utils.normalize_name(rec.tally_name)
            for cand in Template.search([('name', '=ilike', rec.tally_name.strip())]):
                if utils.normalize_name(cand.name) == norm and cand.product_variant_id:
                    variant = cand.product_variant_id
                    break

        uom = self._resolve_product_uom(bits.get('base_uom'))
        categ = self._resolve_product_category(rec.tally_parent)
        vals = {
            'name': (rec.tally_name or 'Unknown Stock Item').strip(),
            'type': 'consu',
            'categ_id': categ.id,
            # Odoo 19 keeps a single UOM per product (uom_po_id was removed).
            'uom_id': uom.id,
            'sale_ok': True,
            'purchase_ok': True,
        }
        if bits.get('hsn') and 'l10n_in_hsn_code' in Template._fields:
            vals['l10n_in_hsn_code'] = bits['hsn'][:10]

        if variant:
            template = variant.product_tmpl_id
            # Update refreshes name/category/HSN but never disturbs the
            # configured UOMs or other Odoo-side product data.
            safe = {'name': vals['name'], 'categ_id': vals['categ_id']}
            if ('l10n_in_hsn_code' in vals
                    and 'l10n_in_hsn_code' in Template._fields
                    and not template.l10n_in_hsn_code):
                safe['l10n_in_hsn_code'] = vals['l10n_in_hsn_code']
            template.write(safe)
            rec.write({'import_status': 'updated',
                       'odoo_model': 'product.product', 'odoo_record_id': variant.id})
            self._ensure_product_mapping(rec, variant, bits)
            return 'updated'
        if rec.match_status in ('possible_duplicate', 'manual_review'):
            rec.write({'import_status': 'review'})
            return 'skipped'
        template = Template.create(vals)
        variant = template.product_variant_id
        rec.write({'import_status': 'imported', 'match_status': 'matched',
                   'odoo_model': 'product.product', 'odoo_record_id': variant.id})
        self._ensure_product_mapping(rec, variant, bits)
        return 'created'

    def _resolve_product_uom(self, base_uom_name):
        """Resolve a Tally base unit to uom.uom (mapping → name → default)."""
        Uom = self.env['uom.uom']
        norm = utils.normalize_name(base_uom_name or '')
        if norm:
            mapping = self.env['tally.mapping'].search(
                [('mapping_type', '=', 'uom'),
                 ('company_id', '=', self.company_id.id)], limit=200)
            for cand in mapping:
                if utils.normalize_name(cand.tally_name) == norm and cand.odoo_uom_id:
                    return cand.odoo_uom_id
            for cand in Uom.search([]):
                if utils.normalize_name(cand.name) == norm:
                    return cand
        fallback = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        return fallback or Uom.search([], limit=1)

    def _resolve_product_category(self, stock_group):
        """Get-or-create product.category from a Tally stock group name."""
        Category = self.env['product.category']
        name = (stock_group or '').strip()
        if not name:
            default = self.env.ref('product.product_category_all', raise_if_not_found=False)
            if default:
                return default
            root = Category.search([('parent_id', '=', False)], order='id', limit=1)
            if root:
                return root
            return Category.create({'name': 'Tally Import'})
        existing = Category.search([('name', '=ilike', name)], limit=1)
        if existing:
            return existing
        return Category.create({'name': name})

    def _ensure_product_mapping(self, rec, variant, bits):
        Mapping = self.env['tally.mapping']
        company = self.company_id
        notes = []
        if bits.get('hsn'):
            notes.append('HSN: %s' % bits['hsn'])
        if bits.get('gst_rate') is not None:
            notes.append('Tally GST rate: %s%% (%s; taxes NOT auto-created)' % (
                bits['gst_rate'], bits.get('taxability') or 'n/a'))
        if bits.get('base_uom'):
            notes.append('Tally base UOM: %s' % bits['base_uom'])
        vals = {
            'mapping_type': 'product',
            'odoo_product_id': variant.id,
            'tally_name': rec.tally_name or variant.display_name,
            'tally_guid': rec.tally_guid or False,
            'tally_alter_id': rec.tally_alter_id or False,
            'tally_entity_type': 'stock_item',
            'company_id': company.id,
            'match_method': rec.match_method if rec.match_method != 'none' else 'guid',
            'confidence': rec.match_confidence or 100.0,
            'source_batch_id': self.id,
            'notes': '\n'.join(notes),
        }
        existing = Mapping.search([
            ('mapping_type', '=', 'product'), ('odoo_product_id', '=', variant.id),
            ('company_id', '=', company.id)], limit=1)
        if existing:
            existing.write(vals)
        elif rec.tally_guid:
            # Reuse only an unbound GUID row — never reassign another record's mapping.
            by_guid = Mapping.search([
                ('mapping_type', '=', 'product'), ('tally_guid', '=', rec.tally_guid),
                ('odoo_product_id', '=', False)], limit=1)
            if by_guid:
                by_guid.write(vals)
            else:
                Mapping.create(vals)
        else:
            Mapping.create(vals)

    def _import_tax(self, rec):
        """Wire a Tally duty ledger to an Odoo sale tax via mapping.

        Odoo taxes are adopted, never created (Tally ledgers carry no rate).
        Non-invoice ledgers (TDS/TCS/RCM/payroll) are parked as trace.
        Returns created/updated/skipped status.
        """
        Mapping = self.env['tally.mapping']
        company = self.company_id
        guid = (rec.tally_guid or '').strip()
        kind = self._tally_tax_kind(rec.tally_name)

        if kind in ('RCM', 'OTHER'):
            rec.write({
                'import_status': 'skipped',
                'warning_message': 'Not an invoice tax (%s ledger) — parked as trace, '
                                   'no mapping created.' % kind,
            })
            return 'skipped'

        tax = None
        if guid:
            mapping = Mapping.search([
                ('mapping_type', '=', 'tax'), ('tally_guid', '=', guid)], limit=1)
            if mapping and mapping.odoo_tax_id:
                tax = mapping.odoo_tax_id
        if not tax and rec.odoo_model == 'account.tax' and rec.odoo_record_id:
            tax = self.env['account.tax'].browse(rec.odoo_record_id).exists() or None

        if tax:
            created = self._ensure_tax_mapping(rec, tax, kind)
            rec.write({'import_status': 'imported' if created else 'updated',
                       'odoo_model': 'account.tax', 'odoo_record_id': tax.id})
            return 'imported' if created else 'updated'
        if rec.match_status in ('possible_duplicate', 'manual_review'):
            rec.write({'import_status': 'review'})
            return 'skipped'
        # Deterministic dead-end, surfaced — never a fake tax.
        rec.write({
            'import_status': 'review',
            'warning_message': 'No unmapped Odoo sale tax of kind %s. Create the tax, '
                               'add its Entity Mapping (paste Tally GUID %s), then Match again.'
                               % (kind, guid or '—'),
        })
        return 'skipped'

    def _ensure_tax_mapping(self, rec, tax, kind):
        """Create/update the canonical tax mapping row. Returns True if created."""
        Mapping = self.env['tally.mapping']
        company = self.company_id
        vals = {
            'mapping_type': 'tax',
            'odoo_tax_id': tax.id,
            'tally_name': rec.tally_name or tax.name,
            'tally_guid': rec.tally_guid or False,
            'tally_alter_id': rec.tally_alter_id or False,
            'tally_entity_type': 'ledger',
            'company_id': company.id,
            # 'auto_kind' exists only on staging rows; canonical rows record 'import'.
            'match_method': ('import' if rec.match_method in ('none', 'auto_kind')
                             else rec.match_method),
            'confidence': rec.match_confidence or 100.0,
            'source_batch_id': self.id,
            'notes': 'Tally %s duty ledger wired by master import.' % kind,
        }
        existing = Mapping.search([
            ('mapping_type', '=', 'tax'), ('odoo_tax_id', '=', tax.id),
            ('company_id', '=', company.id)], limit=1)
        if existing:
            existing.write(vals)
            return False
        if rec.tally_guid:
            # Reuse only an unbound GUID row — never reassign another tax's mapping.
            by_guid = Mapping.search([
                ('mapping_type', '=', 'tax'), ('tally_guid', '=', rec.tally_guid),
                ('odoo_tax_id', '=', False)], limit=1)
            if by_guid:
                by_guid.write(vals)
                return False
        Mapping.create(vals)
        return True

    def _ensure_mapping(self, rec, partner):
        """Create/update the canonical tally.mapping row (GUID-carrying)."""
        Mapping = self.env['tally.mapping']
        company = self.company_id
        domain = [('mapping_type', '=', 'customer'),
                  ('odoo_partner_id', '=', partner.id),
                  ('company_id', '=', company.id)]
        existing = Mapping.search(domain, limit=1)
        vals = {
            'mapping_type': 'customer',
            'odoo_partner_id': partner.id,
            'tally_name': rec.tally_name or partner.name,
            'tally_guid': rec.tally_guid or False,
            'tally_alter_id': rec.tally_alter_id or False,
            'company_id': company.id,
            'match_method': rec.match_method if rec.match_method != 'none' else 'guid',
            'confidence': rec.match_confidence or 100.0,
            'source_batch_id': self.id,
        }
        if existing:
            # Never create a duplicate mapping; refresh GUID/audit fields.
            existing.write(vals)
        else:
            # GUID-level reuse, scoped to customer rows with no partner bound yet —
            # never reassign another record's mapping.
            by_guid = Mapping.search(
                [('mapping_type', '=', 'customer'),
                 ('tally_guid', '=', rec.tally_guid),
                 ('odoo_partner_id', '=', False)] if rec.tally_guid else [(0, '=', 1)],
                limit=1)
            if by_guid:
                by_guid.write(vals)
            else:
                Mapping.create(vals)

    # ------------------------------------------------------------------
    # Retry / reconcile / verify
    # ------------------------------------------------------------------
    def action_retry_failed(self):
        for batch in self:
            failed = batch.record_ids.filtered(lambda r: r.import_status == 'failed')
            failed.write({'import_status': 'pending', 'error_message': False,
                          'validation_status': 'pending'})
            batch.write({'state': 'ready'})
            batch._log('Retry armed for %d failed records.' % len(failed))
        return True

    def action_cancel(self):
        self.write({'state': 'cancelled'})
        return True

    def _write_reconciliation(self):
        self.ensure_one()
        stats = self.get_reconciliation()
        self._log('RECONCILE: %s' % json.dumps(stats, default=str))

    def get_reconciliation(self):
        """Tally source vs Odoo mapped/unmapped/excluded/error counts."""
        self.ensure_one()
        Rec = self.env['tally.import.record']
        base = [('batch_id', '=', self.id)]
        stats = {}
        for cls in ('customer', 'vendor', 'product', 'uom', 'tax'):
            total = Rec.search_count(base + [('classification', '=', cls)])
            mapped = Rec.search_count(base + [('classification', '=', cls),
                                              ('odoo_record_id', '!=', False)])
            imported = Rec.search_count(base + [('classification', '=', cls),
                                                ('import_status', 'in', ('imported', 'updated'))])
            errors = Rec.search_count(base + [('classification', '=', cls),
                                              ('import_status', '=', 'failed')])
            review = Rec.search_count(base + [('classification', '=', cls),
                                              ('import_status', '=', 'review')])
            stats[cls] = {'tally_source': total, 'odoo_mapped': mapped,
                          'imported': imported, 'unmapped': total - mapped,
                          'errors': errors, 'review': review}
        for label, rtype in (('groups', 'Group'), ('currencies', 'Currency')):
            total = Rec.search_count(base + [('record_type', '=', rtype)])
            stats[label] = {'tally_source': total, 'odoo_mapped': 0,
                            'unmapped': total, 'excluded': total, 'errors': 0}
        stats['product_note'] = ('No Stock Item records in source.' if not self.has_stock_items
                                 else '%d Stock Item rows importable into products.'
                                 % self.stock_item_count)
        return stats

    def action_verify_mappings(self):
        """Re-resolve every imported GUID against live Odoo records."""
        for batch in self:
            ok = missing = 0
            for rec in batch.record_ids.filtered(lambda r: r.odoo_model and r.odoo_record_id):
                try:
                    target = self.env[rec.odoo_model].browse(rec.odoo_record_id)
                    exists = bool(target.exists())
                except KeyError:
                    exists = False
                if exists:
                    ok += 1
                else:
                    missing += 1
                    rec.write({'error_message': 'Linked %s no longer exists.' % rec.odoo_model})
            batch._log('VERIFY: %d mappings OK, %d dangling.' % (ok, missing))
        return True

    # ------------------------------------------------------------------
    # Wizard presentation helper
    # ------------------------------------------------------------------
    def _wizard_summary_html(self, stage):
        self.ensure_one()
        rows = [
            ('File', self.source_filename or ''),
            ('Encoding', self.encoding_used or ''),
            ('State', self.state),
            ('Total records', self.total_records),
            ('Ledgers / Groups / Currencies / Stock Items',
             '%d / %d / %d / %d' % (self.ledger_count, self.group_count,
                                    self.currency_count, self.stock_item_count)),
            ('Customers / Vendors / Accounting',
             '%d / %d / %d' % (self.customer_count, self.vendor_count, self.account_count)),
            ('New / Matched / Review / Errors',
             '%d / %d / %d / %d' % (self.new_count, self.matched_count,
                                    self.review_count, self.error_count)),
            ('Created / Updated / Skipped',
             '%d / %d / %d' % (self.created_count, self.updated_count, self.skipped_count)),
        ]
        if not self.has_stock_items and stage in ('analyse', 'classify', 'match',
                                                  'validate', 'done'):
            rows.append(('Products',
                         'No Stock Item records in this file — product import unavailable.'))
        html = ['<table class="table table-sm"><tbody>']
        for label, value in rows:
            html.append('<tr><th>%s</th><td>%s</td></tr>' % (label, value))
        html.append('</tbody></table>')
        return ''.join(html)
