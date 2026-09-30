# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    """Tally identity on partners (master-importer foundation).

    GUID is the permanent external identity; name is never used as key.
    """

    _inherit = 'res.partner'

    tally_guid = fields.Char(
        string='Tally GUID', copy=False, index=True,
        help="Permanent Tally identity (GUID from Tally master).",
    )
    tally_alter_id = fields.Char(
        string='Tally Alter ID', copy=False, index=True,
        help="Tally AlterID (version stamp) for reconciliation.",
    )
    tally_parent = fields.Char(
        string='Tally Parent Group',
        help="Tally parent/group name at import time (traceability).",
    )
    tally_classification = fields.Selection(
        selection=[
            ('customer', 'Customer'),
            ('vendor', 'Vendor'),
            ('bank', 'Bank'),
            ('cash', 'Cash'),
            ('tax', 'Tax'),
            ('income', 'Income'),
            ('expense', 'Expense'),
            ('asset', 'Asset'),
            ('liability', 'Liability'),
            ('equity', 'Equity'),
            ('accounting', 'Accounting'),
            ('other', 'Other'),
        ],
        string='Tally Classification', index=True,
        help="How this partner was classified during Tally master import.",
    )
    tally_last_batch_id = fields.Many2one(
        'tally.import.batch', string='Last Tally Import Batch',
        copy=False, readonly=True,
        help="Batch that last created/updated this partner.",
    )

    _tally_guid_company_uniq = models.Constraint(
        'UNIQUE(tally_guid, company_id)',
        'A partner with this Tally GUID already exists for this company.',
    )
