from __future__ import annotations

import unittest

from qtc_tunnel.focus import hwnd_value, same_hwnd


class HwndHelperTests(unittest.TestCase):
    def test_same_numeric_hwnd_values_match(self):
        self.assertTrue(same_hwnd(123, 123))
        self.assertTrue(same_hwnd(123, 123))

    def test_ctypes_wrappers_compare_by_value(self):
        from ctypes import wintypes

        left = wintypes.HWND(0x1234)
        right = wintypes.HWND(0x1234)
        self.assertFalse(left == right)
        self.assertTrue(same_hwnd(left, right))
        self.assertEqual(hwnd_value(left), 0x1234)

    def test_null_handles_normalize_to_zero(self):
        self.assertEqual(hwnd_value(None), 0)
        self.assertTrue(same_hwnd(None, 0))


if __name__ == "__main__":
    unittest.main()
