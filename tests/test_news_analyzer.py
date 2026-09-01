from data.news_analyzer import analyze_news


def test_policy_document_scores_industrial_internet_as_high_priority():
    score = analyze_news("八部门印发推动工业互联网高质量发展实施意见，建设工业5G专网")

    assert score.sentiment == "positive"
    assert score.risk_level == "high"
    assert "工业互联网" in score.tags
    assert "工业5G" in score.tags
    assert "多部门政策" in score.tags


def test_market_regulator_name_is_not_regulatory_risk():
    score = analyze_news("工信部、市场监管总局等八部门联合印发工业互联网实施意见")

    assert score.sentiment == "positive"
    assert "监管函" not in score.tags


def test_regulatory_letter_still_counts_as_risk():
    score = analyze_news("公司收到交易所监管函")

    assert score.sentiment == "negative"
    assert score.risk_level == "high"
    assert "监管函" in score.tags
