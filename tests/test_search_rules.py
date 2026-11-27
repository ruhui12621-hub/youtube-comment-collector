import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from search_rules import is_relevant_monitor_text, should_exclude_text


class SearchRulesTests(unittest.TestCase):
    def test_target_legion_pro_models_are_kept(self):
        self.assertTrue(is_relevant_monitor_text("Lenovo Legion Pro 27UD-10"))
        self.assertTrue(is_relevant_monitor_text("Legion Pro 32UD-10 monitor review"))
        self.assertTrue(is_relevant_monitor_text("Lenovo Legion Pro 27Q-10 OLED monitor"))

    def test_qd_models_need_explicit_pro(self):
        self.assertTrue(is_relevant_monitor_text("Lenovo Legion Pro 27QD-10 monitor"))
        self.assertFalse(is_relevant_monitor_text("Lenovo Legion 27QD-10 monitor"))
        self.assertTrue(should_exclude_text("Lenovo Legion 27QD-10 monitor"))
        self.assertFalse(should_exclude_text("Lenovo Legion Pro 27QD-10 monitor"))

    def test_other_lenovo_product_families_are_rejected(self):
        self.assertFalse(is_relevant_monitor_text("Lenovo LOQ 27Q-10 monitor"))
        self.assertFalse(is_relevant_monitor_text("Yoga Pro 27UD-10 laptop"))
        self.assertFalse(is_relevant_monitor_text("ThinkPad OLED HDR review"))

    def test_generic_and_handheld_results_are_rejected(self):
        self.assertFalse(is_relevant_monitor_text("Best OLED HDR gaming monitor"))
        self.assertFalse(is_relevant_monitor_text("Legion Go handheld review"))

    def test_monitor_context_can_coexist_with_laptop_context(self):
        self.assertTrue(
            is_relevant_monitor_text(
                "Legion Pro 27UD-10 monitor gaming test with a laptop"
            )
        )


if __name__ == "__main__":
    unittest.main()
