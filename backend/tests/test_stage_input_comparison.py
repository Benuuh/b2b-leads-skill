"""G1 comparison: the same observations as hand-built input and as a staged draft."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import compare_stage_inputs as comparison


def test_staged_draft_matches_manual_input_on_the_case_matrix():
    report = comparison.compare()
    assert report['ok'] is True
    assert report['totals'].get('staged_accepted_manual_rejected', 0) == 0
    assert report['totals'] == {'equivalent_accepted': 4, 'equivalent_rejected': 1,
                                'staged_withheld': 9}
    for row in report['cases']:
        assert row['outcome'] == row['expected'], row
        if row['outcome'] == 'equivalent_accepted':
            assert row['candidate_identical'] is True
            assert row['manual']['decision'] == row['staged']['decision']


def test_withheld_cases_report_a_reason_instead_of_a_partial_candidate():
    report = comparison.compare()
    withheld = [row for row in report['cases'] if row['outcome'] == 'staged_withheld']
    assert {row['staged']['reason_code'] for row in withheld} == {
        'MULTIPLE_SUBJECTS', 'DUPLICATE_SUBJECT_REVIEW', 'CONFLICTING_VALUES',
        'EVIDENCE_OUT_OF_DATE', 'IDENTITY_MISMATCH', 'UNSUPPORTED_FIELD',
        'EXCERPT_UNVERIFIED', 'WEBSITE_UNCONFIRMED'}
    for row in withheld:
        assert row['staged']['admissible'] is False
        assert row['staged']['decision'] is None
