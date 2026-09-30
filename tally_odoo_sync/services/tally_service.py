# -*- coding: utf-8 -*-
"""Tally service: XML generation from validated payload + HTTP transport.

Architecture:
    Odoo Invoice
        -> tally.payload.build_payload() -> validated TallyInvoicePayload dict
        -> tally.payload.validate_invoice_for_tally() -> validation result
        -> tally.service.generate_xml(payload) -> XML string
        -> tally.service.send_to_tally(xml, company) -> response string
        -> tally.service.parse_response(response) -> result dict

The XML generator ONLY transforms the payload to XML. It does NOT:
- resolve customers or products
- calculate taxes
- guess mappings
- retrieve random data
- modify Odoo records
"""
import hashlib
import logging
import uuid
import xml.etree.ElementTree as ET

import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# XML ids of the l10n_in tax tags used to classify GST taxes.
_TAX_TAG_XMLIDS = {
    'CGST': 'l10n_in.tax_tag_cgst',
    'SGST': 'l10n_in.tax_tag_sgst',
    'IGST': 'l10n_in.tax_tag_igst',
    'CESS': 'l10n_in.tax_tag_cess',
}

_TAX_TYPES = ('CGST', 'SGST', 'IGST', 'CESS')

_GST_DUTY_HEAD = {
    'CGST': 'CGST',
    'SGST': 'SGST',
    'IGST': 'IGST',
    'CESS': 'CESS',
    'OTHER': 'GST',
}

# Tolerance for balance validation (floating point)
_VOUCHER_BALANCE_TOLERANCE = 0.02


class TallyService(models.AbstractModel):
    """Stateless service layer: XML generation + HTTP transport + response parsing."""

    _name = 'tally.service'
    _description = 'Tally Integration Service Layer'

    # ==================================================================
    # Public API: XML generation from payload
    # ==================================================================
    def generate_xml(self, payload):
        """Build the Tally ``Import Data`` envelope from a validated payload dict.

        :param payload: dict produced by ``tally_payload.build_payload()``
        :return: XML string
        :rtype: str
        """
        company = payload['company']
        buyer = payload['buyer']
        invoice_num = payload['invoice_number']
        invoice_date = payload['invoice_date']
        tally_company = company['company_name']
        guid = payload.get('tally_voucher_guid', self._deterministic_guid(
            payload.get('invoice_id', 0), company.get('_company_id', 0)))
        nature = payload.get('nature_of_transaction', 'Intra-State')

        # --- Accounting balance validation before generation ---
        balance = self.validate_voucher_balance(payload)
        if not balance['balanced']:
            _logger.warning("Voucher balance validation failed for %s: %s", invoice_num, balance['message'])
            raise UserError(_('Voucher is not balanced: %s', balance['message']))

        root = ET.Element('ENVELOPE')
        header = ET.SubElement(root, 'HEADER')
        ET.SubElement(header, 'TALLYREQUEST').text = 'Import Data'
        body = ET.SubElement(root, 'BODY')
        importdata = ET.SubElement(body, 'IMPORTDATA')
        requestdesc = ET.SubElement(importdata, 'REQUESTDESC')
        ET.SubElement(requestdesc, 'REPORTNAME').text = 'Vouchers'
        staticvars = ET.SubElement(requestdesc, 'STATICVARIABLES')
        ET.SubElement(staticvars, 'SVCURRENTCOMPANY').text = tally_company
        requestdata = ET.SubElement(importdata, 'REQUESTDATA')
        tallymsg = ET.SubElement(requestdata, 'TALLYMESSAGE', {'xmlns:UDF': 'TallyUDF'})

        self._append_voucher(tallymsg, payload, guid, nature)

        xml_data = ET.tostring(root, encoding='unicode')
        _logger.debug("Generated Tally XML for invoice %s (%d bytes)",
                       invoice_num, len(xml_data))
        return xml_data

    def validate_voucher_balance(self, payload):
        """Validate accounting equation: sum credits - sum debits = 0.

        For target invoice:
            Sales (credit positive) + Tax (credit positive) + Customer (debit negative) = 0

        :return: dict with 'balanced' bool, 'message' str, 'difference' float
        """
        try:
            subtotal = float(payload['totals']['subtotal'])
            total_tax = float(payload['totals']['total_tax'])
            grand_total = float(payload['totals']['grand_total'])
        except Exception:
            return {'balanced': False, 'message': 'Invalid totals in payload', 'difference': 0.0}

        # Sales + tax - grand_total should be ~0
        diff = (subtotal + total_tax) - grand_total
        if abs(diff) > _VOUCHER_BALANCE_TOLERANCE:
            return {
                'balanced': False,
                'message': 'Subtotal %.2f + Tax %.2f != Grand Total %.2f (diff %.2f)' % (
                    subtotal, total_tax, grand_total, diff),
                'difference': diff,
            }

        # Cross-check inventory sum vs subtotal
        inv_sum = sum(float(l['taxable_amount']) for l in payload.get('lines', []))
        if abs(inv_sum - subtotal) > _VOUCHER_BALANCE_TOLERANCE:
            return {
                'balanced': False,
                'message': 'Inventory sum %.2f != Subtotal %.2f (diff %.2f)' % (
                    inv_sum, subtotal, inv_sum - subtotal),
                'difference': inv_sum - subtotal,
            }

        # Tax summary sum vs total_tax
        tax_sum = sum(float(t['amount']) for t in payload.get('tax_summary', []))
        if abs(tax_sum - total_tax) > _VOUCHER_BALANCE_TOLERANCE:
            return {
                'balanced': False,
                'message': 'Tax summary sum %.2f != Total Tax %.2f (diff %.2f)' % (
                    tax_sum, total_tax, tax_sum - total_tax),
                'difference': tax_sum - total_tax,
            }

        return {'balanced': True, 'message': 'Balanced', 'difference': diff}

    # ==================================================================
    # Voucher construction from payload - Target Invoice Structure
    # ==================================================================
    def _append_voucher(self, parent, payload, guid, nature):
        company = payload['company']
        buyer = payload['buyer']
        totals = payload['totals']
        lines = payload['lines']
        mappings = payload['tally_mappings']

        voucher = ET.SubElement(parent, 'VOUCHER', {
            'VCHTYPE': 'Sales',
            'ACTION': 'Create',
            'OBJVIEW': 'Invoice Voucher View',
        })

        # Header fields
        ET.SubElement(voucher, 'DATE').text = payload['invoice_date']
        ET.SubElement(voucher, 'GUID').text = guid
        ET.SubElement(voucher, 'VOUCHERTYPENAME').text = 'Sales'
        ET.SubElement(voucher, 'VOUCHERNUMBER').text = payload['invoice_number']
        ET.SubElement(voucher, 'PARTYLEDGERNAME').text = mappings['customer_ledger']
        ET.SubElement(voucher, 'PERSISTEDVIEW').text = 'Invoice Voucher View'
        ET.SubElement(voucher, 'VCHTYPE').text = 'Sales'
        ET.SubElement(voucher, 'NARRATION').text = ''

        # --- Customer header - dynamically from resolved buyer ---
        # BASICBUYERNAME / BUYERNAME / CONSIGNEE
        ET.SubElement(voucher, 'BASICBUYERNAME').text = buyer['buyer_name']
        ET.SubElement(voucher, 'BUYERNAME').text = buyer['buyer_name']
        # ADDRESS.LIST with nested ADDRESS
        addr_list = ET.SubElement(voucher, 'ADDRESS.LIST', {'TYPE': 'String'})
        ET.SubElement(addr_list, 'ADDRESS').text = buyer['buyer_address'] or ''
        basic_addr_list = ET.SubElement(voucher, 'BASICBUYERADDRESS.LIST', {'TYPE': 'String'})
        ET.SubElement(basic_addr_list, 'BASICBUYERADDRESS').text = buyer['buyer_address'] or ''
        ET.SubElement(voucher, 'COUNTRYNAME').text = buyer.get('buyer_country', '') or ''
        ET.SubElement(voucher, 'STATENAME').text = buyer.get('buyer_state', '') or ''
        ET.SubElement(voucher, 'PARTYGSTIN').text = buyer.get('buyer_gstin', '') or ''
        ET.SubElement(voucher, 'BASICCONSIGNEENAME').text = buyer['buyer_name']
        ET.SubElement(voucher, 'CONSIGNEESTATENAME').text = buyer.get('buyer_state', '') or ''
        # Compatibility fields for Tally GST (some versions expect these)
        if buyer['buyer_gstin']:
            ET.SubElement(voucher, 'GSTIN').text = buyer['buyer_gstin']
        ET.SubElement(voucher, 'PLACEOFSUPPLY').text = buyer.get('buyer_state', '') or ''
        # Buyer country/state for newer Tally
        ET.SubElement(voucher, 'CONSIGNEECOUNTRYNAME').text = buyer.get('buyer_country', '') or ''

        # --- Inventory entries: ONE per invoice line, directly under VOUCHER ---
        for line in lines:
            self._append_inventory_entry(voucher, line, mappings['sales_ledger'])

        # --- Ledger entries: separate customer debit + tax credits ---
        # Customer debit: AMOUNT = -grand_total, ISDEEMEDPOSITIVE = Yes (debit)
        cust_entry = ET.SubElement(voucher, 'LEDGERENTRIES.LIST')
        ET.SubElement(cust_entry, 'LEDGERNAME').text = mappings['customer_ledger']
        ET.SubElement(cust_entry, 'ISDEEMEDPOSITIVE').text = 'Yes'
        ET.SubElement(cust_entry, 'AMOUNT').text = self._fmt_amount(-float(totals['grand_total']))

        # Tax credit legs: each tax is credit -> ISDEEMEDPOSITIVE = No, AMOUNT positive
        for tax_item in payload.get('tax_summary', []):
            self._append_tax_entry(voucher, tax_item)

    def _append_inventory_entry(self, voucher, line, sales_ledger):
        """Add a single ALLINVENTORYENTRIES.LIST for one invoice line.

        Structure:
          ALLINVENTORYENTRIES.LIST
            STOCKITEMNAME
            ISDEEMEDPOSITIVE
            RATE  (e.g. "120/PKT")
            AMOUNT (taxable amount)
            BILLEDQTY (e.g. "15 PKT")
            ACTUALQTY (same)
            ACCOUNTINGALLOCATIONS.LIST
              LEDGERNAME (sales ledger)
              ISDEEMEDPOSITIVE No
              AMOUNT (taxable amount)
        """
        inv = ET.SubElement(voucher, 'ALLINVENTORYENTRIES.LIST')
        ET.SubElement(inv, 'STOCKITEMNAME').text = line['tally_stock_item'] or line.get('item_name', 'Item')
        ET.SubElement(inv, 'ISDEEMEDPOSITIVE').text = 'No'
        # RATE with UOM if available: "120/PKT"
        uom_name = line.get('tally_uom') or line.get('uom') or ''
        rate = line['rate']
        if uom_name:
            rate_text = '%s/%s' % (rate, uom_name)
        else:
            rate_text = rate
        ET.SubElement(inv, 'RATE').text = rate_text
        ET.SubElement(inv, 'AMOUNT').text = self._fmt_amount(float(line['taxable_amount']))
        # Quantities: "15 PKT"
        qty = line['quantity']
        if float(qty) == int(float(qty)):
            qty_str = str(int(float(qty)))
        else:
            qty_str = str(qty)
        billed = ('%s %s' % (qty_str, uom_name)).strip() if uom_name else qty_str
        ET.SubElement(inv, 'BILLEDQTY').text = billed
        ET.SubElement(inv, 'ACTUALQTY').text = billed
        # Keep UOM element for compatibility (some tests check UOM)
        if uom_name:
            ET.SubElement(inv, 'UOM').text = uom_name

        # Accounting allocation - sales ledger
        alloc = ET.SubElement(inv, 'ACCOUNTINGALLOCATIONS.LIST')
        ET.SubElement(alloc, 'LEDGERNAME').text = sales_ledger
        ET.SubElement(alloc, 'ISDEEMEDPOSITIVE').text = 'No'
        ET.SubElement(alloc, 'AMOUNT').text = self._fmt_amount(float(line['taxable_amount']))

    def _append_tax_entry(self, voucher, tax_item):
        """Add a tax LEDGERENTRIES.LIST entry (credit)."""
        entry = ET.SubElement(voucher, 'LEDGERENTRIES.LIST')
        ET.SubElement(entry, 'LEDGERNAME').text = tax_item['ledger_name']
        ET.SubElement(entry, 'ISDEEMEDPOSITIVE').text = 'No'
        ET.SubElement(entry, 'AMOUNT').text = self._fmt_amount(float(tax_item['amount']))

    # ==================================================================
    # Public API: HTTP transport (preserved from original)
    # ==================================================================
    def send_to_tally(self, xml_data, company=None):
        """POST the generated XML to the Tally HTTP API.

        :param xml_data: XML string produced by :meth:`generate_xml`
        :param company: ``res.company`` used for endpoint / credentials
        :return: raw response body as string
        :rtype: str
        """
        company = company or self.env.company
        url = self._resolve_tally_url(
            (company.tally_server_url or 'http://localhost:9000').strip())
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'Accept': 'text/xml',
        }
        auth = None
        if company.tally_username:
            auth = (company.tally_username, company.tally_password or '')
        payload = xml_data if isinstance(xml_data, bytes) else xml_data.encode('utf-8')

        _logger.info("Sending Tally XML to %s (%s bytes)", url, len(payload))
        try:
            response = requests.post(
                url,
                data=payload,
                headers=headers,
                auth=auth,
                timeout=company.tally_timeout or 30,
                verify=company.tally_verify_ssl,
            )
        except requests.exceptions.Timeout:
            raise
        except requests.exceptions.ConnectionError:
            raise
        except requests.exceptions.RequestException as error:
            raise UserError(_('Tally request failed: %s', error)) from error

        if response.status_code >= 400:
            raise UserError(
                _('Tally returned HTTP %s: %s',
                  response.status_code, (response.text or '')[:1000])
            )
        _logger.info("Tally responded with HTTP %s (%s bytes)",
                     response.status_code, len(response.content))
        return response.text

    # ==================================================================
    # Public API: Response parsing (preserved from original)
    # ==================================================================
    def parse_response(self, response):
        """Parse the Tally XML response.

        :return: dict with keys ``status`` (bool), ``message`` (str),
            ``voucher_id`` (str or None), ``guid`` (str or None),
            ``error_category`` (str or None)
        """
        if not response or not response.strip():
            return {
                'status': False,
                'message': _('Empty response from Tally'),
                'voucher_id': None,
                'guid': None,
                'error_category': 'UNKNOWN_TALLY_ERROR',
            }
        try:
            root = ET.fromstring(response)
        except ET.ParseError as error:
            return {
                'status': False,
                'message': _('Invalid XML response from Tally: %s', error),
                'voucher_id': None,
                'guid': None,
                'error_category': 'INVALID_XML',
            }

        status = root.findtext('.//STATUS')
        description = (root.findtext('.//DESC') or root.findtext('.//LINEERROR') or '').strip()

        if status is not None and str(status).strip() == '0':
            return {
                'status': True,
                'message': description or _('Synchronized successfully'),
                'voucher_id': self._extract_voucher_id(root),
                'guid': self._extract_guid(root),
                'error_category': None,
            }
        if status is not None:
            # Determine error category from description
            cat = 'TALLY_REJECTED'
            desc_lower = description.lower()
            if 'ledger' in desc_lower and 'does not exist' in desc_lower:
                cat = 'MISSING_MASTER'
            elif 'already' in desc_lower or 'duplicate' in desc_lower:
                cat = 'DUPLICATE_VOUCHER'
            return {
                'status': False,
                'message': description or _('Tally returned STATUS %s', status),
                'voucher_id': None,
                'guid': None,
                'error_category': cat,
            }

        # Real Tally success answer: <RESPONSE> summary block
        is_summary = root.tag == 'RESPONSE' or root.find('.//RESPONSE') is not None
        if is_summary:
            errors = self._as_int(root.findtext('.//ERRORS'))
            exceptions = self._as_int(root.findtext('.//EXCEPTIONS'))
            if errors == 0 and exceptions == 0:
                return {
                    'status': True,
                    'message': _(
                        'Tally import completed (created %s, altered %s)',
                        root.findtext('.//CREATED') or '0',
                        root.findtext('.//ALTERED') or '0',
                    ),
                    'voucher_id': self._extract_voucher_id(root),
                    'guid': self._extract_guid(root),
                    'error_category': None,
                }
            return {
                'status': False,
                'message': description or _(
                    'Tally reported %s errors during the import', errors),
                'voucher_id': None,
                'guid': None,
                'error_category': 'TALLY_REJECTED',
            }
        return {
            'status': False,
            'message': description or _('Tally response did not contain a STATUS element'),
            'voucher_id': None,
            'guid': None,
            'error_category': 'UNKNOWN_TALLY_ERROR',
        }

    # ==================================================================
    # Deterministic GUID (preserved for backward compatibility)
    # ==================================================================
    def _deterministic_guid(self, invoice_id, company_id):
        """Generate a deterministic UUID5 for duplicate protection."""
        return str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            'odoo-tally-%s-%s' % (company_id, invoice_id),
        ))

    # ==================================================================
    # Static tax classification (used by payload builder)
    # ==================================================================
    @staticmethod
    def _classify_tax_static(tax):
        """Classify a tax into CGST / SGST / IGST / CESS / OTHER.

        Resolution: l10n_in tags -> tax group name -> tax name.
        This is a static method so it can be called from the payload builder
        without needing an ORM environment.
        """
        # Check l10n_in tags if available
        try:
            for tax_type, xmlid in _TAX_TAG_XMLIDS.items():
                try:
                    tag = tax.env.ref(xmlid, raise_if_not_found=False)
                except Exception:
                    tag = None
                if tag and tag in tax.tag_ids:
                    return tax_type
        except Exception:
            pass

        # Fall back to tax group name
        group_name = (tax.tax_group_id.name or '').upper()
        for tax_type in _TAX_TYPES:
            if tax_type in group_name:
                return tax_type

        # Fall back to tax name
        tax_name = (tax.name or '').upper()
        for tax_type in _TAX_TYPES:
            if tax_type in tax_name:
                return tax_type
        return 'OTHER'

    # ==================================================================
    # Networking helpers (preserved from original)
    # ==================================================================
    @staticmethod
    def _is_wsl():
        try:
            with open('/proc/version') as version_file:
                return 'microsoft' in version_file.read().lower()
        except OSError:
            return False

    @staticmethod
    def _wsl_host_ip():
        try:
            import subprocess
            output = subprocess.check_output(
                ['ip', 'route', 'show', 'default'], text=True)
            parts = output.split()
            return parts[parts.index('via') + 1]
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def _resolve_tally_url(self, url):
        from urllib.parse import urlsplit, urlunsplit
        if not url:
            return url
        parsed = urlsplit(url)
        if parsed.hostname not in ('localhost', '127.0.0.1'):
            return url
        if not self._is_wsl():
            return url
        host_ip = self._wsl_host_ip()
        if not host_ip:
            return url
        host = host_ip if parsed.port is None else '%s:%s' % (host_ip, parsed.port)
        return urlunsplit(
            (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))

    # ==================================================================
    # Response parsing helpers
    # ==================================================================
    def _extract_voucher_id(self, root):
        node = root.find('.//VOUCHER/VOUCHERNUMBER')
        if node is None:
            node = root.find('.//VOUCHERNUMBER')
        return node.text.strip() if node is not None and node.text else None

    def _extract_guid(self, root):
        node = root.find('.//VOUCHER/GUID')
        if node is None:
            node = root.find('.//GUID')
        return node.text.strip() if node is not None and node.text else None

    @staticmethod
    def _as_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _fmt_amount(amount):
        value = float(amount or 0.0)
        if value == int(value):
            return str(int(value))
        return ('%.2f' % value).rstrip('0').rstrip('.')
