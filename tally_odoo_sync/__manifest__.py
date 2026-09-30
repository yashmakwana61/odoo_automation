# -*- coding: utf-8 -*-
{
    'name': 'Tally Odoo Sync',
    'version': '1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'Push posted customer invoices from Odoo to Tally ERP as Sales Vouchers',
    'description': """
Tally Odoo Sync
===============

Synchronize posted customer invoices (``account.move`` / ``move_type = out_invoice``)
from Odoo to Tally ERP over the Tally HTTP XML API. Odoo stays the source of truth
for invoicing; Tally is used as the accounting backend and receives a GST-compliant
Sales Voucher for every posted invoice.

Main features
-------------
* Automatic push on ``action_post()`` (only for ``out_invoice``).
* Configurable Tally endpoint and credentials per company.
* GST compliant Sales Voucher XML (CGST / SGST / IGST / CESS classification).
* Optional auto-creation of missing ledgers in Tally.
* Full traceability stored on the invoice: sync status, Tally voucher id,
  voucher GUID, raw response and last sync date.
* Chatter logging for success and failure with reason.
* Retry cron for failed records plus a manual "Sync to Tally" button.
* Duplicate protection through a deterministic Tally GUID.
* Comprehensive error handling: connection timeout, invalid XML, ledger
  missing, duplicate entry.
    """,
    'author': 'Odoo Technical Architect',
    'website': 'https://example.com',
    'depends': ['account', 'l10n_in'],
    # No ir.model.access rows are required: the module only extends existing
    # models (account.move, res.company, res.config.settings) and defines one
    # AbstractModel (tally.service) which has no DB table.
    'data': [
        'security/ir.model.access.csv',
        'data/cron.xml',
        'data/sequence.xml',
        'views/tally_mapping_views.xml',
        'views/tally_sync_log_views.xml',
        'views/account_move_views.xml',
        'views/res_config_settings_views.xml',
        'views/tally_preview_wizard_views.xml',
        'views/tally_import_views.xml',
        'views/tally_master_identity_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
