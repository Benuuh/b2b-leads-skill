"""Offline cost-baseline report calculator; does not access runs or the network."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TOOL_VERSION = '0.1.0-dev'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class Measurement(Strict):
    value: float | None = Field(ge=0)
    source: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=500)
    missing_reason: str | None = None

    @model_validator(mode='after')
    def explain_missing(self):
        if self.value is None and not (self.missing_reason or '').strip():
            raise ValueError('missing measurement requires a reason')
        if self.value is not None and self.missing_reason is not None:
            raise ValueError('measured value cannot have a missing reason')
        return self


class TokenCounts(Strict):
    input: Measurement
    output: Measurement
    cache: Measurement
    cache_is_input_subset: bool | None
    billing_basis: str = Field(min_length=1, max_length=500)


class Milestone(Strict):
    deliverable_count: int = Field(gt=0)
    elapsed_seconds: Measurement


class Report(Strict):
    report_version: Literal['cost-baseline-1']
    run_id: str = Field(min_length=1, max_length=120)
    release: str = Field(min_length=1, max_length=200)
    spec_version: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=120)
    metrics_version: str = Field(min_length=1, max_length=120)
    config_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    sample_kind: Literal['synthetic', 'fixed_capture', 'real']
    sample_description: str = Field(min_length=1, max_length=1000)
    environment: str = Field(min_length=1, max_length=1000)
    tools: list[str] = Field(min_length=1, max_length=50)
    started_at: str = Field(min_length=1, max_length=80)
    ended_at: str = Field(min_length=1, max_length=80)
    wall_seconds: Measurement
    active_seconds: Measurement
    paused_seconds: Measurement
    first_delivery_seconds: Measurement
    first_five_seconds: Measurement
    delivery_milestones: list[Milestone] = Field(max_length=1000)
    candidates_deduplicated: int = Field(ge=0)
    admitted: int = Field(ge=0)
    deliverable: int = Field(ge=0)
    failures: dict[str, int]
    missing_fields: dict[str, int]
    stages_seconds: dict[str, Measurement]
    calls: dict[str, Measurement]
    returned_characters: Measurement
    agent_turns: Measurement
    tokens: TokenCounts

    @model_validator(mode='after')
    def check_counts(self):
        if not self.deliverable <= self.admitted <= self.candidates_deduplicated:
            raise ValueError('funnel counts must be deliverable <= admitted <= candidates')
        if any(v < 0 for v in (*self.failures.values(), *self.missing_fields.values())):
            raise ValueError('counts must be nonnegative')
        if self.deliverable < 5 and self.first_five_seconds.value is not None:
            raise ValueError('first five cannot be measured with fewer than five deliveries')
        return self


def summarize(report: Report) -> dict:
    active = report.active_seconds.value
    input_tokens, output_tokens = report.tokens.input.value, report.tokens.output.value
    # Input already includes cached input when the provider defines it as a subset.
    return {
        'deliverable_per_active_hour': report.deliverable * 3600 / active if active else None,
        'input_plus_output_tokens_per_deliverable': (
            (input_tokens + output_tokens) / report.deliverable
            if report.deliverable and input_tokens is not None and output_tokens is not None
            and report.tokens.input.scope == report.tokens.output.scope
            and report.tokens.input.source == report.tokens.output.source
            and (report.tokens.cache_is_input_subset is True or report.tokens.cache.value == 0) else None),
        'first_five_seconds': report.first_five_seconds.value,
        # A report alone cannot establish auditability, fixed-sample replay or consent.
        'g0_passed': False,
        'g0_note': 'Manual audit and fixed-sample replay are required; this calculator does not certify G0.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Redacted JSON report; do not include credentials or suppression lists')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = Report.model_validate_json(args.input.read_text(encoding='utf-8-sig'))
    result = {'tool_version': TOOL_VERSION, 'report': report.model_dump(), 'derived': summarize(report)}
    if args.output.resolve() == args.input.resolve():
        parser.error('output must differ from input')
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
