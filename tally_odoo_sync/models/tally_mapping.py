# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class TallyMapping(models.Model):
    """Deterministic Odoo-to-Tally entity mapping.

    Every Odoo entity that needs a Tally equivalent MUST have an explicit
    mapping record.  Synchronization is blocked when a mapping is missing.
    """

    _name = 'tally.mapping'
    _description = 'Tally Entity Mapping'
    _rec_name = 'display_name'
    _order = 'mapping_type, sequence, id'

    MAPPING_TYPES = [
        ('customer', 'Customer → Tally Ledger'),
        ('product', 'Product → Tally Stock Item'),
        ('tax', 'Tax → Tally Tax Ledger'),
        ('uom', 'UOM → Tally Unit'),
    ]

    mapping_type = fields.Selection(
        selection=MAPPING_TYPES,
        string='Mapping Type',
        required=True,
        index=True,
    )
    sequence = fields.Integer(default=10)

    # -- Source (Odoo) --------------------------------------------------------
    odoo_partner_id = fields.Many2one(
        'res.partner', string='Customer', index=True,
        ondelete='cascade',
        help="Odoo customer/partner to map.",
    )
    odoo_product_id = fields.Many2one(
        'product.product', string='Product', index=True,
        ondelete='cascade',
        help="Odoo product to map.",
    )
    odoo_tax_id = fields.Many2one(
        'account.tax', string='Tax', index=True,
        ondelete='cascade',
        help="Odoo tax to map.",
    )
    odoo_uom_id = fields.Many2one(
        'uom.uom', string='UOM', index=True,
        ondelete='cascade',
        help="Odoo Unit of Measure to map.",
    )

    # -- Target (Tally) -------------------------------------------------------
    tally_name = fields.Char(
        string='Tally Name',
        required=True,
        help="Exact name of the entity in Tally (ledger, stock item, unit, etc.).",
    )
    # -- Canonical Tally identity (master-importer layer) ----------------------
    # The GUID is the permanent external identity; the name is display only.
    tally_guid = fields.Char(
        string='Tally GUID', index=True, copy=False,
        help="Permanent Tally GUID from the master import. Used for idempotent "
             "matching and invoice-sync resolution.",
    )
    tally_alter_id = fields.Char(
        string='Tally Alter ID', index=True, copy=False,
        help="Tally AlterID version stamp at mapping time.",
    )
    tally_entity_type = fields.Selection(
        selection=[
            ('ledger', 'Ledger'),
            ('group', 'Group'),
            ('currency', 'Currency'),
            ('stock_item', 'Stock Item'),
            ('unit', 'Unit'),
            ('other', 'Other'),
        ],
        string='Tally Entity Type', default='ledger', index=True,
    )
    match_method = fields.Selection(
        selection=[
            ('manual', 'Manual'),
            ('guid', 'Tally GUID'),
            ('mapping', 'Existing Mapping'),
            ('gstin', 'GSTIN'),
            ('stable_id', 'Stable Identifier'),
            ('exact_name', 'Exact Name'),
            ('name_state', 'Name + State'),
            ('name_gstin', 'Name + GSTIN'),
            ('fuzzy', 'Fuzzy Candidate'),
            ('import', 'Master Import'),
        ],
        string='Match Method', default='manual',
    )
    confidence = fields.Float(string='Confidence %', default=100.0)
    source_batch_id = fields.Many2one(
        'tally.import.batch', string='Source Import Batch',
        ondelete='set null', index=True, copy=False,
        help="Batch that created/last verified this mapping.",
    )
    last_verified_at = fields.Datetime(string='Last Verified At', readonly=True)

    # -- Scope ----------------------------------------------------------------
    company_id = fields.Many2one(
        'res.company', string='Company', index=True,
        default=lambda self: self.env.company,
        required=True,
        help="Company scope for this mapping.",
    )
    active = fields.Boolean(default=True)
    notes = fields.Text()

    # -- Computed display ------------------------------------------------------
    display_name = fields.Char(
        compute='_compute_display_name', store=True,
    )

    @api.depends('mapping_type', 'odoo_partner_id', 'odoo_product_id',
                 'odoo_tax_id', 'odoo_uom_id', 'tally_name')
    def _compute_display_name(self):
        for rec in self:
            source = ''
            if rec.mapping_type == 'customer':
                source = rec.odoo_partner_id.name or ''
            elif rec.mapping_type == 'product':
                source = rec.odoo_product_id.display_name or ''
            elif rec.mapping_type == 'tax':
                source = rec.odoo_tax_id.name or ''
            elif rec.mapping_type == 'uom':
                source = rec.odoo_uom_id.name or ''
            rec.display_name = '%s → %s' % (source, rec.tally_name) if source else rec.tally_name

    # -- Constraints -----------------------------------------------------------
    @api.constrains('mapping_type', 'odoo_partner_id', 'odoo_product_id',
                    'odoo_tax_id', 'odoo_uom_id', 'company_id')
    def _check_unique_mapping(self):
        for rec in self:
            domain = [
                ('mapping_type', '=', rec.mapping_type),
                ('company_id', '=', rec.company_id.id),
                ('id', '!=', rec.id),
            ]
            if rec.mapping_type == 'customer' and rec.odoo_partner_id:
                domain.append(('odoo_partner_id', '=', rec.odoo_partner_id.id))
            elif rec.mapping_type == 'product' and rec.odoo_product_id:
                domain.append(('odoo_product_id', '=', rec.odoo_product_id.id))
            elif rec.mapping_type == 'tax' and rec.odoo_tax_id:
                domain.append(('odoo_tax_id', '=', rec.odoo_tax_id.id))
            elif rec.mapping_type == 'uom' and rec.odoo_uom_id:
                domain.append(('odoo_uom_id', '=', rec.odoo_uom_id.id))
            else:
                continue
            if self.search_count(domain):
                raise ValidationError(_(
                    'A mapping of type "%s" already exists for this entity '
                    'in company %s.', rec.mapping_type, rec.company_id.name,
                ))

    @api.constrains('tally_name')
    def _check_tally_name(self):
        for rec in self:
            if rec.tally_name and rec.tally_name != rec.tally_name.strip():
                raise ValidationError(_('Tally Name must not have leading/trailing spaces.'))

    _tally_guid_company_uniq = models.Constraint(
        'UNIQUE(tally_guid, company_id)',
        'A mapping with this Tally GUID already exists for this company.',
    )

    # -- ORM defaults ----------------------------------------------------------
    @api.model
    def _default_company(self):
        return self.env.company

    # -- Search helpers --------------------------------------------------------
    # Canonical resolution order: GUID -> mapping -> GSTIN/name.
    # These methods are the SINGLE lookup mechanism consumed by both the
    # master importer and the invoice -> Tally XML sync (no duplicate logic).
    @api.model
    def resolve_by_guid(self, tally_guid, company=None):
        """Return the mapping record for a Tally GUID, or an empty recordset."""
        if not tally_guid:
            return self.browse()
        domain = [('tally_guid', '=', str(tally_guid).strip()), ('active', '=', True)]
        if company:
            domain.append(('company_id', '=', company.id))
        return self.search(domain, limit=1)

    @api.model
    def resolve_partner_by_guid(self, tally_guid, company=None):
        rec = self.resolve_by_guid(tally_guid, company)
        if rec and rec.mapping_type == 'customer' and rec.odoo_partner_id:
            return rec.odoo_partner_id
        return None

    @api.model
    def resolve_customer(self, partner, company=None):
        """Return the Tally ledger name for *partner*, or ``None``."""
        company = company or self.env.company
        # Prefer the GUID-carrying canonical row for this partner.
        rec = self.search([
            ('mapping_type', '=', 'customer'),
            ('odoo_partner_id', '=', partner.id),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ], limit=1)
        if rec:
            return rec.tally_name
        # Fallback: partner imported with GUID but mapping scoped elsewhere.
        if getattr(partner, 'tally_guid', None):
            by_guid = self.resolve_by_guid(partner.tally_guid, None)
            if by_guid:
                return by_guid.tally_name
        return None

    @api.model
    def resolve_tally_identity(self, partner, company=None):
        """Return canonical Tally identity dict for a partner.

        Used by invoice sync: ``{'name', 'guid', 'alter_id'}``.
        """
        company = company or self.env.company
        rec = self.search([
            ('mapping_type', '=', 'customer'),
            ('odoo_partner_id', '=', partner.id),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ], limit=1)
        if rec:
            return {'name': rec.tally_name, 'guid': rec.tally_guid or False,
                    'alter_id': rec.tally_alter_id or False}
        if getattr(partner, 'tally_guid', None):
            return {'name': partner.name, 'guid': partner.tally_guid,
                    'alter_id': getattr(partner, 'tally_alter_id', False)}
        return {'name': None, 'guid': None, 'alter_id': None}

    @api.model
    def resolve_vendor(self, partner, company=None):
        """Vendors share the customer mapping table (supplier_rank on partner)."""
        return self.resolve_customer(partner, company)

    @api.model
    def resolve_account(self, partner_or_name, company=None):
        """Resolve an accounting ledger name (partner-backed or literal)."""
        if hasattr(partner_or_name, 'id') and getattr(partner_or_name, '_name', '') == 'res.partner':
            return self.resolve_customer(partner_or_name, company)
        return str(partner_or_name) if partner_or_name else None

    @api.model
    def resolve_product(self, product, company=None):
        """Return the Tally stock item name for *product*, or ``None``."""
        company = company or self.env.company
        rec = self.search([
            ('mapping_type', '=', 'product'),
            ('odoo_product_id', '=', product.id),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ], limit=1)
        return rec.tally_name if rec else None

    @api.model
    def resolve_tax(self, tax, company=None):
        """Return the Tally tax ledger name for *tax*, or ``None``."""
        company = company or self.env.company
        rec = self.search([
            ('mapping_type', '=', 'tax'),
            ('odoo_tax_id', '=', tax.id),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ], limit=1)
        return rec.tally_name if rec else None

    @api.model
    def resolve_uom(self, uom, company=None):
        """Return the Tally unit name for *uom*, or ``None``."""
        company = company or self.env.company
        rec = self.search([
            ('mapping_type', '=', 'uom'),
            ('odoo_uom_id', '=', uom.id),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ], limit=1)
        return rec.tally_name if rec else None

    # -- Bulk resolution -------------------------------------------------------
    @api.model
    def resolve_all(self, invoice):
        """Resolve all required mappings for an invoice.

        Returns a dict:
            {
                'customer': {'name': str, 'partner_id': record, 'missing': bool},
                'products': [{name, product_id, missing}, ...],
                'taxes': [{name, tax_id, missing}, ...],
                'uoms': [{name, uom_id, missing}, ...],
                'all_resolved': bool,
                'missing': [str, ...],
            }
        """
        company = invoice.company_id
        partner = invoice.commercial_partner_id
        result = {
            'customer': {'name': None, 'partner_id': partner, 'missing': True},
            'products': [],
            'taxes': [],
            'uoms': [],
            'all_resolved': True,
            'missing': [],
        }

        # Customer
        customer_name = self.resolve_customer(partner, company)
        if customer_name:
            result['customer'] = {'name': customer_name, 'partner_id': partner, 'missing': False}
        else:
            result['missing'].append('Customer ledger mapping for "%s"' % partner.name)

        # Products, taxes, UOMs from invoice lines
        seen_taxes = set()
        seen_uoms = set()
        for line in invoice.invoice_line_ids:
            if line.display_type in ('line_section', 'line_note'):
                continue

            # Product
            if line.product_id:
                pname = self.resolve_product(line.product_id, company)
                if pname:
                    result['products'].append({
                        'name': pname, 'product_id': line.product_id, 'missing': False,
                    })
                else:
                    result['products'].append({
                        'name': None, 'product_id': line.product_id, 'missing': True,
                    })
                    result['missing'].append(
                        'Tally Stock Item mapping for "%s"' % line.product_id.display_name)

            # Tax
            for tax in line.tax_ids:
                if tax.id in seen_taxes:
                    continue
                seen_taxes.add(tax.id)
                tname = self.resolve_tax(tax, company)
                if tname:
                    result['taxes'].append({
                        'name': tname, 'tax_id': tax, 'missing': False,
                    })
                else:
                    result['taxes'].append({
                        'name': None, 'tax_id': tax, 'missing': True,
                    })
                    result['missing'].append(
                        'Tally Tax Ledger mapping for "%s"' % tax.name)

            # UOM
            if line.product_uom_id:
                uid = line.product_uom_id.id
                if uid not in seen_uoms:
                    seen_uoms.add(uid)
                    uname = self.resolve_uom(line.product_uom_id, company)
                    if uname:
                        result['uoms'].append({
                            'name': uname, 'uom_id': line.product_uom_id, 'missing': False,
                        })
                    else:
                        result['uoms'].append({
                            'name': None, 'uom_id': line.product_uom_id, 'missing': True,
                        })
                        result['missing'].append(
                            'Tally UOM mapping for "%s"' % line.product_uom_id.name)

        result['all_resolved'] = not result['missing']
        return result
