# -*- coding: utf-8 -*-
import hashlib
import json
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class TallySyncLog(models.Model):
    """Immutable audit log for every Tally synchronization attempt."""

    _name = 'tally.sync.log'
    _description = 'Tally Sync Audit Log'
    _order = 'create_date desc'
    _rec_name = 'display_name'

    invoice_id = fields.Many2one(
        'account.move', string='Invoice', required=True, index=True,
        ondelete='cascade',
    )
    invoice_number = fields.Char(string='Invoice Number')
    company_id = fields.Many2one('res.company', string='Company', index=True)

    sync_attempt_id = fields.Char(
        string='Attempt ID', index=True,
        help="Unique ID for this synchronization attempt.",
    )
    sync_timestamp = fields.Datetime(
        string='Timestamp', default=fields.Datetime.now, index=True,
    )

    # -- Payload / XML hashes for duplicate detection -------------------------
    request_hash = fields.Char(
        string='Request Hash', index=True,
        help="SHA-256 hash of the invoice data sent to Tally.",
    )
    xml_hash = fields.Char(
        string='XML Hash',
        help="SHA-256 hash of the generated XML.",
    )

    # -- Status ---------------------------------------------------------------
    STATUS_SELECTION = [
        ('draft', 'Draft'),
        ('ready', 'Ready for Tally'),
        ('validating', 'Validating'),
        ('validated', 'Validated'),
        ('building_payload', 'Building Payload'),
        ('sending', 'Sending'),
        ('success', 'Success'),
        ('not_ready', 'Not Ready'),
        ('validation_failed', 'Validation Failed'),
        ('network_error', 'Network Error'),
        ('timeout', 'Timeout'),
        ('sync_unknown', 'Sync Unknown'),
        ('retrying', 'Retrying'),
        ('invalid_xml', 'Invalid XML'),
        ('tally_rejected', 'Tally Rejected'),
        ('missing_master', 'Missing Master'),
        ('duplicate_voucher', 'Duplicate Voucher'),
        ('auth_config_error', 'Auth/Config Error'),
        ('unknown_error', 'Unknown Error'),
    ]
    sync_status = fields.Selection(
        selection=STATUS_SELECTION,
        string='Status', default='draft', index=True, required=True,
    )

    # -- Tally response -------------------------------------------------------
    tally_voucher_id = fields.Char(string='Tally Voucher ID')
    tally_voucher_guid = fields.Char(string='Tally Voucher GUID')
    tally_response_raw = fields.Text(string='Raw Tally Response')

    # -- Error ----------------------------------------------------------------
    error_code = fields.Char(string='Error Code')
    error_message = fields.Text(string='Error Message')

    # -- Audit extensions -----------------------------------------------------
    attempt_number = fields.Integer(string='Attempt Number', readonly=True, default=1)
    duration = fields.Float(string='Duration (s)', readonly=True,
                            help="Sync attempt duration in seconds.")
    source = fields.Selection(
        [('manual', 'Manual'), ('cron', 'Cron'), ('post', 'On Post'), ('retry', 'Retry')],
        string='Source', default='manual')
    correlation_id = fields.Char(string='Correlation ID', index=True,
                                  help="Deterministic GUID for tracing across logs.")
    payload_hash = fields.Char(string='Payload Hash', index=True)

    # -- Payload data (stored as JSON for audit) ------------------------------
    payload_json = fields.Text(
        string='Payload JSON',
        help="Complete TallyInvoicePayload at the time of sync.",
    )
    validation_json = fields.Text(
        string='Validation Result JSON',
        help="Validation result at the time of sync.",
    )

    # -- Display --------------------------------------------------------------
    display_name = fields.Char(
        compute='_compute_display_name', store=True,
    )

    @api.depends('invoice_number', 'sync_status', 'sync_timestamp')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s / %s / %s' % (
                rec.invoice_number or '?',
                rec.sync_status or '?',
                rec.sync_timestamp or '?',
            )

    @api.model
    def create_log(self, invoice, status='draft', **kwargs):
        """Create a new sync log entry for an invoice attempt."""
        import uuid
        attempt_num = self.search_count([('invoice_id', '=', invoice.id)]) + 1
        attempt_id = '%s-%s' % (invoice.id, fields.Datetime.now().strftime('%Y%m%d%H%M%S%f'))
        # Deterministic correlation via GUID
        try:
            corr = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'tally-%s-%s-%s' % (invoice.company_id.id, invoice.id, attempt_num)))
        except Exception:
            corr = attempt_id
        vals = {
            'invoice_id': invoice.id,
            'invoice_number': invoice.name,
            'company_id': invoice.company_id.id,
            'sync_attempt_id': attempt_id,
            'sync_status': status,
            'sync_timestamp': fields.Datetime.now(),
            'attempt_number': attempt_num,
            'correlation_id': corr,
            'source': self.env.context.get('tally_source', 'manual'),
        }
        vals.update(kwargs)
        return self.create(vals)

    def update_status(self, status, **kwargs):
        """Update the log entry with new status and optional fields."""
        vals = {'sync_status': status}
        vals.update(kwargs)
        self.write(vals)

    def store_payload(self, payload):
        """Store the complete payload as JSON."""
        try:
            self.write({'payload_json': json.dumps(payload, default=str, indent=2)})
        except (TypeError, ValueError) as e:
            _logger.warning("Could not serialize payload for log %s: %s", self.id, e)

    def store_validation(self, validation_result):
        """Store the validation result as JSON."""
        try:
            self.write({'validation_json': json.dumps(validation_result, default=str, indent=2)})
        except (TypeError, ValueError) as e:
            _logger.warning("Could not serialize validation for log %s: %s", self.id, e)

    @api.model
    def check_duplicate(self, invoice):
        """Check if this invoice has already been successfully synced.

        Uses invoice_id + company as deterministic key; also checks request_hash
        for cross-invoice duplicate hash collisions.

        :return: existing log record if found, or False
        """
        # Primary check: same invoice already success
        existing = self.search([
            ('invoice_id', '=', invoice.id),
            ('sync_status', '=', 'success'),
        ], limit=1)
        if existing:
            return existing
        # Secondary: same company + request_hash already success (hash collision / duplicate payload)
        if invoice.tally_request_hash:
            dup = self.search([
                ('company_id', '=', invoice.company_id.id),
                ('request_hash', '=', invoice.tally_request_hash),
                ('sync_status', '=', 'success'),
                ('invoice_id', '!=', invoice.id),
            ], limit=1)
            if dup:
                _logger.warning("Duplicate request_hash %s already synced via invoice %s",
                                invoice.tally_request_hash, dup.invoice_number)
                return dup
        return False
