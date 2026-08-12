from datetime import date

from zotero_arxiv_daily.weekly_venues import (
    IeeeXploreClient,
    VenuePaper,
    is_within_lookback,
    matches_keywords,
    normalize_doi,
    normalize_title,
    parse_bool,
)


def make_paper(**overrides):
    values = dict(
        venue_key="JSSC",
        venue_name="IEEE Journal of Solid-State Circuits",
        title="A Low-Jitter Fractional-N PLL",
        authors=["Ada Lovelace"],
        abstract="A frequency synthesizer with low phase noise.",
        doi="10.1109/JSSC.2026.1",
        ieee_url="https://ieeexplore.ieee.org/document/1",
        article_number="1",
        publication_date="2026-08-10",
        content_type="Journals",
    )
    values.update(overrides)
    return VenuePaper(**values)


def test_normalizers_support_doi_urls_and_punctuation():
    assert normalize_doi("https://doi.org/10.1109/JSSC.2026.1") == "10.1109/jssc.2026.1"
    assert normalize_title("A Low-Jitter PLL!") == "alowjitterpll"


def test_keyword_filter_uses_title_and_abstract_case_insensitively():
    paper = make_paper()
    assert matches_keywords(paper, ["PLL"])
    assert matches_keywords(paper, ["phase noise"])
    assert not matches_keywords(paper, ["neural implant"])
    assert matches_keywords(paper, [])


def test_lookback_uses_ieee_insert_or_publication_date():
    assert is_within_lookback(make_paper(publication_date="2026-08-10"), 14, date(2026, 8, 12))
    assert not is_within_lookback(make_paper(publication_date="2026-06-01"), 14, date(2026, 8, 12))
    assert is_within_lookback(make_paper(publication_date="2026"), 14, date(2026, 8, 12))


def test_parse_bool_is_safe_for_unset_github_variable():
    assert parse_bool("") is True
    assert parse_bool("true") is True
    assert parse_bool("false") is False


def test_ieee_query_uses_insertion_date_delta(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"articles": []}

    def fake_get(url, params, headers, timeout):
        captured.update(params)
        return Response()

    monkeypatch.setattr("zotero_arxiv_daily.weekly_venues.requests.get", fake_get)
    IeeeXploreClient("test-key").search_venue("JSSC", "IEEE Journal", 100, 14)

    start = date.fromisoformat(
        f"{captured['start_date'][:4]}-{captured['start_date'][4:6]}-{captured['start_date'][6:]}"
    )
    end = date.fromisoformat(
        f"{captured['end_date'][:4]}-{captured['end_date'][4:6]}-{captured['end_date'][6:]}"
    )
    assert (end - start).days == 14
    assert captured["sort_field"] == "article_number"
