# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    """Expose the per-company Tally configuration in the settings UI."""

    _inherit = 'res.config.settings'

    tally_sync_enabled = fields.Boolean(
        related='company_id.tally_sync_enabled', readonly=False)
    tally_sync_on_post = fields.Boolean(
        related='company_id.tally_sync_on_post', readonly=False)
    tally_server_url = fields.Char(
        related='company_id.tally_server_url', readonly=False)
    tally_company_name = fields.Char(
        related='company_id.tally_company_name', readonly=False)
    tally_username = fields.Char(
        related='company_id.tally_username', readonly=False)
    tally_password = fields.Char(
        related='company_id.tally_password', readonly=False)
    tally_timeout = fields.Integer(
        related='company_id.tally_timeout', readonly=False)
    tally_verify_ssl = fields.Boolean(
        related='company_id.tally_verify_ssl', readonly=False)
    tally_sales_ledger = fields.Char(
        related='company_id.tally_sales_ledger', readonly=False)
    tally_auto_create_ledgers = fields.Boolean(
        related='company_id.tally_auto_create_ledgers', readonly=False)
    tally_party_ledger_parent = fields.Char(
        related='company_id.tally_party_ledger_parent', readonly=False)
    tally_sales_ledger_parent = fields.Char(
        related='company_id.tally_sales_ledger_parent', readonly=False)
    tally_tax_ledger_parent = fields.Char(
        related='company_id.tally_tax_ledger_parent', readonly=False)
    tally_declaration = fields.Char(
        related='company_id.tally_declaration', readonly=False)
    tally_signatory = fields.Char(
        related='company_id.tally_signatory', readonly=False)
    tally_retry_max_attempts = fields.Integer(
        related='company_id.tally_retry_max_attempts', readonly=False)
    company_cin = fields.Char(related='company_id.company_cin', readonly=False)
    company_pan = fields.Char(related='company_id.company_pan', readonly=False)
    company_lic = fields.Char(related='company_id.company_lic', readonly=False)
    tally_branch = fields.Char(related='company_id.tally_branch', readonly=False)
    tally_sync_mode = fields.Selection(related='company_id.tally_sync_mode', readonly=False)
    tally_connector_type = fields.Selection(related='company_id.tally_connector_type', readonly=False)

    def action_test_tally_connection(self):
        self.ensure_one()
        return self.company_id.action_test_tally_connection()
