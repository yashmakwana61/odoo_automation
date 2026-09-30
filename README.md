# Odoo Integrations — Telegram + Tally

Custom Odoo 19 modules extracted from the main development tree.

## Modules

- `telegram_odoo_integration` — Collect orders via Telegram bot webhook (`/telegram/webhook`), session-based order collection (`/neworder`, `/done`, `/cancel`, `/status`), AI Parser handoff. Depends: `base`, `sale`.
- `tally_odoo_sync` — Push posted customer invoices to Tally ERP as GST-compliant Sales Vouchers, retry cron, manual sync button. Depends: `account`, `l10n_in`.

Source paths in `odoo19` monorepo:
- `unicore/custom_addons/telegram_odoo_integration`
- `addons/tally_odoo_sync`

## Usage

Copy/symlink these folders into your Odoo `addons_path`, e.g.:

```bash
addons_path = /path/to/odoo/addons,/path/to/odoo-telegram-tally/telegram_odoo_integration,...
```

Or point Odoo directly at this repo root — Odoo will discover both modules.

Update apps list and install from Apps menu.
