"""Add Telegram and AI Parser configuration to the Odoo Settings page."""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    telegram_bot_token = fields.Char(
        string='Telegram Bot Token',
        config_parameter='telegram_odoo_integration.bot_token',
        help='Token obtained from @BotFather when creating the bot on '
             'Telegram. Recommended: set the TELEGRAM_BOT_TOKEN '
             'environment variable instead; it takes precedence over '
             'this value.')
    telegram_allowed_chat_id = fields.Char(
        string='Allowed Chat ID',
        config_parameter='telegram_odoo_integration.allowed_chat_id',
        help='Only messages coming from this Telegram chat id will be '
             'processed. Leave empty to reject all chats (fail-closed). '
             'Find your chat id by messaging @userinfobot on Telegram.')
    ai_parser_url = fields.Char(
        string='AI Parser URL',
        config_parameter='telegram_odoo_integration.ai_parser_url',
        help='Base URL of the independent AI Parser application '
             '(e.g. https://ai-parser.example.com).')
    ai_parser_api_key = fields.Char(
        string='AI Parser API Key',
        config_parameter='telegram_odoo_integration.ai_parser_api_key',
        help='API key for authenticating with the AI Parser. '
             'Do NOT use the Telegram bot token here.')
    max_file_size_mb = fields.Integer(
        string='Max File Size (MB)',
        config_parameter='telegram_odoo_integration.max_file_size_mb',
        default=10,
        help='Maximum allowed file size for Telegram attachments in MB.')
    session_timeout_minutes = fields.Integer(
        string='Session Timeout (minutes)',
        config_parameter='telegram_odoo_integration.session_timeout_minutes',
        default=30,
        help='Idle order sessions are expired after this many minutes.')
    telegram_allowed_chat_ids = fields.Char(
        string='Additional Allowed Chat IDs',
        config_parameter='telegram_odoo_integration.allowed_chat_ids',
        help='Comma or space separated list of additional allowed chat IDs. '
             'For multi-user setups. Leave empty to use single Allowed Chat ID only.')
    rate_limit_per_minute = fields.Integer(
        string='Rate Limit (per minute)',
        config_parameter='telegram_odoo_integration.rate_limit_per_minute',
        default=20,
        help='Maximum Telegram messages per user per minute.')
    max_concurrent_jobs = fields.Integer(
        string='Max Concurrent Jobs per User',
        config_parameter='telegram_odoo_integration.max_concurrent_jobs',
        default=5,
        help='Maximum concurrent processing jobs per Telegram user.')
    poll_interval_minutes = fields.Integer(
        string='Parser Poll Interval (minutes)',
        config_parameter='telegram_odoo_integration.poll_interval_minutes',
        default=5,
        help='Interval for polling AI Parser for pending jobs.')
    job_status_timeout_minutes = fields.Integer(
        string='Job Status Timeout (minutes)',
        config_parameter='telegram_odoo_integration.job_status_timeout_minutes',
        default=30,
        help='Timeout for job status polling before marking as failed.')
