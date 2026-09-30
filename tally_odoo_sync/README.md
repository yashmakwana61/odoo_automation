# Tally Odoo Sync

Push posted customer invoices from Odoo to Tally ERP as GST-compliant **Sales Vouchers**
over the Tally HTTP XML API.

* Odoo remains the **source of truth** for invoicing (invoice is created & posted in Odoo).
* Tally is used as the **accounting backend** and receives a Sales Voucher per posted invoice.
* The Tally response is stored back on the invoice with full traceability.

---

## Module structure

```
tally_odoo_sync/
├── __manifest__.py
├── __init__.py
├── models/
│   ├── __init__.py
│   ├── account_move.py          # fields, action_post trigger, sync orchestration, cron/button
│   ├── res_company.py           # per-company Tally configuration
│   └── res_config_settings.py   # settings UI fields
├── services/
│   ├── __init__.py
│   └── tally_service.py         # TallyService: generate_xml / send_to_tally / parse_response
├── data/
│   └── cron.xml                 # retry cron (every 15 min)
├── views/
│   ├── account_move_views.xml   # form button/fields, list column, search filters
│   └── res_config_settings_views.xml
├── security/
│   └── ir.model.access.csv
├── tests/
│   ├── __init__.py
│   └── test_tally_sync.py
└── README.md
```

---

## Installation guide

1. **Tally side**

   - Start Tally ERP 9 / Prime.
   - Open **Gateway of Tally → F12 Configuration → Advanced Configuration (F1)**.
   - Enable **“Allow Tally to act as an ODBC / HTTP Server”** and set the port to
     **9000** (leave the other settings at their defaults).
   - Open the company that should receive the vouchers (must match the
     *Tally Company Name* configured below).

2. **Odoo side**

   - Copy the `tally_odoo_sync` folder into your addons path (or the
     `addons/` directory of this repository).
   - Restart Odoo with `-u tally_odoo_sync` (or install via Apps).

   ```bash
   ./odoo-bin -d <database> -i tally_odoo_sync --stop-after-init
   ```

3. **Configure** (`Settings → General Settings → Tally Sync` tab)

   | Setting                    | Description                                                    |
   |----------------------------|----------------------------------------------------------------|
   | Enable Tally Sync          | Master switch.                                                 |
   | Sync Automatically on Post | Push on `action_post()`; off = manual/cron only.               |
   | Server URL                 | Default `http://localhost:9000`.                               |
   | Tally Company Name         | Company inside Tally (defaults to the Odoo company name).      |
   | Username / Password        | Optional HTTP basic auth for the Tally endpoint.               |
   | Timeout / Verify SSL       | Request timeout (s) and SSL verification.                      |
   | Sales Ledger Name          | Tally ledger for the sales leg (default `Sales`).              |
   | Auto-create Missing Ledgers| Let Tally create missing party/sales/tax ledgers.              |
   | Party Ledger Parent Group  | Group for customer ledgers (default `Sundry Debtors`).         |
   | Sales Ledger Parent Group  | Group for the sales ledger (default `Sales Accounts`).         |
   | Tax Ledger Parent Group    | Group for GST/CESS output ledgers (default `Duties & Taxes`).  |

   > **Group names must match your Tally chart.** Tally rejects ledger
   > creation under a group that does not exist with `Group '<name>' does not
   > exist!`. Some Tally charts have no top-level `Primary` group (the default
   > assumption), so set these three fields to the actual groups shown in
   > **Gateway of Tally → Chart of Accounts**. `Sundry Debtors` is used for
   > customers, the *Sales Ledger Parent Group* for the sales ledger and the
   > *Tax Ledger Parent Group* for the output GST ledgers.

   > **WSL2 note:** If Odoo runs inside WSL2 while Tally listens on the Windows
   > host, `localhost` / `127.0.0.1` inside WSL points at the WSL VM, not at
   > Windows, so a `localhost:9000` endpoint is refused (Postman on Windows
   > still works). The module detects WSL and transparently rewrites
   > `localhost` endpoints to the Windows host gateway IP (e.g.
   > `http://172.29.x.x:9000`), so you can keep `http://localhost:9000` in the
   > settings. This only applies to `localhost`/`127.0.0.1` — explicit hostnames
   > or IPs are used as-is on any platform.

4. **Add the GSTINs**

   - Company GSTIN → `Settings → Companies → Company → Company Information → Tax ID / GSTIN`
     (`company.vat`).
   - Customer GSTIN → `Contacts → customer → VAT` (`partner.vat`).

---

## Workflow

```
Invoice Posted (Odoo)
        ↓  action_post()
Generate XML (tally.service.generate_xml)
        ↓
Send to Tally (tally.service.send_to_tally)  → POST http://localhost:9000
        ↓
Receive Response (tally.service.parse_response)
        ↓
Update Fields in Odoo (tally_sync_status, tally_voucher_id, ...)
        ↓
Post message in chatter (message_post)
```

---

## Data mapping (invoice → Tally XML)

| Odoo                               | Tally XML                                                            |
|------------------------------------|---------------------------------------------------------------------|
| `invoice.name`                     | `<VOUCHERNUMBER>`                                                    |
| `invoice.invoice_date`             | `<DATE>` (YYYYMMDD)                                                  |
| `company_id.name` / `tally_company_name` | `SVCURRENTCOMPANY`                                           |
| `company_id.vat`                   | company GSTIN (used in ledger creation)                              |
| `partner_id.name`                  | `<PARTYLEDGERNAME>` + party ledger entry                             |
| `partner_id.vat`                   | `<GSTIN>` (customer GSTIN)                                           |
| `partner_id` address fields        | party ledger `ADDRESS.LIST` (when auto-creating ledgers)             |
| `invoice_line_ids`                 | `ALLINVENTORYENTRIES.LIST` (ITEMNAME / RATE / QUANTITY / AMOUNT)     |
| line taxes (CGST/SGST/IGST)        | tax ledger entries with `GSTDUTYHEAD`, `GSTRATE`, `GSTTREATMENT`     |
| `amount_untaxed`                   | sales ledger entry                                                   |
| `amount_tax`                       | sum of tax ledger entries                                            |
| `amount_total`                     | party (customer) ledger entry (negative = debit)                     |
| deterministic GUID                 | `<GUID>` (duplicate protection)                                      |

Taxes are classified as CGST / SGST / IGST / CESS using (in order):
l10n_in tax tags → tax group name → tax name.

---

## Error handling

| Scenario                | Behaviour                                                                 |
|-------------------------|---------------------------------------------------------------------------|
| Connection timeout      | Raised as `UserError`, invoice marked `failed` with reason in chatter.     |
| Connection refused      | Raised as `UserError`, invoice marked `failed`.                            |
| HTTP >= 400             | Raised as `UserError` with the Tally body snippet.                         |
| Invalid response XML    | `parse_response` returns failure, invoice marked `failed`.                 |
| Ledger missing          | Tally returns non-zero `STATUS` → marked `failed`; enable auto-create ledgers or create them in Tally. |
| Duplicate entry         | Same deterministic `<GUID>` → Tally deduplicates; already-synced invoices are rejected by the button. |
| Tally business error    | Non-zero `STATUS` in response body → `failed` with `DESC`/`LINEERROR` message. |

All failures are retried by the **cron** (`Tally Sync: Retry Failed Invoices`, every 15 min)
or manually via the **“Sync to Tally”** button on the invoice.

---

## Testing steps

Run the automated test suite (network calls are mocked):

```bash
./odoo-bin -d <test_db> --test-enable --test-tags /tally_odoo_sync -i tally_odoo_sync --stop-after-init
```

Manual end-to-end test against a live Tally:

1. Create a customer invoice (customer with a valid GSTIN).
2. Post it → a chatter message “Invoice successfully synchronized to Tally” appears
   and the *Tally Sync Status* becomes **Synced** with a voucher id.
3. In Tally, open the voucher: `Gateway of Tally → Vouchers → Sales` and verify
   party / sales / tax legs and the GST details.
4. Stop Tally, post another invoice → status becomes **Failed** with a reason.
5. Start Tally again and click **Sync to Tally** on the failed invoice (or wait for
   the cron) → it becomes **Synced**.
6. Check the chatter for both success and failure messages.

---

## Security notes

* The “Sync to Tally” button is restricted to the accounting groups
  (`account.group_account_invoice`, `account.group_account_readonly`).
* The retry cron runs server-side (admin context) but only ever touches posted
  `out_invoice` records.
* Tally responses are validated (XML parsing + `STATUS` check) before any field
  update, and the raw response is logged/store for audit.
* All traffic is logged through the standard Python `logging` framework
  (`tally_odoo_sync` logger).

---

## Optional enhancements (not implemented here)

* FastAPI middleware between Odoo and Tally (queuing, retries, throttling).
* Celery / Odoo queue for asynchronous sync.
* Dedicated sync-log model + dashboard (Kanban/graph of statuses, timings).
* Multi-company support is already per-company via `res.company` fields.
