# -*- coding: utf-8 -*-
"""User-friendly master-import wizard (upload -> results)."""
import base64
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class TallyImportWizard(models.TransientModel):
    _name = 'tally.import.wizard'
    _description = 'Tally Master Import Wizard'

    step = fields.Selection(
        selection=[
            ('upload', '1. Upload'),
            ('analyse', '2. Analyse'),
            ('classify', '3. Classification'),
            ('match', '4. Matching'),
            ('validate', '5. Validation & Dry Run'),
            ('confirm', '6. Confirm Import'),
            ('done', '7. Results'),
        ],
        default='upload',
    )
    batch_id = fields.Many2one('tally.import.batch', string='Import Batch')
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True,
    )
    source_filename = fields.Char(string='Filename')
    source_file = fields.Binary(string='Master.json File', attachment=False)
    dry_run = fields.Boolean(string='Dry Run Only', default=True)
    summary_html = fields.Html(string='Summary', readonly=True)
    preview_json = fields.Text(string='Preview (JSON)', readonly=True)

    def action_upload_and_analyse(self):
        self.ensure_one()
        if not self.source_file:
            raise UserError(_('Please choose a Master.json file first.'))
        batch = self.env['tally.import.batch'].create({
            'company_id': self.company_id.id,
            'source_filename': self.source_filename or 'Master.json',
            'source_file': self.source_file,
            'dry_run': self.dry_run,
        })
        try:
            batch.action_analyse()
        except UserError:
            raise
        self.write({'batch_id': batch.id, 'step': 'analyse',
                    'summary_html': batch._wizard_summary_html('analyse')})
        return self._reopen()

    def action_classify(self):
        self.ensure_one()
        self.batch_id.action_classify()
        self.write({'step': 'classify',
                    'summary_html': self.batch_id._wizard_summary_html('classify')})
        return self._reopen()

    def action_match(self):
        self.ensure_one()
        self.batch_id.action_match()
        self.write({'step': 'match',
                    'summary_html': self.batch_id._wizard_summary_html('match'),
                    'preview_json': json.dumps(self.batch_id.get_preview(), indent=2)})
        return self._reopen()

    def action_validate_dry_run(self):
        self.ensure_one()
        self.batch_id.action_dry_run()
        self.write({'step': 'validate',
                    'summary_html': self.batch_id._wizard_summary_html('validate'),
                    'preview_json': json.dumps(self.batch_id.get_preview(), indent=2)})
        return self._reopen()

    def action_confirm_import(self):
        self.ensure_one()
        if self.dry_run:
            raise UserError(_('Uncheck "Dry Run Only" to perform the real import.'))
        self.batch_id.write({'dry_run': False})
        self.batch_id.action_import()
        self.write({'step': 'done',
                    'summary_html': self.batch_id._wizard_summary_html('done'),
                    'preview_json': json.dumps(
                        self.batch_id.get_reconciliation(), indent=2, default=str)})
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }
