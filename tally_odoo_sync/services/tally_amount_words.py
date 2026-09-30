# -*- coding: utf-8 -*-
"""Amount in words conversion using Indian numbering terminology."""

_ONES = [
    '', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight',
    'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen',
    'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen',
]
_TENS = [
    '', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy',
    'Eighty', 'Ninety',
]


def _two_digits(n):
    """Convert 0-99 to words."""
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + ('' if ones == 0 else ' ' + _ONES[ones])


def _three_digits(n):
    """Convert 0-999 to words."""
    if n == 0:
        return ''
    hundreds, remainder = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(_ONES[hundreds] + ' Hundred')
    if remainder:
        parts.append(_two_digits(remainder))
    return ' '.join(parts)


def _chunk_words(n):
    """Convert an integer to Indian-style words.

    Indian grouping: Crores, Lakhs, Thousands, Hundreds.
    """
    if n == 0:
        return 'Zero'

    parts = []
    # Crores (10000000+)
    if n >= 10000000:
        crore_val = n // 10000000
        parts.append(_three_digits(crore_val) + ' Crore')
        n %= 10000000
    # Lakhs (100000-9999999)
    if n >= 100000:
        lakh_val = n // 100000
        parts.append(_three_digits(lakh_val) + ' Lakh')
        n %= 100000
    # Thousands (1000-99999)
    if n >= 1000:
        thousand_val = n // 1000
        parts.append(_three_digits(thousand_val) + ' Thousand')
        n %= 1000
    # Hundreds (0-999)
    if n > 0:
        parts.append(_three_digits(n))
    return ' '.join(parts)


def amount_in_words(amount):
    """Convert a monetary amount to Indian-English words.

    Examples:
        0          -> "Zero Rupees Only"
        1          -> "One Rupees Only"
        100000     -> "One Lakh Rupees Only"
        10000000   -> "One Crore Rupees Only"
        11800      -> "Eleven Thousand Eight Hundred Rupees Only"
        11800.50   -> "Eleven Thousand Eight Hundred Rupees and Fifty Paise Only"

    Paise are appended with "and X Paise" when present.
    """
    amount = float(amount or 0)
    if amount < 0:
        return 'Minus ' + amount_in_words(-amount)

    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    # Handle floating point imprecision (e.g. 0.1 + 0.2 = 0.30000000000000004)
    if paise >= 100:
        rupees += 1
        paise = 0

    words = _chunk_words(rupees) + ' Rupees'
    if paise > 0:
        words += ' and ' + _two_digits(paise) + ' Paise'
    words += ' Only'
    return words
