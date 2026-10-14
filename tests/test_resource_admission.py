"""Costly operations share one budget; existing app commit counts toward it."""
import threading
import unittest
from unittest.mock import patch
from converter import resource_admission as admission
from converter.resources import plan_resources, GiB


class ResourceAdmissionTests(unittest.TestCase):
    def test_export_and_ai_cannot_start_while_conversion_owns_budget(self):
        with admission.heavy_operation('conversion'):
            errors = []
            def attempt():
                try:
                    with admission.heavy_operation('export'):
                        errors.append('unexpected admission')
                except admission.ResourceBusy:
                    errors.append('busy')
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ['busy'])
        with admission.heavy_operation('AI'):
            self.assertEqual(admission.busy_operation(), 'AI')

    def test_existing_commit_reduces_eight_gib_plan(self):
        plan = plan_resources(20000, 20000, available_bytes=12 * GiB, owned_bytes=7 * GiB)
        self.assertEqual(plan['budget_bytes'], GiB)
        self.assertLessEqual(plan['estimated_peak_bytes'], GiB)
        self.assertFalse(plan['llm_allowed'])

    def test_measured_over_budget_stops_before_next_page(self):
        with patch.object(admission, 'process_memory', return_value={
            'measurement_available': True, 'private_commit_bytes': 8 * GiB}):
            with self.assertRaisesRegex(RuntimeError, '완료한 쪽은 보관'):
                admission.check_measured_budget()

    def test_actual_windows_sample_records_this_process(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows memory sample')
        sample = admission.process_memory()
        self.assertTrue(sample['measurement_available'])
        self.assertGreater(sample['private_commit_bytes'], 0)
        self.assertGreater(sample['working_set_bytes'], 0)
        self.assertIn(os.getpid(), [p['pid'] for p in sample['processes']])
        self.assertFalse(sample['browser_included'])
