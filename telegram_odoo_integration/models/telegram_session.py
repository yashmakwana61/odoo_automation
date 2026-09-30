"""Session-based order collection model for Telegram integration.

Manages the lifecycle of an order being assembled from multiple Telegram
messages and attachments before submission to the AI Parser.
"""
import json
import logging
import uuid

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class TelegramOrderSession(models.Model):
    _name = 'telegram.order.session'
    _description = 'Telegram Order Session'
    _rec_name = 'session_id'
    _order = 'id desc'

    session_id = fields.Char(
        string='Session ID', required=True, readonly=True,
        default=lambda self: str(uuid.uuid4()), index=True, copy=False)
    staff_id = fields.Many2one(
        'res.users', string='Staff', required=True,
        ondelete='cascade', readonly=True)
    chat_id = fields.Char(
        string='Telegram Chat ID', required=True, readonly=True)
    telegram_username = fields.Char(
        string='Telegram Username', readonly=True)
    state = fields.Selection([
        ('new', 'New'),
        ('collecting', 'Collecting'),
        ('submitted', 'Submitted'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('incomplete', 'Incomplete'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
        ('needs_review', 'Needs Review'),
        ('retrying', 'Retrying'),
    ], string='Status', default='new', readonly=True, copy=False, index=True)
    request_id = fields.Char(
        string='AI Parser Request ID', readonly=True, copy=False, index=True)
    # New job-based fields (Phase 6/8)
    parser_job_id = fields.Char(
        string='Parser Job ID', readonly=True, copy=False, index=True)
    idempotency_key = fields.Char(
        string='Idempotency Key', readonly=True, copy=False, index=True)
    telegram_message_ids = fields.Text(
        string='Telegram Message IDs', readonly=True)
    # Extended status to support new state machine
    ai_parser_status = fields.Selection([
        ('PROCESSING', 'Processing'),
        ('SUCCESS', 'Success'),
        ('INCOMPLETE', 'Incomplete'),
        ('REQUIRES_REVIEW', 'Requires Review'),
        ('FAILED', 'Failed'),
        # New states
        ('RECEIVED', 'Received'),
        ('QUEUED', 'Queued'),
        ('EXTRACTING', 'Extracting'),
        ('NORMALIZING', 'Normalizing'),
        ('VALIDATING', 'Validating'),
        ('RESOLVING', 'Resolving'),
        ('READY_FOR_ODOO', 'Ready for Odoo'),
        ('SENT_TO_ODOO', 'Sent to Odoo'),
        ('COMPLETED', 'Completed'),
        ('NEEDS_REVIEW', 'Needs Review'),
        ('RETRYING', 'Retrying'),
    ], string='AI Parser Status', readonly=True, copy=False)
    ai_parser_response = fields.Text(
        string='AI Parser Response', readonly=True)
    sale_order_id = fields.Many2one(
        'sale.order', string='Sales Order',
        ondelete='set null', readonly=True)
    sale_order_name = fields.Char(
        string='Sales Order Reference', readonly=True)
    message_ids = fields.One2many(
        'telegram.session.message', 'session_id',
        string='Messages')
    attachment_ids = fields.One2many(
        'telegram.session.attachment', 'session_id',
        string='Attachments')
    message_count = fields.Integer(
        string='Messages', compute='_compute_counts')
    attachment_count = fields.Integer(
        string='Attachments', compute='_compute_counts')
    last_update_id = fields.Char(
        string='Last Telegram Update ID', readonly=True, index=True)
    error_message = fields.Text(
        string='Error Message', readonly=True)
    incomplete_fields = fields.Text(
        string='Incomplete Fields', readonly=True)

    @api.depends('message_ids', 'attachment_ids')
    def _compute_counts(self):
        for session in self:
            session.message_count = len(session.message_ids)
            session.attachment_count = len(session.attachment_ids)

    def action_submit(self):
        """Transition to submitted state and trigger AI Parser submission."""
        self.ensure_one()
        if self.state != 'collecting':
            return False
        if not self.message_ids and not self.attachment_ids:
            return False
        # Generate idempotency key from chat+messages if not exists
        idem = self.idempotency_key or str(uuid.uuid4())
        self.write({
            'state': 'submitted',
            'request_id': self.request_id or str(uuid.uuid4()),
            'idempotency_key': idem,
        })
        self._send_to_ai_parser()
        return True

    def _send_to_ai_parser(self):
        """Build request payload and send to AI Parser."""
        self.ensure_one()
        from odoo.addons.telegram_odoo_integration.models.ai_parser_client import (
            AIParserClient,
        )
        client = AIParserClient(self.env)
        try:
            result = client.submit_order(self)
            _logger.info(
                'AI Parser response for session %s: %s',
                self.session_id, result)
            # New job API returns job_id
            job_id = result.get('job_id') or result.get('id') or result.get('request_id')
            if job_id:
                self.write({'parser_job_id': str(job_id)})
            # Link telegram.message records with job_id for correlation
            if job_id:
                msgs = self.env['telegram.message'].sudo().search([
                    ('session_id', '=', self.id),
                ])
                if msgs:
                    msgs.write({'parser_job_id': str(job_id)})
            status = result.get('status') or result.get('state')
            # Normalize
            if status:
                status = str(status).upper()
            if status in ('PROCESSING', 'RECEIVED', 'QUEUED', 'EXTRACTING',
                          'NORMALIZING', 'VALIDATING', 'RESOLVING',
                          'READY_FOR_ODOO', 'SENT_TO_ODOO'):
                self.write({
                    'state': 'processing',
                    'ai_parser_status': status,
                    'ai_parser_response': json.dumps(result, ensure_ascii=False),
                })
            elif status in ('SUCCESS', 'COMPLETED'):
                self._handle_success(result)
            elif status in ('INCOMPLETE',):
                self._handle_incomplete(result)
            elif status in ('NEEDS_REVIEW', 'REQUIRES_REVIEW'):
                self._handle_needs_review(result)
            elif status == 'RETRYING':
                self.write({
                    'state': 'retrying',
                    'ai_parser_status': 'RETRYING',
                    'ai_parser_response': json.dumps(result, ensure_ascii=False),
                })
            elif status == 'FAILED':
                self._handle_failed(result)
            else:
                _logger.warning(
                    'Unknown AI Parser status for session %s: %s',
                    self.session_id, status)
                self.write({
                    'state': 'processing',
                    'ai_parser_status': status or 'PROCESSING',
                    'ai_parser_response': json.dumps(result, ensure_ascii=False),
                })
        except Exception as exc:
            _logger.exception(
                'Failed to submit session %s to AI Parser', self.session_id)
            self.write({
                'state': 'failed',
                'ai_parser_status': 'FAILED',
                'error_message': str(exc),
            })

    def _handle_success(self, result):
        """Handle successful AI Parser response."""
        self.ensure_one()
        vals = {
            'state': 'completed',
            'ai_parser_status': 'SUCCESS',
            'ai_parser_response': json.dumps(result, ensure_ascii=False),
        }
        sale_order_id = result.get('sale_order_id')
        if sale_order_id:
            vals['sale_order_id'] = sale_order_id
        if result.get('sale_order_name'):
            vals['sale_order_name'] = result['sale_order_name']
        self.write(vals)
        if sale_order_id:
            self._link_messages_to_order(sale_order_id)
        _logger.info(
            'Session %s completed. Order: %s',
            self.session_id, result.get('sale_order_name'))

    def _link_messages_to_order(self, sale_order_id):
        """Link all telegram.message records of this session to the sale order."""
        self.ensure_one()
        messages = self.env['telegram.message'].sudo().search([
            ('session_id', '=', self.id),
        ])
        if messages:
            messages.write({
                'order_id': sale_order_id,
                'state': 'order_created',
            })
            _logger.info(
                'Linked %d telegram messages to order %s for session %s',
                len(messages), sale_order_id, self.session_id)

    def _handle_incomplete(self, result):
        """Handle incomplete AI Parser response."""
        self.ensure_one()
        vals = {
            'state': 'incomplete',
            'ai_parser_status': 'INCOMPLETE',
            'ai_parser_response': json.dumps(result, ensure_ascii=False),
        }
        sale_order_id = result.get('sale_order_id') or result.get('order_id')
        if sale_order_id:
            try:
                vals['sale_order_id'] = int(sale_order_id)
            except Exception:
                vals['sale_order_id'] = sale_order_id
        if result.get('sale_order_name') or result.get('order_name'):
            vals['sale_order_name'] = result.get('sale_order_name') or result.get('order_name')
        missing = result.get('incomplete_fields') or result.get('missing_fields') or []
        if missing:
            vals['incomplete_fields'] = ', '.join(missing) if isinstance(missing, (list, tuple)) else str(missing)
        self.write(vals)
        if vals.get('sale_order_id'):
            self._link_messages_to_order(vals['sale_order_id'])
        _logger.info(
            'Session %s incomplete. Fields needed: %s',
            self.session_id, vals.get('incomplete_fields'))

    def _handle_needs_review(self, result):
        """Handle NEEDS_REVIEW state."""
        self.ensure_one()
        vals = {
            'state': 'needs_review',
            'ai_parser_status': 'NEEDS_REVIEW',
            'ai_parser_response': json.dumps(result, ensure_ascii=False),
            'error_message': result.get('message') or result.get('reason') or 'Needs review',
        }
        sale_order_id = result.get('sale_order_id') or result.get('order_id')
        if sale_order_id:
            try:
                vals['sale_order_id'] = int(sale_order_id)
            except Exception:
                pass
        if result.get('sale_order_name') or result.get('order_name'):
            vals['sale_order_name'] = result.get('sale_order_name') or result.get('order_name')
        # Store reason details
        if result.get('reason'):
            vals['incomplete_fields'] = str(result.get('reason'))
        self.write(vals)
        if vals.get('sale_order_id'):
            self._link_messages_to_order(vals['sale_order_id'])

    def _handle_failed(self, result):
        """Handle failed AI Parser response."""
        self.ensure_one()
        self.write({
            'state': 'failed',
            'ai_parser_status': 'FAILED',
            'ai_parser_response': json.dumps(result, ensure_ascii=False),
            'error_message': result.get('message') or result.get('error') or result.get('reason') or 'AI Parser processing failed',
        })

    def action_cancel(self):
        """Cancel the session and clean up temporary files."""
        self.ensure_one()
        if self.state in ('completed', 'cancelled', 'expired'):
            return False
        self._cleanup_temp_files()
        self.write({'state': 'cancelled'})
        return True

    def action_retry(self):
        """Retry submission to AI Parser after a failure."""
        self.ensure_one()
        if self.state not in ('failed', 'incomplete'):
            return False
        self.write({
            'state': 'collecting',
            'ai_parser_status': False,
            'ai_parser_response': False,
            'error_message': False,
            'request_id': str(uuid.uuid4()),
        })
        return True

    def _cleanup_temp_files(self):
        """Remove temporary attachment files."""
        for attachment in self.attachment_ids:
            attachment._unlink_file()
        return True

    def get_status_message(self):
        """Return a human-readable status message for Telegram."""
        self.ensure_one()
        # Use status mapper for richer messages
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_status_mapper import (
                format_ack_message, format_completed, format_incomplete,
                format_needs_review, format_failed, USER_MESSAGES,
            )
            ref = self.parser_job_id or self.session_id[:8]
            if self.state == 'processing':
                return USER_MESSAGES['processing'].format(ref=ref)
            if self.state == 'retrying':
                return USER_MESSAGES['retrying'].format(ref=ref)
            if self.state == 'completed':
                return format_completed(self.sale_order_name, item_count=None)
            if self.state == 'incomplete':
                missing = []
                if self.incomplete_fields:
                    missing = [x.strip() for x in self.incomplete_fields.split(',')]
                return format_incomplete(self.sale_order_name, missing)
            if self.state == 'needs_review':
                return format_needs_review(self.parser_job_id or self.session_id, reason=self.error_message, short_ref=ref)
            if self.state == 'failed':
                return format_failed(self.parser_job_id or self.session_id, short_ref=ref)
        except Exception:
            pass
        messages = {
            'new': 'Session started. Send your order details.',
            'collecting': (
                'Collecting order details. '
                'Send /done when ready to submit.'
            ),
            'submitted': 'Order submitted for processing.',
            'processing': 'AI Parser is processing your order.',
            'completed': (
                'Order received successfully.\n'
                'Sales Order %s is being prepared in Odoo.'
            ) % (self.sale_order_name or ''),
            'incomplete': (
                'Sales Order %s created in Odoo but is not ready for '
                'Tally synchronization.\n\n'
                'Please open the Sales Order in Odoo and complete the '
                'missing information.'
            ) % (self.sale_order_name or ''),
            'failed': (
                'Unable to process this order.\n'
                'Please review the input or contact the administrator.'
            ),
            'cancelled': 'Order cancelled.',
            'expired': 'Order session expired.',
            'needs_review': 'Order needs review. Please check in Odoo. Reference: %s' % (self.parser_job_id or self.session_id[:8]),
            'retrying': 'We\'re still processing your order. Reference: %s' % (self.parser_job_id or self.session_id[:8]),
        }
        return messages.get(self.state, 'Unknown status.')

    @api.autovacuum
    def _cron_cleanup_expired_sessions(self):
        """Mark and clean up expired sessions."""
        timeout = int(self.env['ir.config_parameter'].sudo().get_param(
            'telegram_odoo_integration.session_timeout_minutes', '30'))
        if timeout <= 0:
            return
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(minutes=timeout)
        expired = self.search([
            ('state', '=', 'collecting'),
            ('write_date', '<', cutoff),
        ])
        for session in expired:
            session._cleanup_temp_files()
            session.write({'state': 'expired'})
        if expired:
            _logger.info('Expired %d idle order sessions', len(expired))

    def _cron_poll_ai_parser(self):
        """Poll the AI Parser for sessions stuck in 'processing' state.

        Uses job_id if available, else falls back to request_id.
        Configurable max attempts / timeout via ir.config_parameter.
        """
        # Include both processing and retrying
        processing = self.search([
            ('state', 'in', ('processing', 'submitted', 'retrying')),
            '|',
            ('request_id', '!=', False),
            ('parser_job_id', '!=', False),
        ])
        if not processing:
            return
        from odoo.addons.telegram_odoo_integration.models.ai_parser_client import (
            AIParserClient,
        )
        from odoo.addons.telegram_odoo_integration.models.telegram_status_mapper import (
            map_parser_state,
        )
        client = AIParserClient(self.env)
        for session in processing:
            job_ref = session.parser_job_id or session.request_id
            if not job_ref:
                continue
            try:
                result = client.query_status(job_ref)
                _logger.info(
                    'AI Parser poll for session %s: %s',
                    session.session_id, result)
                status = (result.get('status') or result.get('state') or '').upper()
                if not status:
                    continue
                t_state = map_parser_state(status)
                if status in ('SUCCESS', 'COMPLETED'):
                    session._handle_success(result)
                elif status == 'INCOMPLETE':
                    session._handle_incomplete(result)
                elif status in ('NEEDS_REVIEW', 'REQUIRES_REVIEW'):
                    session._handle_needs_review(result)
                elif status == 'FAILED':
                    session._handle_failed(result)
                elif status == 'RETRYING':
                    session.write({
                        'state': 'retrying',
                        'ai_parser_status': 'RETRYING',
                        'ai_parser_response': json.dumps(result, ensure_ascii=False),
                    })
                elif t_state in ('processing', 'creating_order'):
                    session.write({
                        'ai_parser_status': status,
                        'ai_parser_response': json.dumps(result, ensure_ascii=False),
                    })
                else:
                    session.write({'ai_parser_status': status})
                # If order created, link
                sale_order_id = result.get('sale_order_id') or result.get('order_id')
                sale_order_name = result.get('sale_order_name') or result.get('order_name')
                if sale_order_id and not session.sale_order_id:
                    try:
                        session.write({'sale_order_id': int(sale_order_id)})
                    except Exception:
                        pass
                if sale_order_name and not session.sale_order_name:
                    session.write({'sale_order_name': sale_order_name})
                # Update telegram.message correlation
                if result.get('parser_state'):
                    msgs = self.env['telegram.message'].sudo().search([
                        ('session_id', '=', session.id),
                    ])
                    if msgs:
                        msgs.write({'parser_state': status})
            except Exception:
                _logger.exception(
                    'Failed to poll AI Parser for session %s',
                    session.session_id)

    def short_ref(self):
        """User-friendly short reference derived from job_id or session_id."""
        self.ensure_one()
        if self.parser_job_id:
            # job_id may be like ORD-20260925-000184
            return self.parser_job_id
        return self.session_id[:8]


class TelegramSessionMessage(models.Model):
    _name = 'telegram.session.message'
    _description = 'Telegram Session Message'
    _order = 'sequence, id'

    session_id = fields.Many2one(
        'telegram.order.session', string='Session',
        required=True, ondelete='cascade', index=True)
    message_text = fields.Text(string='Message Text', required=True)
    telegram_message_id = fields.Char(
        string='Telegram Message ID', readonly=True)
    sequence = fields.Integer(string='Sequence', default=10)


class TelegramSessionAttachment(models.Model):
    _name = 'telegram.session.attachment'
    _description = 'Telegram Session Attachment'
    _order = 'sequence, id'

    session_id = fields.Many2one(
        'telegram.order.session', string='Session',
        required=True, ondelete='cascade', index=True)
    filename = fields.Char(string='Filename', required=True)
    mime_type = fields.Char(string='MIME Type', required=True)
    file_size = fields.Integer(string='File Size (bytes)')
    file_hash = fields.Char(string='File Hash (SHA-256)')
    file_path = fields.Char(string='Temporary File Path', readonly=True)
    telegram_file_id = fields.Char(
        string='Telegram File ID', readonly=True)
    telegram_file_unique_id = fields.Char(
        string='Telegram File Unique ID', readonly=True)
    secure_token = fields.Char(
        string='Secure Access Token', readonly=True, index=True,
        default=lambda self: str(uuid.uuid4()))
    sequence = fields.Integer(string='Sequence', default=10)
    is_served = fields.Boolean(string='Served', default=False)

    def _unlink_file(self):
        """Remove the physical file from disk."""
        import os
        for record in self:
            if record.file_path and os.path.exists(record.file_path):
                try:
                    os.unlink(record.file_path)
                except OSError:
                    _logger.warning(
                        'Could not delete temp file %s', record.file_path)

    def mark_served(self):
        """Mark attachment as served to AI Parser."""
        self.write({'is_served': True})

    def serve_file(self):
        """Return file content for the AI Parser."""
        self.ensure_one()
        import os
        if not self.file_path or not os.path.exists(self.file_path):
            return None
        with open(self.file_path, 'rb') as fh:
            return fh.read()
