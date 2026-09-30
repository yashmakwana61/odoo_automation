"""HTTP controllers for the Telegram integration."""
import logging

from odoo import http
from odoo.http import request

from odoo.addons.telegram_odoo_integration.models.telegram_bot_service import (
    TelegramBotService,
)

_logger = logging.getLogger(__name__)


class TelegramWebhook(http.Controller):
    """Public webhook endpoint receiving Telegram bot updates."""

    @http.route('/telegram/webhook', type='json', auth='public', csrf=False,
                methods=['POST'])
    def telegram_webhook(self, **kwargs):
        """Entry point for Telegram updates.

        Telegram POSTs a *plain* JSON object (e.g. ``{"update_id": ...,
        "message": {...}}``), not a JSON-RPC envelope. Odoo's ``type='json'``
        routing parses the body but only forwards a ``params`` member to the
        method, so the full update is read explicitly through
        ``request.get_json_data()``. Any ``kwargs`` (e.g. from a JSON-RPC
        envelope) are merged as a fallback.

        The returned dict is wrapped by the framework in the standard jsonrpc
        envelope; Telegram ignores the response body, the user-facing reply is
        sent back to the chat through the Telegram Bot API by the service.
        """
        _logger.info('Telegram webhook received')

        try:
            data = request.get_json_data() or {}
        except Exception:  # noqa: BLE001 - never let the webhook crash
            _logger.exception('Could not parse Telegram webhook body')
            return {'success': False, 'error': 'Invalid JSON body'}
        data.update(kwargs)

        service = TelegramBotService(request.env)
        return service.process_update(data)


class TelegramHealth(http.Controller):
    """Health/readiness checks for Telegram integration."""

    @http.route('/telegram/health', type='http', auth='public', csrf=False, methods=['GET'])
    def health(self, **kwargs):
        """Return integration health: Telegram config, Parser, Odoo."""
        import time
        result = {
            'status': 'ok',
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'checks': {},
        }
        # Telegram bot config
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_bot_service import TelegramBotService
            svc = TelegramBotService(request.env)
            token_ok = bool(svc.get_bot_token())
            result['checks']['telegram_bot'] = 'ok' if token_ok else 'not_configured'
        except Exception as e:
            result['checks']['telegram_bot'] = 'error: %s' % e
        # Parser
        try:
            from odoo.addons.telegram_odoo_integration.models.ai_parser_client import AIParserClient
            client = AIParserClient(request.env)
            health = client.check_health()
            result['checks']['ai_parser'] = health.get('status', 'unknown')
            result['checks']['ai_parser_detail'] = health
        except Exception as e:
            result['checks']['ai_parser'] = 'error: %s' % e
        # Odoo DB
        try:
            request.env['telegram.message'].sudo().search([], limit=1)
            result['checks']['odoo_db'] = 'ok'
        except Exception as e:
            result['checks']['odoo_db'] = 'error: %s' % e
            result['status'] = 'degraded'
        # Metrics
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import get_all_counters
            result['metrics'] = get_all_counters()
        except Exception:
            pass
        # Overall status
        if any('error' in str(v) for v in result['checks'].values()):
            result['status'] = 'degraded'
        return request.make_json_response(result)

    @http.route('/telegram/metrics', type='http', auth='public', csrf=False, methods=['GET'])
    def metrics(self, **kwargs):
        """Expose metrics as JSON (protected by simple token if configured)."""
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import get_all_counters
            counters = get_all_counters()
            return request.make_json_response(counters)
        except Exception as e:
            return request.make_json_response({'error': str(e)}, status=500)


class AIParserAttachment(http.Controller):
    """Serve session attachments to the AI Parser via secure tokens."""

    @http.route(
        '/ai_parser/attachment/<string:token>',
        type='http', auth='public', csrf=False, methods=['GET'])
    def serve_attachment(self, token, **kwargs):
        """Serve an attachment file to the AI Parser.

        The token is a random UUID generated when the attachment is created.
        Single-use: after serving, the attachment is marked and the token
        is invalidated. Time-limited: tokens older than the configured TTL
        are rejected.
        """
        from datetime import datetime, timedelta
        from odoo.addons.telegram_odoo_integration.models.ai_parser_client import (
            ATTACHMENT_TOKEN_TTL_SECONDS,
        )

        Attachment = request.env['telegram.session.attachment'].sudo()
        attachment = Attachment.search([
            ('secure_token', '=', token),
            ('is_served', '=', False),
        ], limit=1)

        if not attachment:
            _logger.warning(
                'Attachment not found or already served: %s', token)
            return request.make_json_response(
                {'error': 'Not found or already served'}, status=404)

        # Check TTL
        created = attachment.create_date or datetime.now()
        if datetime.now() - created > timedelta(seconds=ATTACHMENT_TOKEN_TTL_SECONDS):
            _logger.warning('Attachment token expired: %s', token)
            return request.make_json_response(
                {'error': 'Token expired'}, status=410)

        file_content = attachment.serve_file()
        if file_content is None:
            _logger.error(
                'Attachment file not found on disk: %s', attachment.file_path)
            return request.make_json_response(
                {'error': 'File not found on server'}, status=404)

        attachment.mark_served()

        from odoo.http import Response
        response = Response(file_content, content_type=attachment.mime_type)
        response.headers['Content-Disposition'] = (
            'attachment; filename="%s"' % attachment.filename)
        response.headers['Cache-Control'] = 'no-store'
        _logger.info(
            'Served attachment %s for session %s',
            attachment.filename, attachment.session_id.session_id)
        return response
