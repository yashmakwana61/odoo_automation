{
    'name': 'Telegram Odoo Integration',
    'version': '19.0.2.0.0',
    'category': 'Sales/Sales',
    'summary': 'Collect orders via Telegram and submit to AI Parser for Sales Order creation',
    'description': """
Telegram Odoo Integration
=========================

Connect a Telegram bot to Odoo through a webhook and collect customer
orders from internal staff via a session-based flow. Order packages are
submitted to an independent AI Parser application for processing.

Features
--------
* Public JSON webhook endpoint ``/telegram/webhook`` for Telegram updates.
* Session-based order collection: staff can send multiple text messages,
  images, PDFs, and Excel files belonging to a single order.
* Bot commands: ``/neworder``, ``/done``, ``/cancel``, ``/status``.
* Secure attachment serving to the AI Parser via time-limited tokens.
* Idempotent update processing (duplicate Telegram updates are ignored).
* Configurable bot token, allowed chat ID, AI Parser URL, and limits.
* Full Telegram chat history stored in ``telegram.message``.
* Order session tracking in ``telegram.order.session``.
* Audit logging of every update and JSON error responses.

AI Parser Integration
---------------------
The AI Parser is a **separate independent application**. This module
communicates with it via a defined API contract. No AI/OCR logic is
implemented within this module.

Compatibility: Odoo 19 Community Edition.
""",
    'author': 'Precisefect Solutions Pvt. Ltd.',
    'website': 'https://precisefect.com',
    'license': 'OPL-1',
    'depends': [
        'base',
        'sale',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/telegram_session_views.xml',
        'views/telegram_message_views.xml',
        'views/sale_order_views.xml',
        'data/ir_cron_data.xml',
    ],
    'demo': [],
    'installable': True,
    'application': False,
    'auto_install': False,
}
