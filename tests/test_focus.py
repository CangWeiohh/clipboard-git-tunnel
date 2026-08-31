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

    def test_large_hwnd_values_do_not_overflow(self):
        # EnumWindows on 64-bit Windows can yield HWND values whose high
        # 32 bits are set. focus.py must never feed such values through a
        # ctypes function whose argtypes are missing (defaults to C int),
        # which raises OverflowError and aborts the whole enumeration.
        from ctypes import wintypes

        large = 0x0000000112345678
        self.assertEqual(hwnd_value(large), large)
        self.assertTrue(same_hwnd(wintypes.HWND(large), large))
        self.assertNotEqual(hwnd_value(large), 0x12345678)


if __name__ == "__main__":
    unittest.main()
