"""材料性硬度与封顶。"""

import unittest

from app.deal_monitor.materiality import (
    QUALITY_FINANCING,
    QUALITY_HARD,
    QUALITY_SOFT_PRODUCT,
    classify_deal_quality,
    finalize_materiality_score,
)


class TestDealQuality(unittest.TestCase):
    def test_marketplace_soft(self):
        text = (
            "CrowdStrike Brings the Falcon Platform to the Anthropic Claude Marketplace. "
            "Anthropic customers can procure Falcon."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_SOFT_PRODUCT)
        score = finalize_materiality_score(
            text, "ir:CRWD", ["LLM"], llm_score=85, event_type="ai_platform_deal"
        )
        self.assertLessEqual(score, 58)

    def test_financing_capped(self):
        text = (
            "BLUE OWL MANAGED FUNDS LEAD $2.4 BILLION AI FACTORY FINANCING FOR IREN. "
            "Financing for NVIDIA AI Infrastructure deployment."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_FINANCING)
        score = finalize_materiality_score(text, "pr_newswire", ["LLM"], llm_score=90)
        self.assertLessEqual(score, 60)

    def test_eose_power_boost(self):
        text = (
            "MN8 Energy, Eos Energy Enterprises, And Google Collaborate To Provide "
            "Energy Resources to PJM Grid with long-duration storage agreement."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_HARD)
        score = finalize_materiality_score(text, "finnhub:GOOGL", ["LLM"], llm_score=63)
        self.assertGreaterEqual(score, 78)

    def test_gcp_platform_not_soft_only(self):
        text = (
            "CrowdStrike today announced the Falcon platform is now available on "
            "Google Cloud infrastructure."
        )
        # available on google cloud → 非纯软整合
        self.assertNotEqual(classify_deal_quality(text), QUALITY_SOFT_PRODUCT)

    def test_claudeforce_hard(self):
        text = (
            "Salesforce and Anthropic announce Claudeforce, expanding their strategic "
            "partnership to integrate Claude with Salesforce Agentforce via product integration."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_HARD)
        score = finalize_materiality_score(
            text, "finnhub:CRM", ["LLM"], llm_score=82, event_type="ai_platform_deal"
        )
        self.assertGreaterEqual(score, 70)


if __name__ == "__main__":
    unittest.main()
