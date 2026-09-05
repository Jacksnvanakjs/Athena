"""中文展示标题提炼。"""

from app.deal_monitor.headline_zh import build_zh_headline, needs_zh_headline


def test_power_deal_zh():
    h = build_zh_headline(
        "CVX",
        "Microsoft and Chevron Sign 20-Year Power Deal For Texas Data Center - EnergyNow.com",
        "Microsoft and Chevron signed a 20-year power deal.",
    )
    assert h.startswith("CVX：")
    assert "电力" in h
    assert "EnergyNow" not in h


def test_sale_to_flex():
    h = build_zh_headline(
        "FLEX",
        "EPC Power Announces Sale to Flex for $4.4 Billion",
        "EPC Power announces sale to Flex for $4.4 billion.",
    )
    assert h.startswith("FLEX：")
    assert "收购" in h


def test_already_zh_kept():
    src = "SO：电力公司与 OpenAI 用电合同"
    assert needs_zh_headline(src) is False
    assert build_zh_headline("SO", src, "") == src


def test_cloudflare_openai():
    h = build_zh_headline(
        "NET",
        "Cloudflare Partners with OpenAI Daybreak Models to Redefine Vulnerability Management",
        "Cloudflare today announced partnership with OpenAI.",
    )
    assert h.startswith("NET：")
    assert "OpenAI" in h or "合作" in h


def test_claudeforce_not_agentforce():
    h = build_zh_headline(
        "CRM",
        "CRM：Salesforce×Anthropic Claudeforce",
        "Salesforce and Anthropic announce Claudeforce with Agentforce integration.",
    )
    assert "Claudeforce" in h
    assert "扩展 Agentforce" not in h


def test_gpu_deploy():
    h = build_zh_headline(
        "QMLS",
        "QumulusAI Completes Deployment of All 616 NVIDIA RTX PRO 6000 Blackwell GPUs for Runpod",
        "",
    )
    assert "GPU" in h or "部署" in h
    assert h.startswith("QMLS：")
