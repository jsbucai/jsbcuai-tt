import copy
import pytest
from core.api_source import validate_snapshot, SourceIncomplete
from test_business_rules import facts,event


def test_old_snapshot_still_accepted_without_observation_fields():
    validate_snapshot(facts())


def test_timestamp_evidence_is_preserved_by_validation():
    data=facts(events=[dict(event(),posted_at='2026-09-25T10:00:01+08:00')])
    data['receipt_covered_through']='2026-09-25T10:00:10+08:00'
    data['inventory'][0]['stock_observed_at']='2026-09-25T10:00:05+08:00'
    original=copy.deepcopy(data)
    assert validate_snapshot(data)==original


@pytest.mark.parametrize('value',['2026-09-25T10:00:01','not-a-date',123])
@pytest.mark.parametrize('where',['snapshot','inventory','event'])
def test_invalid_source_evidence_is_rejected(value,where):
    data=facts(events=[event()])
    target,key=(data,'receipt_covered_through') if where=='snapshot' else (
        (data['inventory'][0],'stock_observed_at') if where=='inventory' else
        (data['receipt_events'][0],'posted_at'))
    target[key]=value
    with pytest.raises(SourceIncomplete,match='观察时间'):
        validate_snapshot(data)
