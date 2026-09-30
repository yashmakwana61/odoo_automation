"""Pure-Python parser for Telegram order messages.

Kept deliberately free of any ORM dependency so it can be unit tested in
isolation and reused by any caller.
"""
import re
from decimal import Decimal, InvalidOperation

# Matches one product item:  <name> Qty:<qty> Price:<price>
# - "Order:" prefix is stripped beforehand
# - "Qty"/"Price" keywords are case-insensitive and may be separated by ':' or '='
# - quantities/prices accept integers or decimals (comma or dot decimal separator)
# - the product name uses a tempered token so it can never swallow a following
#   " Qty:..." sequence (which would merge several comma separated products
#   into a single item).
ITEM_RE = re.compile(
    r'^\s*(?P<name>(?:(?!\s+[Qq]ty\b).)+)'
    r'\s+[Qq]ty\s*[:=]\s*(?P<qty>\d+(?:[.,]\d+)?)'
    r'\s+[Pp]rice\s*[:=]\s*(?P<price>\d+(?:[.,]\d+)?)\s*$',
)

# Optional leading "Order:" keyword (case-insensitive).
ORDER_PREFIX_RE = re.compile(r'^\s*[Oo]rder\s*[:=]\s*')

INVALID_FORMAT_MSG = (
    'Invalid format. Use: Order: Product Qty:2 Price:100\n'
    'Multiple products: Order: Product1 Qty:1 Price:10, '
    'Product2 Qty:2 Price:20'
)


class TelegramOrderParser:
    """Parse Telegram order messages.

    Supported formats (keywords ``Order``/``Qty``/``Price`` are
    case-insensitive):

    * ``Order: Laptop Qty:2 Price:50000``
    * ``Order: Laptop Qty:2 Price:50000, Mouse Qty:3 Price:300``
    * ``Order: Laptop Qty:2 Price:50000``
      ``Mouse Qty:3 Price:300``

    NOTE: When several products are listed on the same line they must be
    separated by commas; a product name must therefore not contain a comma.
    Product names containing commas can be sent one per line instead.
    """

    @classmethod
    def parse(cls, text):
        """Parse ``text`` into a list of order items.

        :param text: raw message text
        :return: tuple ``(items, error)`` where ``items`` is a list of dicts
            ``{'name': str, 'qty': Decimal, 'price': Decimal}`` and ``error``
            is ``None`` on success, otherwise a user-friendly message.
        """
        if not text or not text.strip():
            return [], 'Message is empty.'

        normalized = ORDER_PREFIX_RE.sub('', text.strip()).strip()
        if not normalized:
            return [], INVALID_FORMAT_MSG

        items = []
        for fragment in cls._split_fragments(normalized):
            match = ITEM_RE.match(fragment)
            if not match:
                return [], INVALID_FORMAT_MSG
            try:
                qty = Decimal(match.group('qty').replace(',', '.'))
                price = Decimal(match.group('price').replace(',', '.'))
            except InvalidOperation:
                return [], INVALID_FORMAT_MSG
            if qty <= 0:
                return [], 'Quantity must be a positive number.'
            if price < 0:
                return [], 'Price cannot be negative.'
            items.append({
                'name': match.group('name').strip(),
                'qty': qty,
                'price': price,
            })

        return items, None

    @classmethod
    def _split_fragments(cls, normalized):
        """Split a normalized payload into single-product fragments.

        A full line that already matches the item pattern is kept whole
        (so product names containing commas work on their own line);
        otherwise the line is split on commas.
        """
        fragments = []
        for line in normalized.splitlines():
            line = line.strip()
            if not line:
                continue
            if ITEM_RE.match(line):
                fragments.append(line)
                continue
            for part in line.split(','):
                part = part.strip()
                if part:
                    fragments.append(part)
        return fragments
