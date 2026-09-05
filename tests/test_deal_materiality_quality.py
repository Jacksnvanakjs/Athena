"""材料性硬度与封顶。"""

import unittest

from app.deal_monitor.materiality import (
    QUALITY_FINANCING,
    QUALITY_HARD,
    QUALITY_MA,
    QUALITY_SOFT_PRODUCT,
    QUALITY_VAGUE,
    calibrate_score_toward_outcome,
    classify_deal_quality,
    finalize_materiality_score,
    is_large_score_outcome_gap,
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
        self.assertLessEqual(score, 52)

    def test_financing_capped(self):
        text = (
            "BLUE OWL MANAGED FUNDS LEAD $2.4 BILLION AI FACTORY FINANCING FOR IREN. "
            "Financing for NVIDIA AI Infrastructure deployment."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_FINANCING)
        score = finalize_materiality_score(text, "pr_newswire", ["LLM"], llm_score=90)
        self.assertLessEqual(score, 55)

    def test_financing_capabilities_not_financing(self):
        text = (
            "HPE today announced an expanded collaboration with Oracle to help scale "
            "Oracle’s global AI infrastructure by deploying HPE Juniper Networking "
            "across Oracle’s AI data centers, with support services and financing capabilities."
        )
        self.assertNotEqual(classify_deal_quality(text), QUALITY_FINANCING)

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
        self.assertGreaterEqual(score, 72)

    def test_ma_sale_capped(self):
        text = "EPC Power Announces Sale to Flex for $4.4 Billion in the AI Era"
        self.assertEqual(classify_deal_quality(text), QUALITY_MA)
        score = finalize_materiality_score(text, "pr_newswire", [], llm_score=90)
        self.assertLessEqual(score, 65)

    def test_vague_capped(self):
        text = "Company expands strategic partnership with OpenAI to explore collaboration."
        self.assertEqual(classify_deal_quality(text), QUALITY_VAGUE)
        score = finalize_materiality_score(text, "google_news:x", [], llm_score=88)
        self.assertLessEqual(score, 48)

    def test_power_deal_commercial(self):
        text = "Microsoft and Chevron Sign 20-Year Power Deal For Texas Data Center"
        self.assertEqual(classify_deal_quality(text), QUALITY_HARD)

    def test_calibrate_and_gap(self):
        self.assertTrue(is_large_score_outcome_gap(80, 35))
        self.assertFalse(is_large_score_outcome_gap(70, 62))
        cal = calibrate_score_toward_outcome(80, 35)
        self.assertLess(cal, 70)
        self.assertGreaterEqual(cal, 35)

    def test_verizon_google_cloud_hard(self):
        text = (
            "Google Cloud today announced a new strategic partnership agreement with Verizon "
            "focused on delivering faster AI experiences by deploying Google Cloud."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_HARD)
        score = finalize_materiality_score(text, "finnhub:VZ", [], llm_score=60)
        self.assertGreaterEqual(score, 70)

    def test_amd_instinct_anthropic(self):
        text = "AMD：与 Anthropic 部署 Instinct GPU"
        self.assertEqual(classify_deal_quality(text), QUALITY_HARD)
        score = finalize_materiality_score(text, "finnhub:AMD", [], llm_score=50)
        self.assertGreaterEqual(score, 68)

    def test_utility_openai_power_not_inflated(self):
        text = (
            "SO：电力公司与 OpenAI 用电合同\n"
            "Large-load growth; Georgia Power customers; Agreement with OpenAI for power."
        )
        self.assertEqual(classify_deal_quality(text), QUALITY_HARD)
        score = finalize_materiality_score(text, "pr_newswire", [], llm_score=90)
        self.assertLessEqual(score, 65)
        self.assertGreaterEqual(score, 60)


if __name__ == "__main__":
    unittest.main()
