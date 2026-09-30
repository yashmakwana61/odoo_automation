"""Unit tests for the Telegram order message parser."""
from odoo.tests import TransactionCase, tagged

from odoo.addons.telegram_odoo_integration.models.telegram_parser import (
    TelegramOrderParser,
)


@tagged('post_install', '-at_install')
class TestTelegramOrderParser(TransactionCase):

    def test_single_product(self):
        items, error = TelegramOrderParser.parse(
            'Order: Laptop Qty:2 Price:50000')
        self.assertIsNone(error)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], 'Laptop')
        self.assertEqual(float(items[0]['qty']), 2.0)
        self.assertEqual(float(items[0]['price']), 50000.0)

    def test_multiple_products_comma_separated(self):
        items, error = TelegramOrderParser.parse(
            'Order: Laptop Qty:2 Price:50000, Mouse Qty:3 Price:300')
        self.assertIsNone(error)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['name'], 'Laptop')
        self.assertEqual(items[1]['name'], 'Mouse')
        self.assertEqual(float(items[1]['price']), 300.0)

    def test_multiple_products_newline_separated(self):
        items, error = TelegramOrderParser.parse(
            'Order: Laptop Qty:2 Price:50000\nMouse Qty:3 Price:300')
        self.assertIsNone(error)
        self.assertEqual(len(items), 2)

    def test_product_name_with_comma_on_own_line(self):
        items, error = TelegramOrderParser.parse(
            'Order: Phone, 5G Qty:1 Price:2000')
        self.assertIsNone(error)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], 'Phone, 5G')

    def test_case_insensitive_keywords(self):
        items, error = TelegramOrderParser.parse('order: phone qty: 1 price: 150')
        self.assertIsNone(error)
        self.assertEqual(items[0]['name'], 'phone')

    def test_decimal_quantity_and_price(self):
        items, error = TelegramOrderParser.parse('Order: Milk Qty:1.5 Price:2.5')
        self.assertIsNone(error)
        self.assertEqual(float(items[0]['qty']), 1.5)
        self.assertEqual(float(items[0]['price']), 2.5)

    def test_invalid_format(self):
        items, error = TelegramOrderParser.parse('I want a laptop please')
        self.assertEqual(items, [])
        self.assertIsNotNone(error)

    def test_zero_quantity_rejected(self):
        items, error = TelegramOrderParser.parse('Order: Laptop Qty:0 Price:100')
        self.assertEqual(items, [])
        self.assertIsNotNone(error)

    def test_empty_message(self):
        items, error = TelegramOrderParser.parse('')
        self.assertEqual(items, [])
        self.assertIsNotNone(error)

    def test_negative_price_rejected(self):
        items, error = TelegramOrderParser.parse('Order: Laptop Qty:1 Price:-10')
        self.assertEqual(items, [])
        self.assertIsNotNone(error)
