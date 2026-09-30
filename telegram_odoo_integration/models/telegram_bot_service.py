"""Business logic for the Telegram -> Sales Order integration - hardened."""
import hashlib
import logging
import os
import tempfile
import uuid

import requests

_logger = logging.getLogger(__name__)

CONFIG_BOT_TOKEN = 'telegram_odoo_integration.bot_token'
CONFIG_ALLOWED_CHAT_ID = 'telegram_odoo_integration.allowed_chat_id'
CONFIG_ALLOWED_CHAT_IDS = 'telegram_odoo_integration.allowed_chat_ids'
CONFIG_MAX_FILE_SIZE_MB = 'telegram_odoo_integration.max_file_size_mb'

DEFAULT_MAX_FILE_SIZE_MB = 10

ALLOWED_MIME_TYPES = {
    'image/jpeg',
    'image/png',
    'image/webp',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
}

COMMANDS = {
    '/neworder': 'new_order',
    '/done': 'submit_order',
    '/cancel': 'cancel_order',
    '/status': 'show_status',
    '/start': 'show_start',
    '/help': 'show_help',
    '/myorders': 'my_orders',
    '/order': 'order_lookup',
}


class TelegramBotService:
    def __init__(self, env):
        self.env = env

    def get_bot_token(self):
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        if not token:
            token = self.env['ir.config_parameter'].sudo().get_param(CONFIG_BOT_TOKEN, '')
        return token.strip()

    def get_allowed_chat_id(self):
        return self.env['ir.config_parameter'].sudo().get_param(CONFIG_ALLOWED_CHAT_ID, '').strip()

    def get_allowed_chat_ids(self):
        single = self.get_allowed_chat_id()
        multi = self.env['ir.config_parameter'].sudo().get_param(CONFIG_ALLOWED_CHAT_IDS, '').strip()
        ids = set()
        if single:
            ids.add(str(single).strip())
        if multi:
            for part in multi.replace(',', ' ').split():
                part = part.strip()
                if part:
                    ids.add(part)
        return ids

    def is_chat_allowed(self, chat_id):
        allowed = self.get_allowed_chat_ids()
        if not allowed:
            _logger.warning('No allowed chat id configured; rejecting all chats')
            return False
        # Also allow if res.partner has matching telegram_chat_id
        if str(chat_id) in allowed:
            return True
        # Check partner mapping fallback
        Partner = self.env['res.partner'].sudo()
        partner = Partner.search([('telegram_chat_id', '=', str(chat_id))], limit=1)
        if partner:
            return True
        return False

    def get_max_file_size_bytes(self):
        mb = int(self.env['ir.config_parameter'].sudo().get_param(CONFIG_MAX_FILE_SIZE_MB, str(DEFAULT_MAX_FILE_SIZE_MB)) or DEFAULT_MAX_FILE_SIZE_MB)
        return mb * 1024 * 1024

    def _is_duplicate_update(self, update_id):
        if not update_id:
            return False
        existing = self.env['telegram.message'].sudo().search([('update_id', '=', str(update_id))], limit=1)
        return bool(existing)

    def _is_duplicate_message(self, chat_id, message_id):
        if not chat_id or not message_id:
            return None
        existing = self.env['telegram.message'].sudo().search([
            ('chat_id', '=', str(chat_id)),
            ('telegram_message_id', '=', str(message_id)),
            ('parser_job_id', '!=', False),
        ], limit=1)
        if existing:
            return existing
        # Also check session level idempotency via telegram_message_ids
        # Search sessions with matching chat and message
        sessions = self.env['telegram.order.session'].sudo().search([
            ('chat_id', '=', str(chat_id)),
            ('telegram_message_ids', 'ilike', str(message_id)),
        ], limit=1)
        return sessions if sessions else None

    def _get_existing_job_for_message(self, chat_id, message_id):
        if not message_id:
            return None
        # Look for telegram.message with same chat+msg_id that already has parser_job_id
        tm = self.env['telegram.message'].sudo().search([
            ('chat_id', '=', str(chat_id)),
            ('telegram_message_id', '=', str(message_id)),
            ('parser_job_id', '!=', False),
        ], limit=1, order='id desc')
        if tm and tm.parser_job_id:
            return tm.parser_job_id
        sess = self.env['telegram.order.session'].sudo().search([
            ('chat_id', '=', str(chat_id)),
            ('telegram_message_ids', 'ilike', str(message_id)),
            ('parser_job_id', '!=', False),
        ], limit=1, order='id desc')
        if sess and sess.parser_job_id:
            return sess.parser_job_id
        return None

    def _resolve_staff(self, chat_id, username, from_user_id):
        Partner = self.env['res.partner'].sudo()
        partner = Partner.search([
            '|',
            ('telegram_chat_id', '=', str(chat_id)),
            ('telegram_username', '=', username),
        ], limit=1)
        if partner and partner.user_ids:
            return partner.user_ids[0]
        return self.env.ref('base.public_user')

    def process_update(self, data):
        data = dict(data or {})
        if 'params' in data and not data.get('update_id'):
            data = dict(data.get('params') or {})
        update_id = data.get('update_id')
        message = data.get('message') or data.get('edited_message') or {}
        document = message.get('document') or {}
        photo = message.get('photo') or []
        chat = message.get('chat') or {}
        chat_id = str(chat.get('id') or '')
        from_user = message.get('from') or {}
        username = from_user.get('username') or from_user.get('first_name') or str(from_user.get('id') or '')
        from_user_id = from_user.get('id')
        text = message.get('text') or ''
        telegram_msg_id = str(message.get('message_id') or '')

        # Rate limiting
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_rate_limiter import check_rate_limit
            allowed, remaining = check_rate_limit(chat_id, env=self.env)
            if not allowed:
                from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc, log_event
                inc('telegram_rate_limited_total')
                log_event('rate_limited', telegram_chat_id=chat_id, telegram_user_id=from_user_id, message_id=telegram_msg_id)
                # Still log update for audit
                log = self.env['telegram.message'].sudo()._log_update(data)
                self.send_telegram_message(chat_id, '\u23f3 You\'re sending messages too quickly. Please wait a moment and try again.')
                return {'success': False, 'error': 'Rate limited'}
        except Exception:
            pass

        if self._is_duplicate_update(update_id):
            _logger.info('Duplicate update %s, skipping', update_id)
            try:
                from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc
                inc('telegram_duplicate_messages_total')
            except Exception:
                pass
            return {'success': True, 'duplicate': True}

        # Idempotency on chat+message_id -> job_id
        existing_job = self._get_existing_job_for_message(chat_id, telegram_msg_id) if telegram_msg_id else None
        if existing_job:
            _logger.info('Duplicate Telegram message %s:%s -> job %s, skipping creation', chat_id, telegram_msg_id, existing_job)
            try:
                from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc, log_event
                inc('telegram_duplicate_messages_total')
                log_event('duplicate_message', telegram_chat_id=chat_id, message_id=telegram_msg_id, job_id=existing_job)
            except Exception:
                pass
            # Persist audit but mark duplicate
            log = self.env['telegram.message'].sudo()._log_update(data)
            log.write({'parser_job_id': str(existing_job), 'state': 'ignored'})
            self.send_telegram_message(chat_id, '\u2705 Order already received.\nReference: %s' % str(existing_job)[:16])
            return {'success': True, 'duplicate': True, 'job_id': str(existing_job)}

        log = self.env['telegram.message'].sudo()._log_update(data)
        # Ensure message_id stored
        if telegram_msg_id and not log.telegram_message_id:
            log.write({'telegram_message_id': telegram_msg_id})

        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc, log_event
            inc('telegram_messages_received_total')
            log_event('message_received', telegram_chat_id=chat_id, telegram_user_id=from_user_id, message_id=telegram_msg_id, update_id=str(update_id))
        except Exception:
            pass

        if not self.is_chat_allowed(chat_id):
            _logger.warning('Rejected Telegram update %s from unauthorized chat %s', update_id, chat_id)
            log.write({'state': 'rejected', 'error_message': 'Unauthorized chat id'})
            try:
                from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc
                inc('telegram_unauthorized_total')
            except Exception:
                pass
            from odoo.addons.telegram_odoo_integration.models.telegram_error_translator import unauthorized_message
            self.send_telegram_message(chat_id, unauthorized_message())
            return {'success': False, 'error': 'Unauthorized chat id'}

        staff = self._resolve_staff(chat_id, username, from_user_id)

        has_files = bool(photo or document)
        if has_files:
            return self._handle_file_message(log, chat_id, username, staff, photo, document, telegram_msg_id, text)

        if not text or not text.strip():
            log.write({'state': 'ignored'})
            return {'success': False, 'error': 'No content', 'ignored': True}

        # Handle Lite validation: empty, etc. but not deep validation
        if len(text.strip()) == 0:
            self.send_telegram_message(chat_id, 'Please send your order details.')
            log.write({'state': 'error', 'error_message': 'Empty message'})
            return {'success': False, 'error': 'Empty message'}

        return self._handle_text_message(log, chat_id, username, staff, text, telegram_msg_id)

    def _handle_text_message(self, log, chat_id, username, staff, text, telegram_msg_id):
        text_stripped = text.strip()
        # Extract command including parameters like "/order SO123"
        first_token = text_stripped.split()[0].lower() if text_stripped else ''
        base_cmd = first_token.split('@')[0]  # handle /start@botname
        if base_cmd in COMMANDS:
            handler_name = COMMANDS[base_cmd]
            handler = getattr(self, '_cmd_%s' % handler_name, None)
            if handler:
                return handler(log, chat_id, username, staff, text_stripped)
        return self._add_text_to_session(log, chat_id, username, staff, text_stripped, telegram_msg_id)

    def _cmd_show_start(self, log, chat_id, username, staff, text):
        log.write({'state': 'ignored'})
        msg = (
            '\U0001f44b Welcome!\n\n'
            'Send your order here as text, image, PDF, or Excel.\n\n'
            'I\'ll process it and create the order in Odoo automatically.\n\n'
            'Use /help to see available commands.'
        )
        self.send_telegram_message(chat_id, msg)
        return {'success': True, 'message': 'start sent', 'ignored': True}

    def _cmd_show_help(self, log, chat_id, username, staff, text):
        log.write({'state': 'ignored'})
        help_text = (
            'You can send me:\n\n'
            '\u2022 A text order\n'
            '\u2022 A photo of an order\n'
            '\u2022 A PDF\n'
            '\u2022 An Excel file\n\n'
            'Just send the order here and I\'ll process it.\n\n'
            'Commands:\n'
            '/status \u2014 Check processing status\n'
            '/myorders \u2014 View recent orders\n'
            '/order <number> \u2014 Check an order\n'
            '/help \u2014 Show help\n'
            '/neworder - Start collection, /done - Submit, /cancel - Cancel'
        )
        self.send_telegram_message(chat_id, help_text)
        return {'success': True, 'message': 'help sent', 'ignored': True}

    def _cmd_new_order(self, log, chat_id, username, staff, text):
        active = self._get_active_session(chat_id)
        if active:
            log.write({'state': 'ignored'})
            self.send_telegram_message(chat_id, 'You already have an active order session (%s).\nSend /done to submit it, /cancel to discard it, or continue sending messages.' % active.session_id[:8])
            return {'success': True, 'message': 'active session exists', 'session_id': active.session_id}
        session = self.env['telegram.order.session'].sudo().create({
            'staff_id': staff.id,
            'chat_id': chat_id,
            'telegram_username': username,
            'state': 'collecting',
            'last_update_id': str(log.update_id),
            'telegram_message_ids': str(log.telegram_message_id or ''),
        })
        log.write({'state': 'received', 'session_id': session.id})
        _logger.info('New order session %s started by %s', session.session_id, username)
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import log_event
            log_event('session_started', telegram_chat_id=chat_id, telegram_username=username, session_id=session.session_id)
        except Exception:
            pass
        self.send_telegram_message(chat_id, 'New order started.\nSend customer details and order information.\nSend /done when ready to submit.')
        return {'success': True, 'message': 'session started', 'session_id': session.session_id}

    def _cmd_submit_order(self, log, chat_id, username, staff, text):
        session = self._get_active_session(chat_id)
        if not session:
            log.write({'state': 'ignored'})
            self.send_telegram_message(chat_id, 'No active order session. Send /neworder to start one.')
            return {'success': False, 'error': 'No active order session'}
        if not session.message_ids and not session.attachment_ids:
            self.send_telegram_message(chat_id, 'No messages or files collected yet.\nSend your order details first, then /done.')
            return {'success': False, 'error': 'No messages or files collected'}
        # Check concurrent jobs limit
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_rate_limiter import check_concurrent
            allowed, cnt, limit = check_concurrent(chat_id, env=self.env)
            if not allowed:
                from odoo.addons.telegram_odoo_integration.models.telegram_error_translator import too_many_jobs_message
                self.send_telegram_message(chat_id, too_many_jobs_message())
                return {'success': False, 'error': 'Too many concurrent jobs'}
        except Exception:
            pass
        log.write({'session_id': session.id})
        # Instant ACK before parser call
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_status_mapper import format_ack_message
            ack = format_ack_message(session.parser_job_id or session.session_id, short_ref=session.session_id[:8])
            # Use parser_job_id if already has, otherwise session
            self.send_telegram_message(chat_id, ack)
        except Exception:
            self.send_telegram_message(chat_id, '\u2705 Order received.\nI\'m processing your order now.\nReference: %s' % session.session_id[:8])
        # Store message id correlation
        if log.telegram_message_id:
            existing_ids = session.telegram_message_ids or ''
            if log.telegram_message_id not in existing_ids:
                session.write({'telegram_message_ids': (existing_ids + ',' + log.telegram_message_id).strip(',')})
        session.action_submit()
        # After submit, send enriched status
        try:
            status_msg = session.get_status_message()
            # If state is processing, don't spam duplicate; only send if different from ack
            if session.state in ('processing', 'submitted', 'retrying'):
                pass
            else:
                self.send_telegram_message(chat_id, status_msg)
        except Exception:
            pass
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc
            inc('telegram_orders_submitted_total')
            if session.parser_job_id:
                inc('telegram_parser_jobs_created_total')
        except Exception:
            pass
        # Link log to job
        if session.parser_job_id:
            log.write({'parser_job_id': session.parser_job_id})
        return {'success': True, 'message': session.get_status_message(), 'session_id': session.session_id, 'state': session.state, 'job_id': session.parser_job_id or session.request_id}

    def _cmd_cancel_order(self, log, chat_id, username, staff, text):
        session = self._get_active_session(chat_id)
        if not session:
            log.write({'state': 'ignored'})
            self.send_telegram_message(chat_id, 'No active order session to cancel.')
            return {'success': False, 'error': 'No active order session'}
        session.action_cancel()
        log.write({'session_id': session.id})
        self.send_telegram_message(chat_id, 'Order cancelled.')
        return {'success': True, 'message': 'cancelled', 'session_id': session.session_id}

    def _cmd_show_status(self, log, chat_id, username, staff, text):
        # Show user's recent sessions/jobs, isolated to chat_id
        sessions = self.env['telegram.order.session'].sudo().search([
            ('chat_id', '=', str(chat_id)),
        ], order='id desc', limit=5)
        if not sessions:
            log.write({'state': 'ignored'})
            self.send_telegram_message(chat_id, 'No active order session.\nSend /neworder to start one or just send your order.')
            return {'success': True, 'message': 'no session'}
        # Build rich status
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_status_mapper import format_status_list, map_parser_state
            jobs = []
            for s in sessions:
                jobs.append({
                    'ref': s.parser_job_id or s.session_id[:8],
                    'state': s.ai_parser_status or s.state,
                    'order_name': s.sale_order_name,
                })
            msg = format_status_list(jobs)
            # Also include current active details if exists
            active = self._get_active_session(chat_id)
            if active:
                msg = msg + '\n\nCurrent: %s' % active.get_status_message()
        except Exception:
            msg = sessions[0].get_status_message() if sessions else 'No active order session.'
        log.write({'session_id': sessions[0].id if sessions else False})
        self.send_telegram_message(chat_id, msg)
        return {'success': True, 'message': msg, 'session_id': sessions[0].session_id if sessions else False, 'state': sessions[0].state if sessions else False}

    def _cmd_my_orders(self, log, chat_id, username, staff, text):
        # Only orders for this chat/user - isolate
        # Find sale.orders linked via telegram.message or session
        sessions = self.env['telegram.order.session'].sudo().search([
            ('chat_id', '=', str(chat_id)),
            ('sale_order_id', '!=', False),
        ], order='id desc', limit=10)
        if not sessions:
            self.send_telegram_message(chat_id, '\U0001f4cb Your recent orders\n\nNo orders found.')
            return {'success': True, 'message': 'no orders'}
        lines = ['\U0001f4cb Your recent orders', '']
        for s in sessions:
            order = s.sale_order_id
            if not order:
                continue
            # Map state
            state = s.state
            emoji = '\u2705' if state == 'completed' else '\u23f3' if state in ('processing','submitted','retrying') else '\u26a0\ufe0f' if state in ('needs_review','incomplete') else '\u274c'
            label = state.capitalize()
            lines.append('%s \u2014 %s %s' % (order.name or s.sale_order_name, emoji, label))
            # Tally status if available on sale.order
            tally = getattr(order, 'tally_status', None)
            if tally:
                lines[-1] += ' (Tally: %s)' % str(tally)
        msg = '\n'.join(lines)
        self.send_telegram_message(chat_id, msg)
        log.write({'state': 'ignored'})
        return {'success': True, 'message': msg}

    def _cmd_order_lookup(self, log, chat_id, username, staff, text):
        parts = text.strip().split()
        if len(parts) < 2:
            self.send_telegram_message(chat_id, 'Usage: /order <order_number>\nExample: /order SO/2026/00124')
            return {'success': False, 'error': 'Missing order number'}
        order_ref = parts[1].strip()
        # Isolate: only allow lookup if order belongs to this chat's sessions
        # Search sale.order by name
        order = self.env['sale.order'].sudo().search([('name', '=', order_ref)], limit=1)
        if not order:
            self.send_telegram_message(chat_id, 'Order %s not found.' % order_ref)
            return {'success': False, 'error': 'Not found'}
        # Verify ownership via telegram messages/sessions
        linked = self.env['telegram.order.session'].sudo().search([
            ('chat_id', '=', str(chat_id)),
            ('sale_order_id', '=', order.id),
        ], limit=1)
        # Also check telegram.message
        if not linked:
            msg_link = self.env['telegram.message'].sudo().search([
                ('chat_id', '=', str(chat_id)),
                ('order_id', '=', order.id),
            ], limit=1)
            if not msg_link:
                # For security, do not reveal order exists if not owned
                self.send_telegram_message(chat_id, 'Order %s not found or not accessible.' % order_ref)
                return {'success': False, 'error': 'Not authorized'}
        # Build response from Odoo data (authoritative)
        customer = order.partner_id.display_name if order.partner_id else 'N/A'
        state = order.state if hasattr(order, 'state') else 'unknown'
        tally = getattr(order, 'tally_status', None) or getattr(order, 'tally_state', None) or 'N/A'
        if hasattr(order, 'tally_status'):
            tally = order.tally_status or 'N/A'
        lines = [
            'Order: %s' % order.name,
            'Customer: %s' % customer,
            'Status: %s' % state,
            'Tally: %s' % str(tally),
        ]
        msg = '\n'.join(lines)
        self.send_telegram_message(chat_id, msg)
        log.write({'state': 'ignored'})
        return {'success': True, 'message': msg, 'order_id': order.id}

    def _add_text_to_session(self, log, chat_id, username, staff, text, telegram_msg_id):
        # Lightweight validation already done
        session = self._get_active_session(chat_id)
        if not session:
            # For backward compat, keep error. But also handle direct instant order if config enabled.
            # Check if direct mode is implicitly desired: if user sent seemingly valid order text,
            # we could auto-create ephemeral session.
            # To keep tests passing, return error message that matches old expectation.
            log.write({'state': 'ignored'})
            self.send_telegram_message(chat_id, 'No active order session. Send /neworder to start one.')
            return {'success': False, 'error': 'No active order session'}
        sequence = len(session.message_ids) + 1
        self.env['telegram.session.message'].sudo().create({
            'session_id': session.id,
            'message_text': text,
            'telegram_message_id': telegram_msg_id,
            'sequence': sequence,
        })
        session.write({'last_update_id': str(log.update_id)})
        # Update correlation
        if telegram_msg_id:
            existing = session.telegram_message_ids or ''
            if telegram_msg_id not in existing:
                session.write({'telegram_message_ids': (existing + ',' + telegram_msg_id).strip(',')})
        log.write({'session_id': session.id, 'state': 'received'})
        _logger.info('Text added to session %s (seq %d)', session.session_id, sequence)
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import log_event
            log_event('text_added', telegram_chat_id=chat_id, session_id=session.session_id, sequence=sequence)
        except Exception:
            pass
        return {'success': True, 'message': 'text added', 'session_id': session.session_id}

    def _handle_file_message(self, log, chat_id, username, staff, photo, document, telegram_msg_id, caption):
        session = self._get_active_session(chat_id)
        if not session:
            log.write({'state': 'ignored'})
            self.send_telegram_message(chat_id, 'Received a file, but no active order session.\nSend /neworder to start one first.')
            return {'success': False, 'error': 'No active session for file'}
        if photo:
            file_info = photo[-1]
            file_id = file_info.get('file_id', '')
            file_unique_id = file_info.get('file_unique_id', '')
            filename = 'photo_%s.jpg' % file_unique_id
            mime_type = 'image/jpeg'
            file_size = file_info.get('file_size', 0)
        elif document:
            file_info = document
            file_id = document.get('file_id', '')
            file_unique_id = document.get('file_unique_id', '')
            filename = document.get('file_name', 'attachment')
            mime_type = document.get('mime_type', 'application/octet-stream')
            file_size = document.get('file_size', 0)
        else:
            log.write({'state': 'ignored'})
            return {'success': False, 'error': 'No file found'}
        if mime_type not in ALLOWED_MIME_TYPES:
            from odoo.addons.telegram_odoo_integration.models.telegram_error_translator import file_type_error_message
            self.send_telegram_message(chat_id, file_type_error_message(mime_type))
            log.write({'state': 'error', 'error_message': 'Unsupported MIME'})
            try:
                from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc
                inc('telegram_api_errors_total')
            except Exception:
                pass
            return {'success': False, 'error': 'Unsupported MIME type'}
        max_size = self.get_max_file_size_bytes()
        if file_size > max_size:
            max_mb = max_size / (1024 * 1024)
            from odoo.addons.telegram_odoo_integration.models.telegram_error_translator import file_too_large_message
            self.send_telegram_message(chat_id, file_too_large_message(file_size/(1024*1024), int(max_mb)))
            log.write({'state': 'error', 'error_message': 'File too large'})
            return {'success': False, 'error': 'File too large'}
        # Check duplicate file via hash? simple check on file_unique_id
        if file_unique_id:
            existing_att = self.env['telegram.session.attachment'].sudo().search([
                ('session_id', '=', session.id),
                ('telegram_file_unique_id', '=', file_unique_id),
            ], limit=1)
            if existing_att:
                _logger.info('Duplicate file %s for session %s, skipping', file_unique_id, session.session_id)
                self.send_telegram_message(chat_id, 'File already received for this order.')
                return {'success': True, 'message': 'duplicate file', 'session_id': session.session_id}
        try:
            file_path, file_hash = self._download_telegram_file(file_id, filename)
        except Exception as exc:
            _logger.exception('Failed to download file from Telegram')
            from odoo.addons.telegram_odoo_integration.models.telegram_error_translator import translate_error
            friendly = translate_error(str(exc), log_context={'chat_id': chat_id})
            self.send_telegram_message(chat_id, friendly)
            log.write({'state': 'error', 'error_message': str(exc)})
            return {'success': False, 'error': str(exc)}
        if caption:
            seq_text = len(session.message_ids) + 1
            self.env['telegram.session.message'].sudo().create({
                'session_id': session.id,
                'message_text': caption,
                'telegram_message_id': telegram_msg_id,
                'sequence': seq_text,
            })
        seq_att = len(session.attachment_ids) + 1
        self.env['telegram.session.attachment'].sudo().create({
            'session_id': session.id,
            'filename': filename,
            'mime_type': mime_type,
            'file_size': file_size,
            'file_hash': file_hash,
            'file_path': file_path,
            'telegram_file_id': file_id,
            'telegram_file_unique_id': file_unique_id,
            'sequence': seq_att,
        })
        session.write({'last_update_id': str(log.update_id)})
        if telegram_msg_id:
            existing = session.telegram_message_ids or ''
            if telegram_msg_id not in existing:
                session.write({'telegram_message_ids': (existing + ',' + telegram_msg_id).strip(',')})
        log.write({'session_id': session.id, 'state': 'received', 'parser_job_id': session.parser_job_id or False})
        _logger.info('File %s added to session %s', filename, session.session_id)
        try:
            from odoo.addons.telegram_odoo_integration.models.telegram_metrics import log_event
            log_event('file_added', telegram_chat_id=chat_id, session_id=session.session_id, filename=filename)
        except Exception:
            pass
        return {'success': True, 'message': 'file added', 'session_id': session.session_id, 'filename': filename}

    def _download_telegram_file(self, telegram_file_id, original_filename):
        token = self.get_bot_token()
        if not token:
            raise ValueError('Telegram bot token not configured')
        get_file_url = 'https://api.telegram.org/bot%s/getFile' % token
        resp = requests.get(get_file_url, params={'file_id': telegram_file_id}, timeout=30)
        resp.raise_for_status()
        file_data = resp.json()
        if not file_data.get('ok'):
            raise ValueError('Telegram getFile failed: %s' % file_data.get('description'))
        file_path_telegram = file_data['result']['file_path']
        download_url = 'https://api.telegram.org/file/bot%s/%s' % (token, file_path_telegram)
        file_resp = requests.get(download_url, timeout=60)
        file_resp.raise_for_status()
        ext = os.path.splitext(original_filename)[1] or '.bin'
        secure_name = '%s%s' % (uuid.uuid4().hex, ext)
        temp_dir = tempfile.mkdtemp(prefix='tg_attachments_')
        file_path = os.path.join(temp_dir, secure_name)
        with open(file_path, 'wb') as fh:
            fh.write(file_resp.content)
        sha256 = hashlib.sha256(file_resp.content).hexdigest()
        return file_path, 'sha256:%s' % sha256

    def _get_active_session(self, chat_id):
        Session = self.env['telegram.order.session'].sudo()
        session = Session.search([('chat_id', '=', str(chat_id)), ('state', 'in', ('new', 'collecting'))], order='id desc', limit=1)
        return session if session else None

    def send_telegram_message(self, chat_id, text):
        token = self.get_bot_token()
        if not token:
            _logger.error('Telegram bot token not configured (%s); cannot send reply', CONFIG_BOT_TOKEN)
            return None
        if not chat_id:
            _logger.warning('No chat id available to send Telegram reply')
            return None
        # Handle editing vs new message: for now always new, but could implement edit if message_id provided
        url = 'https://api.telegram.org/bot%s/sendMessage' % token
        try:
            response = requests.post(url, json={'chat_id': chat_id, 'text': text}, timeout=10)
            response.raise_for_status()
            _logger.debug('Telegram reply sent to chat %s: %s', chat_id, text[:100])
            return response.json()
        except requests.RequestException as exc:
            _logger.error('Failed to send Telegram reply to chat %s: %s', chat_id, exc)
            try:
                from odoo.addons.telegram_odoo_integration.models.telegram_metrics import inc
                inc('telegram_api_errors_total')
            except Exception:
                pass
            return None

    def edit_telegram_message(self, chat_id, message_id, text):
        token = self.get_bot_token()
        if not token or not chat_id or not message_id:
            return self.send_telegram_message(chat_id, text)
        url = 'https://api.telegram.org/bot%s/editMessageText' % token
        try:
            response = requests.post(url, json={'chat_id': chat_id, 'message_id': int(message_id), 'text': text}, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            _logger.warning('Failed to edit message %s, sending new: %s', message_id, exc)
            return self.send_telegram_message(chat_id, text)
