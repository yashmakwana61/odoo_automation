# -*- coding: utf-8 -*-
"""Configurable Tally group -> classification mapping rules."""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.tally_odoo_sync.services import tally_master_utils as utils

_logger = logging.getLogger(__name__)


class TallyGroupMapping(models.Model):
    _name = 'tally.group.mapping'
    _description = 'Tally Group Classification Rule'
    _order = 'priority, id'

    CLASSIFICATIONS = [
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
    ]

    tally_group_name = fields.Char(
        string='Tally Group', required=True, index=True,
        help="Exact Tally group name this rule was derived from (documentation).",
    )
    pattern = fields.Char(
        string='Match Pattern', required=True, index=True,
        help="fnmatch pattern against the Tally parent/group (case-insensitive, * supported).",
    )
    classification = fields.Selection(
        selection=CLASSIFICATIONS, required=True, index=True,
        string='Classification',
    )
    odoo_model = fields.Char(
        string='Odoo Model', default='res.partner',
        help="Target Odoo model for this classification (informational).",
    )
    priority = fields.Integer(
        default=10,
        help="Lower runs first. Exact matches should use low priority.",
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Company', index=True,
        help="Empty = global rule for all companies.",
    )
    notes = fields.Text()

    _pattern_company_uniq = models.Constraint(
        'UNIQUE(pattern, company_id)',
        'A rule with this pattern already exists for this company scope.',
    )

    @api.constrains('pattern')
    def _check_pattern(self):
        for rec in self:
            if not (rec.pattern or '').strip():
                raise ValidationError(_('Match Pattern must not be empty.'))

    @api.model
    def classify(self, tally_parent, company=None):
        """Return (classification, rule) for a Tally parent/group name.

        Falls back to ('other', None) when nothing matches.
        """
        parent = (tally_parent or '').strip()
        domain = ['|', ('company_id', '=', False)]
        if company and company.id:
            domain.append(('company_id', '=', company.id))
        else:
            domain.append(('company_id', '=', False))
        rules = self.search([('active', '=', True)] + domain, order='priority, id')
        for rule in rules:
            if utils.match_pattern(rule.pattern, parent):
                return rule.classification, rule
        # Built-in fallback table (used when no rule configured yet).
        fallback = self._fallback_classification(parent)
        return fallback, None

    @staticmethod
    def _fallback_classification(parent):
        upper = (parent or '').upper().strip()
        if not upper:
            return 'other'
        if upper in ('SUNDRY DEBTORS',) or upper.startswith('DEBTOR'):
            return 'customer'
        if upper in ('SUNDRY CREDITORS',) or 'CREDITOR' in upper:
            return 'vendor'
        if 'BANK' in upper:
            return 'bank'
        if 'CASH' in upper:
            return 'cash'
        if any(k in upper for k in ('DUTIES', 'TAX', 'GST', 'VAT', 'TDS', 'TCS')):
            return 'tax'
        if 'SALES' in upper:
            return 'income'
        if 'PURCHASE' in upper or 'EXPENSE' in upper:
            return 'expense'
        if any(k in upper for k in ('CAPITAL', 'RESERVE', 'EQUITY', 'SHARE')):
            return 'equity'
        if any(k in upper for k in ('LOAN', 'LIABILIT', 'PAYABLE', 'CREDITOR')):
            return 'liability'
        if any(k in upper for k in ('ASSET', 'FIXED', 'RECEIVABLE', 'DEBTOR', 'DEPOSIT')):
            return 'asset'
        return 'accounting' if upper else 'other'

    @api.model
    def ensure_defaults(self, company=None):
        """Seed the standard rule set (idempotent, global scope)."""
        defaults = [
            # (group, pattern, classification, priority, notes)
            ('Sundry Debtors', 'Sundry Debtors', 'customer', 10, 'Tally default customer group'),
            ('Debtors wildcard', 'DEBTOR*', 'customer', 11, 'Custom debtor groups'),
            ('Sundry Creditors', 'Sundry Creditors', 'vendor', 10, 'Tally default vendor group'),
            ('Creditors wildcard', '*CREDITOR*', 'vendor', 11, 'Custom creditor groups'),
            ('Bank Accounts', 'Bank*', 'bank', 20, ''),
            ('Cash-in-Hand', 'Cash*', 'cash', 20, ''),
            ('Duties & Taxes', 'Duties*', 'tax', 20, ''),
            ('Duties & Taxes', '*Tax*', 'tax', 21, ''),
            ('Sales Accounts', 'Sales*', 'income', 30, ''),
            ('Purchase Accounts', 'Purchase*', 'expense', 30, ''),
            ('Direct Expenses', 'Direct*', 'expense', 31, ''),
            ('Indirect Expenses', 'Indirect*', 'expense', 31, ''),
            ('Capital Account', 'Capital*', 'equity', 40, ''),
            ('Loans', '*Loan*', 'liability', 40, ''),
            ('Current Liabilities', '*Liabilit*', 'liability', 41, ''),
            ('Fixed Assets', '*Asset*', 'asset', 40, ''),
        ]
        created = 0
        for group, pattern, classification, priority, notes in defaults:
            existing = self.search([
                ('pattern', '=', pattern),
                ('company_id', '=', False),
            ], limit=1)
            if existing:
                continue
            self.create({
                'tally_group_name': group,
                'pattern': pattern,
                'classification': classification,
                'priority': priority,
                'notes': notes,
            })
            created += 1
        return created
