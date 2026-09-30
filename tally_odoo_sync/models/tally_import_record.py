# -*- coding: utf-8 -*-
"""Staging records: one row per Tally source entity, raw payload preserved."""
import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class TallyImportRecord(models.Model):
    _name = 'tally.import.record'
    _description = 'Tally Import Staging Record'
    _order = 'batch_id, record_type, tally_name'
    _rec_name = 'tally_name'

    batch_id = fields.Many2one(
        'tally.import.batch', string='Import Batch', required=True,
        index=True, ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='batch_id.company_id', store=True, index=True,
    )
    source_index = fields.Integer(string='Source #', index=True)

    # -- Source identity ----------------------------------------------------
    record_type = fields.Selection(
        selection=[
            ('Ledger', 'Ledger'),
            ('Group', 'Group'),
            ('Currency', 'Currency'),
            ('Stock Item', 'Stock Item'),
            ('Stock Group', 'Stock Group'),
            ('Unit', 'Unit'),
            ('Godown', 'Godown'),
            ('Voucher', 'Voucher'),
            ('Cost Centre', 'Cost Centre'),
            ('Cost Category', 'Cost Category'),
            ('Employee', 'Employee'),
            ('Unknown', 'Unknown'),
            ('Other', 'Other'),
        ],
        string='Record Type', required=True, index=True, default='Ledger',
    )
    tally_name = fields.Char(string='Tally Name', index=True)
    tally_guid = fields.Char(string='Tally GUID', index=True)
    tally_alter_id = fields.Char(string='Tally Alter ID', index=True)
    tally_parent = fields.Char(string='Tally Parent/Group', index=True)

    # -- Classification ------------------------------------------------------
    classification = fields.Selection(
        selection=[
            ('customer', 'Customer'),
            ('vendor', 'Vendor'),
            ('bank', 'Bank'),
            ('cash', 'Cash'),
            ('tax', 'Tax'),
            ('income', 'Income'),
            ('expense', 'Expense'),
            ('asset', 'Asset'),
            ('liability', 'Liability'),
            ('equity', 'Equity'),
            ('accounting', 'Accounting'),
            ('currency', 'Currency'),
            ('group', 'Group'),
            ('product', 'Product'),
            ('uom', 'UoM'),
            ('unsupported', 'Unsupported'),
            ('other', 'Other'),
            ('unclassified', 'Unclassified'),
        ],
        string='Classification', index=True, default='unclassified',
    )

    # -- Matching -------------------------------------------------------------
    odoo_model = fields.Char(string='Odoo Model', index=True)
    odoo_record_id = fields.Integer(string='Odoo Record ID', index=True)
    odoo_record_ref = fields.Char(
        compute='_compute_odoo_record_ref', store=False,
        string='Odoo Record',
    )
    match_status = fields.Selection(
        selection=[
            ('new', 'New'),
            ('matched', 'Matched'),
            ('possible_duplicate', 'Possible Duplicate'),
            ('manual_review', 'Manual Review'),
            ('mapping_required', 'Mapping Required'),
            ('skipped', 'Skipped'),
        ],
        string='Match Status', index=True, default='new',
    )
    match_method = fields.Selection(
        selection=[
            ('none', 'None'),
            ('guid', 'Tally GUID'),
            ('mapping', 'Existing Mapping'),
            ('gstin', 'GSTIN'),
            ('stable_id', 'Stable Identifier'),
            ('exact_name', 'Exact Name'),
            ('name_state', 'Name + State'),
            ('name_gstin', 'Name + GSTIN'),
            ('fuzzy', 'Fuzzy Candidate'),
            ('auto_kind', 'Auto: GST Kind'),
        ],
        string='Match Method', default='none',
    )
    match_confidence = fields.Float(string='Confidence %', default=0.0)
    candidate_json = fields.Text(
        string='Duplicate Candidates (JSON)',
        help="JSON list of {model, id, display, confidence, reason}.",
    )

    # -- Validation / import ---------------------------------------------------
    validation_status = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('valid', 'Valid'),
            ('warning', 'Warning'),
            ('error', 'Error'),
        ],
        string='Validation', index=True, default='pending',
    )
    import_status = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('dry_run', 'Dry-Run Only'),
            ('imported', 'Imported'),
            ('updated', 'Updated'),
            ('skipped', 'Skipped'),
            ('failed', 'Failed'),
            ('review', 'Needs Review'),
        ],
        string='Import Status', index=True, default='pending',
    )
    error_message = fields.Text(string='Error')
    warning_message = fields.Text(string='Warning')

    # -- Audit ------------------------------------------------------------------
    raw_payload = fields.Text(
        string='Raw Tally Payload (JSON)', readonly=True,
        help="Original Tally JSON for this entity (audit / re-import).",
    )
    opening_balance = fields.Char(string='Opening Balance (staged)')
    created_at = fields.Datetime(string='Staged At', default=fields.Datetime.now)

    _batch_guid_uniq = models.Constraint(
        'UNIQUE(batch_id, tally_guid)',
        'Duplicate Tally GUID inside the same batch.',
    )

    @api.depends('odoo_model', 'odoo_record_id')
    def _compute_odoo_record_ref(self):
        for rec in self:
            if rec.odoo_model and rec.odoo_record_id:
                try:
                    target = self.env[rec.odoo_model].browse(rec.odoo_record_id)
                    rec.odoo_record_ref = '%s #%d (%s)' % (
                        rec.odoo_model, rec.odoo_record_id,
                        target.exists().display_name if hasattr(target, 'display_name') else '')
                except Exception:
                    rec.odoo_record_ref = '%s #%s' % (rec.odoo_model, rec.odoo_record_id)
            else:
                rec.odoo_record_ref = ''

    def get_payload_dict(self):
        try:
            return json.loads(self.raw_payload or '{}')
        except (TypeError, ValueError):
            return {}

    def action_map_to_partner(self, partner):
        """Manually map this staging record to an existing partner."""
        self.ensure_one()
        Mapping = self.env['tally.mapping']
        company = self.company_id or self.env.company
        existing = Mapping.search([
            ('mapping_type', 'in', ('customer',)),
            ('odoo_partner_id', '=', partner.id),
            ('company_id', '=', company.id),
        ], limit=1)
        vals = {
            'mapping_type': 'customer',
            'odoo_partner_id': partner.id,
            'tally_name': self.tally_name or partner.name,
            'tally_guid': self.tally_guid or False,
            'tally_alter_id': self.tally_alter_id or False,
            'company_id': company.id,
            'match_method': 'manual',
            'confidence': 100.0,
            'source_batch_id': self.batch_id.id,
        }
        if existing:
            existing.write(vals)
        else:
            existing = Mapping.create(vals)
        partner.write({
            'tally_guid': self.tally_guid or partner.tally_guid,
            'tally_alter_id': self.tally_alter_id or partner.tally_alter_id,
            'tally_parent': self.tally_parent or partner.tally_parent,
        })
        self.write({
            'odoo_model': 'res.partner',
            'odoo_record_id': partner.id,
            'match_status': 'matched',
            'match_method': 'mapping',
            'match_confidence': 100.0,
            'import_status': 'pending',
            'error_message': False,
        })
        return True

    def action_skip(self):
        self.write({'import_status': 'skipped', 'match_status': 'skipped'})
        return True

    def action_retry(self):
        """Reset a failed record for retry (idempotent — never duplicates)."""
        self.write({
            'import_status': 'pending',
            'validation_status': 'pending',
            'error_message': False,
        })
        return True
