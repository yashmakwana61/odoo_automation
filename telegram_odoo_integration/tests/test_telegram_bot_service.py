"""Integration tests for the Telegram bot service (session-based flow).

The outbound Telegram HTTP call is mocked so tests never hit the real API.
The AI Parser client is mocked to return configurable responses.
"""
from unittest.mock import patch, MagicMock

from odoo.tests import TransactionCase, tagged

from odoo.addons.telegram_odoo_integration.models.telegram_bot_service import (
    CONFIG_ALLOWED_CHAT_ID,
    CONFIG_BOT_TOKEN,
    TelegramBotService,
)


@tagged('post_install', '-at_install')
class TestTelegramBotService(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].set_param(CONFIG_BOT_TOKEN, 'TEST_TOKEN')
        self.env['ir.config_parameter'].set_param(CONFIG_ALLOWED_CHAT_ID, '123456')
        self.env['ir.config_parameter'].set_param(
            'telegram_odoo_integration.ai_parser_url', 'http://ai-parser.local')
        self.env['ir.config_parameter'].set_param(
            'telegram_odoo_integration.ai_parser_api_key', 'test-api-key')
        self.env['ir.config_parameter'].set_param(
            'telegram_odoo_integration.max_file_size_mb', '10')

    def _mock_telegram_api(self):
        patcher = patch(
            'odoo.addons.telegram_odoo_integration.models.'
            'telegram_bot_service.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'ok': True}
        return mock_post

    def _mock_ai_parser(self, response=None):
        if response is None:
            response = {'status': 'SUCCESS', 'sale_order_id': 1,
                        'sale_order_name': 'SO00001'}
        patcher = patch(
            'odoo.addons.telegram_odoo_integration.models.'
            'ai_parser_client.requests.post')
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = response
        mock_post.return_value.raise_for_status = MagicMock()
        return mock_post

    def _build_update(self, update_id, text='', chat_id=123456,
                      username='john_doe', user_id=42, msg_id=1):
        return {
            'update_id': update_id,
            'message': {
                'message_id': msg_id,
                'chat': {'id': chat_id},
                'from': {'id': user_id, 'username': username},
                'text': text,
            },
        }

    # ------------------------------------------------------------------ #
    # Session lifecycle tests
    # ------------------------------------------------------------------ #
    def test_neworder_creates_session(self):
        self._mock_telegram_api()
        update = self._build_update(1001, '/neworder')
        result = TelegramBotService(self.env).process_update(update)

        self.assertTrue(result['success'])
        self.assertIn('session_id', result)

        session = self.env['telegram.order.session'].search(
            [('session_id', '=', result['session_id'])], limit=1)
        self.assertTrue(session)
        self.assertEqual(session.state, 'collecting')
        self.assertEqual(session.chat_id, '123456')
        self.assertEqual(session.telegram_username, 'john_doe')

    def test_multiple_text_messages_collected(self):
        self._mock_telegram_api()
        # Start session.
        TelegramBotService(self.env).process_update(
            self._build_update(2001, '/neworder'))
        # Send texts.
        TelegramBotService(self.env).process_update(
            self._build_update(2002, 'ABC Industries'))
        result = TelegramBotService(self.env).process_update(
            self._build_update(2003, 'White Bread 400 GMS - 20'))

        self.assertTrue(result['success'])
        session = self.env['telegram.order.session'].search(
            [('chat_id', '=', '123456'),
             ('state', '=', 'collecting')], limit=1)
        self.assertEqual(len(session.message_ids), 2)
        self.assertEqual(session.message_ids[0].message_text, 'ABC Industries')
        self.assertEqual(session.message_ids[1].message_text,
                         'White Bread 400 GMS - 20')

    def test_text_without_session_shows_error(self):
        self._mock_telegram_api()
        update = self._build_update(3001, 'Some text')
        result = TelegramBotService(self.env).process_update(update)
        self.assertFalse(result['success'])
        self.assertIn('No active order session', result['error'])

    def test_done_submits_to_ai_parser(self):
        self._mock_telegram_api()
        self._mock_ai_parser()
        # Start session and add text.
        TelegramBotService(self.env).process_update(
            self._build_update(4001, '/neworder'))
        TelegramBotService(self.env).process_update(
            self._build_update(4002, 'Customer: ABC Corp'))
        result = TelegramBotService(self.env).process_update(
            self._build_update(4003, '/done'))

        self.assertTrue(result['success'])
        self.assertEqual(result['state'], 'completed')

    def test_done_empty_session_rejected(self):
        self._mock_telegram_api()
        TelegramBotService(self.env).process_update(
            self._build_update(5001, '/neworder'))
        result = TelegramBotService(self.env).process_update(
            self._build_update(5002, '/done'))
        self.assertFalse(result['success'])
        self.assertIn('No messages', result['error'])

    def test_cancel_discards_session(self):
        self._mock_telegram_api()
        TelegramBotService(self.env).process_update(
            self._build_update(6001, '/neworder'))
        result = TelegramBotService(self.env).process_update(
            self._build_update(6002, '/cancel'))
        self.assertTrue(result['success'])
        self.assertEqual(result['message'], 'cancelled')

        session = self.env['telegram.order.session'].search(
            [('chat_id', '=', '123456')], limit=1)
        self.assertEqual(session.state, 'cancelled')

    def test_status_shows_session_info(self):
        self._mock_telegram_api()
        TelegramBotService(self.env).process_update(
            self._build_update(7001, '/neworder'))
        result = TelegramBotService(self.env).process_update(
            self._build_update(7002, '/status'))
        self.assertTrue(result['success'])
        self.assertIn('Collecting', result['message'])

    def test_duplicate_update_ignored(self):
        self._mock_telegram_api()
        update = self._build_update(8001, '/neworder')
        result1 = TelegramBotService(self.env).process_update(update)
        result2 = TelegramBotService(self.env).process_update(update)
        self.assertTrue(result2.get('duplicate'))
        # Only one session created.
        sessions = self.env['telegram.order.session'].search(
            [('chat_id', '=', '123456')])
        self.assertEqual(len(sessions), 1)

    def test_only_one_active_session_per_chat(self):
        self._mock_telegram_api()
        TelegramBotService(self.env).process_update(
            self._build_update(9001, '/neworder'))
        result = TelegramBotService(self.env).process_update(
            self._build_update(9002, '/neworder'))
        # Should report existing session, not create a new one.
        self.assertIn('session_id', result)
        sessions = self.env['telegram.order.session'].search(
            [('chat_id', '=', '123456'),
             ('state', 'in', ('new', 'collecting'))])
        self.assertEqual(len(sessions), 1)

    # ------------------------------------------------------------------ #
    # Security tests
    # ------------------------------------------------------------------ #
    def test_unauthorized_chat_is_rejected(self):
        mock_post = self._mock_telegram_api()
        update = self._build_update(
            10001, '/neworder', chat_id=999999, username='intruder')
        result = TelegramBotService(self.env).process_update(update)

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'Unauthorized chat id')
        mock_post.assert_called_once()

    def test_non_text_message_without_session_ignored(self):
        self._mock_telegram_api()
        update = {
            'update_id': 10002,
            'message': {
                'message_id': 1,
                'chat': {'id': 123456},
                'from': {'id': 42, 'username': 'john_doe'},
                'sticker': {'file_id': 'abc', 'file_unique_id': 'def'},
            },
        }
        result = TelegramBotService(self.env).process_update(update)
        self.assertFalse(result['success'])

    # ------------------------------------------------------------------ #
    # Help command
    # ------------------------------------------------------------------ #
    def test_help_command(self):
        mock_post = self._mock_telegram_api()
        update = self._build_update(11001, '/help')
        result = TelegramBotService(self.env).process_update(update)
        self.assertTrue(result['success'])
        self.assertTrue(result.get('ignored'))
        sent_text = mock_post.call_args.kwargs['json']['text']
        self.assertIn('/neworder', sent_text)
        self.assertIn('/done', sent_text)

    def test_start_command(self):
        self._mock_telegram_api()
        update = self._build_update(11002, '/start')
        result = TelegramBotService(self.env).process_update(update)
        self.assertTrue(result['success'])
        self.assertTrue(result.get('ignored'))

    # ------------------------------------------------------------------ #
    # Retry after failure
    # ------------------------------------------------------------------ #
    def test_retry_after_ai_parser_failure(self):
        self._mock_telegram_api()
        self._mock_ai_parser(response={'status': 'FAILED',
                                       'message': 'Something went wrong'})
        # Create session, add text, submit.
        TelegramBotService(self.env).process_update(
            self._build_update(12001, '/neworder'))
        TelegramBotService(self.env).process_update(
            self._build_update(12002, 'Order details'))
        TelegramBotService(self.env).process_update(
            self._build_update(12003, '/done'))

        session = self.env['telegram.order.session'].search(
            [('chat_id', '=', '123456')], limit=1)
        self.assertEqual(session.state, 'failed')

        # Retry.
        result = session.action_retry()
        self.assertTrue(result)
        self.assertEqual(session.state, 'collecting')
