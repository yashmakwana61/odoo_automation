"""Extend res.partner with Telegram identity fields."""
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    telegram_username = fields.Char(
        string='Telegram Username', index=True)
    telegram_chat_id = fields.Char(
        string='Telegram Chat ID', index=True)
