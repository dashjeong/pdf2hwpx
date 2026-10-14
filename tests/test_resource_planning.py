import ctypes
import unittest
from unittest.mock import patch

from converter import resources


class ResourcePlanningTests(unittest.TestCase):
    def test_cap_and_reserved_memory(self):
        high = resources.plan_resources(available_bytes=32 * resources.GiB)
        self.assertEqual(high['budget_bytes'], 8 * resources.GiB)
        limited = resources.plan_resources(available_bytes=4 * resources.GiB)
        self.assertEqual(limited['budget_bytes'], 3 * resources.GiB)
        self.assertLessEqual(limited['estimated_peak_bytes'], limited['budget_bytes'])

    def test_low_memory_scales_large_page_and_disables_llm(self):
        plan = resources.plan_resources(10000, 10000, available_bytes=2 * resources.GiB, use_llm=True)
        self.assertTrue(plan['allowed'])
        self.assertFalse(plan['llm_allowed'])
        self.assertLess(plan['max_pixels'], resources.MAX_PIXELS)
        self.assertLessEqual(plan['scaled_width'] * plan['scaled_height'], plan['max_pixels'])
        self.assertLessEqual(plan['estimated_peak_bytes'], plan['budget_bytes'])

    def test_refuses_below_minimum_working_plan(self):
        plan = resources.plan_resources(available_bytes=resources.GiB)
        self.assertFalse(plan['allowed'])
        self.assertEqual(plan['batch_pages'], 0)
        self.assertFalse(plan['llm_allowed'])

    def test_unknown_measurement_is_flagged_and_does_not_claim_llm_headroom(self):
        with patch.object(resources, 'available_memory_bytes', return_value=None):
            plan = resources.plan_resources(use_llm=True)
        self.assertIsNone(plan['available_bytes'])
        self.assertFalse(plan['measurement_available'])
        self.assertTrue(plan['estimate_only'])
        self.assertFalse(plan['llm_allowed'])
        self.assertEqual(plan['budget_bytes'], resources.UNKNOWN_BUDGET_BYTES)

    def test_fresh_available_ram_rechecks_change_budget(self):
        with patch.object(resources, 'available_memory_bytes', side_effect=[8 * resources.GiB, 2 * resources.GiB]):
            first = resources.plan_resources(use_llm=True)
            second = resources.plan_resources(use_llm=True)
        self.assertTrue(first['llm_allowed'])
        self.assertFalse(second['llm_allowed'])
        self.assertLess(second['budget_bytes'], first['budget_bytes'])

    def test_chunking_retains_all_217_pages_in_order(self):
        chunks = list(resources.chunk_pages(range(217), 17))
        self.assertEqual([page for chunk in chunks for page in chunk], list(range(217)))
        self.assertTrue(all(1 <= len(chunk) <= 17 for chunk in chunks))
        with self.assertRaises(ValueError):
            list(resources.chunk_pages(range(5), 0))

    def test_windows_memory_structure_layout(self):
        self.assertEqual(ctypes.sizeof(resources._MemoryStatusEx), 64)
        self.assertEqual(resources._MemoryStatusEx.ullAvailPhys.offset, 16)


if __name__ == '__main__':
    unittest.main()
