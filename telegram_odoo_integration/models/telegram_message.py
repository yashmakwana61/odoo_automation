"""Telegram chat-history model.

Stores every incoming update as an audit trail and links successful
messages to the created partner and Sales Order.
"""
import json

from odoo import fields, models


class TelegramMessage(models.Model):
    _name = 'telegram.message'
    _description = 'Telegram Message'
    _rec_name = 'message_text'
    _order = 'id desc'

    update_id = fields.Char(
        string='Update ID', index=True, readonly=True)
    chat_id = fields.Char(
        string='Chat ID', index=True, readonly=True)
    telegram_username = fields.Char(
        string='Telegram Username', index=True, readonly=True)
    message_text = fields.Text(string='Message Text', readonly=True)
    state = fields.Selection([
        ('received', 'Received'),
        ('ignored', 'Ignored'),
        ('rejected', 'Rejected'),
        ('error', 'Error'),
        ('order_created', 'Order Created'),
    ], string='Status', default='received', readonly=True, copy=False)
    session_id = fields.Many2one(
        'telegram.order.session', string='Order Session',
        ondelete='set null', readonly=True)
    partner_id = fields.Many2one(
        'res.partner', string='Customer',
        ondelete='set null', readonly=True)
    order_id = fields.Many2one(
        'sale.order', string='Sales Order',
        ondelete='set null', readonly=True)
    error_message = fields.Text(string='Error Message', readonly=True)
    raw_update = fields.Text(string='Raw Update (JSON)', readonly=True)
    # Job correlation (Phase 7 & 23)
    parser_job_id = fields.Char(
        string='Parser Job ID', index=True, readonly=True, copy=False)
    telegram_message_id = fields.Char(
        string='Telegram Message ID', index=True, readonly=True)
    parser_state = fields.Char(
        string='Parser State', readonly=True, copy=False)
    tally_status = fields.Char(
        string='Tally Status', readonly=True, copy=False)

    def _log_update(self, data):
        """Persist a raw Telegram update and return the created record."""
        try:
            raw = json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            raw = str(data)

        message = data.get('message') or data.get('edited_message') or {}
        chat = message.get('chat') or {}
        from_user = message.get('from') or {}
        return self.sudo().create({
            'update_id': str(data.get('update_id') or ''),
            'chat_id': str(chat.get('id') or ''),
            'telegram_username': (
                from_user.get('username')
                or from_user.get('first_name')
                or str(from_user.get('id') or '')
            ),
            'message_text': message.get('text') or '',
            'telegram_message_id': str(message.get('message_id') or ''),
            'state': 'received',
            'raw_update': raw,
        })

    _sql_constraints = [
        ('unique_update_id', 'UNIQUE(update_id)', 'Update ID must be unique.'),
    ]
