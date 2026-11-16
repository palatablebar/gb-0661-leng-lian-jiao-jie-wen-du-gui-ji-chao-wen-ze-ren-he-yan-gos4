import json
from decimal import Decimal

import pytest

from app import store
from app.errors import RuleError
from app.modules.closing import service
from app.modules.closing.calendar import business_day,days_of_bundle,day_window
from app.modules.closing.ledger import build_day,compare_ledgers
from app.pipeline import execute,sample_bytes

END_OF_DAY='2026-11-02T23:00:00+08:00'

def csv_bytes(rows):
    head='shipment,probe,at,value,unit\n'
    return (head+''.join('%s,%s,%s,%s,%s\n'%row for row in rows)).encode()

CROSS_MIDNIGHT=csv_bytes([
 ('00099','P1','2026-11-02T23:00:00+08:00','4','C'),
 ('00099','P1','2026-11-02T23:10:00+08:00','12','C'),
 ('00099','P1','2026-11-02T23:20:00+08:00','12','C'),
 ('00099','P1','2026-11-02T23:50:00+08:00','4','C'),
 ('00099','P1','2026-11-03T00:10:00+08:00','2','C'),
 ('00099','P1','2026-11-03T00:30:00+08:00','2','C'),
])

SIMPLE=csv_bytes([
 ('00099','P1','2026-11-02T09:00:00+08:00','4','C'),
 ('00099','P1','2026-11-02T09:10:00+08:00','12','C'),
 ('00099','P1','2026-11-02T09:20:00+08:00','12','C'),
 ('00099','P1','2026-11-02T09:30:00+08:00','4','C'),
 ('00099','P1','2026-11-02T09:40:00+08:00','4','C'),
 ('00099','P1','2026-11-02T10:00:00+08:00','2','C'),
])

CROSS_HANDOVERS={'00099':[
 {'start':'2026-11-02T23:00:00+08:00','end':'2026-11-02T23:30:00+08:00','owner':'北岸冷运'},
 {'start':'2026-11-02T23:30:00+08:00','end':'2026-11-03T00:30:00+08:00','owner':'滨江仓储'},
]}

def with_received(handovers,index,stamp):
    copy=json.loads(json.dumps(handovers));copy['00099'][index]['received_at']=stamp;return copy

def test_business_day_uses_local_offset():
    assert business_day('2026-11-02T16:30:00Z')=='2026-11-03'
    assert business_day('2026-11-02T15:59:00Z')=='2026-11-02'
    window=day_window('2026-11-02')
    assert window['cutoff'].isoformat()=='2026-11-03T10:00:00+08:00'

def test_cross_midnight_splits_days():
    bundle=execute(CROSS_MIDNIGHT,handovers=CROSS_HANDOVERS)
    assert days_of_bundle(bundle)==['2026-11-02','2026-11-03']
    first=build_day(bundle,'2026-11-02');second=build_day(bundle,'2026-11-03')
    assert Decimal(first['totals']['span_minutes'])==Decimal('60')
    assert Decimal(second['totals']['span_minutes'])==Decimal('30')
    assert first['checks']['balanced'] and second['checks']['balanced']
    covered=Decimal(first['totals']['covered_minutes'])+Decimal(second['totals']['covered_minutes'])
    assert covered==Decimal('20')

def test_each_day_counts_once():
    bundle=execute(CROSS_MIDNIGHT,handovers=CROSS_HANDOVERS)
    first=build_day(bundle,'2026-11-02');second=build_day(bundle,'2026-11-03')
    assert Decimal(first['totals']['span_minutes'])+Decimal(second['totals']['span_minutes'])==Decimal('90')

def test_long_gap_is_unknown_not_compliant():
    bundle=execute(CROSS_MIDNIGHT,handovers=CROSS_HANDOVERS)
    ledger=build_day(bundle,'2026-11-02')
    assert Decimal(ledger['totals']['unknown_minutes'])==Decimal('40')
    assert ledger['checks']['unknown_counts_as_compliant'] is False
    assert Decimal(ledger['totals']['normal_minutes'])+Decimal(ledger['totals']['excess_minutes'])==Decimal(ledger['totals']['covered_minutes'])
    for segment in ledger['unknown_segments']:
        assert segment['reason']=='missing_sample'
        assert segment['minutes']!='0.000000'

def test_late_handover_is_held_not_attributed():
    """跨零点批次交接在次日10点后才到：该段进入未覆盖，等补齐而不是算给任一方。"""
    handovers=with_received(CROSS_HANDOVERS,1,'2026-11-03T10:05:00+08:00')
    bundle=execute(CROSS_MIDNIGHT,handovers=handovers)
    ledger=build_day(bundle,'2026-11-02')
    assert Decimal(ledger['totals']['uncovered_minutes'])==Decimal('30')
    assert Decimal(ledger['pending_minutes'])==Decimal('30')
    assert all(s['reason']=='late_handover' for s in ledger['uncovered_segments'])
    assert all(s['owner'] is None for s in ledger['uncovered_segments'])
    assert ledger['late_handovers'][0]['used_for_attribution'] is False

def test_handover_gap_is_uncovered():
    handovers={'00099':[
        {'start':'2026-11-02T09:00:00+08:00','end':'2026-11-02T09:30:00+08:00','owner':'北岸冷运'},
        {'start':'2026-11-02T09:45:00+08:00','end':'2026-11-02T10:00:00+08:00','owner':'滨江仓储'},
    ]}
    ledger=build_day(execute(SIMPLE,handovers=handovers),'2026-11-02')
    assert Decimal(ledger['totals']['uncovered_minutes'])==Decimal('15')
    assert {s['reason'] for s in ledger['uncovered_segments']}=={'handover_gap'}

def test_overlap_is_disputed():
    handovers={'00099':[
        {'start':'2026-11-02T09:00:00+08:00','end':'2026-11-02T09:40:00+08:00','owner':'北岸冷运'},
        {'start':'2026-11-02T09:20:00+08:00','end':'2026-11-02T10:00:00+08:00','owner':'滨江仓储'},
    ]}
    ledger=build_day(execute(SIMPLE,handovers=handovers),'2026-11-02')
    assert Decimal(ledger['totals']['uncovered_minutes'])==Decimal('20')
    assert {s['reason'] for s in ledger['uncovered_segments']}=={'disputed'}

def test_sample_outside_handover_is_uncovered():
    handovers={'00099':[{'start':'2026-11-02T09:00:00+08:00','end':'2026-11-02T09:20:00+08:00','owner':'北岸冷运'}]}
    ledger=build_day(execute(SIMPLE,handovers=handovers),'2026-11-02')
    assert Decimal(ledger['totals']['uncovered_minutes'])==Decimal('40')
    assert Decimal(ledger['totals']['span_minutes'])==Decimal('60')
    assert Decimal(ledger['totals']['covered_minutes'])==Decimal('20')
    assert {s['reason'] for s in ledger['uncovered_segments']}=={'handover_gap'}

def test_day_ledger_matches_pipeline_on_clean_sample():
    bundle=execute(sample_bytes('clean'))
    ledger=build_day(bundle,'2026-11-02')
    assert ledger['totals']['degree_minutes']==bundle['calculate']['totals']['degree_minutes']
    assert ledger['totals']['excess_minutes']==bundle['calculate']['totals']['excess']
    assert ledger['totals']['unknown_minutes']==bundle['calculate']['totals']['unknown']

def test_owner_excess_reconciles():
    ledger=build_day(execute(sample_bytes('clean')),'2026-11-02')
    owners=sum(Decimal(row['excess_minutes']) for row in ledger['owners'])
    assert abs(owners-Decimal(ledger['totals']['excess_minutes']))<Decimal('0.00001')
    assert ledger['checks']['owner_excess_delta']=='0.000000'


# ---- 发布、幂等与回滚 ----

def test_publish_once_then_replay(client):
    first=service.publish('2026-11-02')
    second=service.publish('2026-11-02')
    assert first['version']==second['version']==1
    assert second['idempotent'] is True
    assert len(store.ledger_versions('2026-11-02'))==1
    assert store.active_ledger('2026-11-02')['totals']==first['totals']

def test_replay_does_not_add_minutes(client):
    first=service.publish('2026-11-02')
    for _ in range(3):service.publish('2026-11-02')
    ledger=store.active_ledger('2026-11-02')
    assert ledger['totals']==first['totals']
    assert Decimal(ledger['totals']['covered_minutes'])==Decimal('320')

def test_hold_policy_keeps_old_ledger(client):
    service.publish('2026-11-02')
    before=store.active_ledger('2026-11-02')
    with pytest.raises(RuleError) as error:service.publish('2026-11-02',policy='hold')
    assert error.value.code=='NEEDS_EVIDENCE'
    assert error.value.status==409
    after=store.active_ledger('2026-11-02')
    assert after['version']==before['version'] and after['totals']==before['totals']

def test_new_run_creates_version_and_diff(client):
    service.publish('2026-11-02')
    first=store.active_ledger('2026-11-02')
    client.post('/api/runs',json={'threshold':'9'})
    second=service.publish('2026-11-02')
    assert second['version']==2 and second['idempotent'] is False
    changes=service.diff('2026-11-02')
    assert changes['from_version']==1 and changes['to_version']==2
    assert store.active_ledger('2026-11-02')['version']==2
    assert first['totals']['excess_minutes']!=second['totals']['excess_minutes'] or changes['changed'] is not None

def test_restore_matches_target_totals(client):
    service.publish('2026-11-02')
    original=store.active_ledger('2026-11-02')
    client.post('/api/runs',json={'threshold':'9'})
    service.publish('2026-11-02')
    restored=service.restore('2026-11-02',original['version'])
    assert restored['totals']==original['totals']
    assert restored['restored_from']==original['version']
    assert store.active_ledger('2026-11-02')['totals']==original['totals']
    again=service.restore('2026-11-02',original['version'])
    assert again['idempotent'] is True and again['version']==restored['version']

def test_publish_failure_keeps_previous_version(client,monkeypatch):
    service.publish('2026-11-02')
    before=store.active_ledger('2026-11-02')
    client.post('/api/runs',json={'threshold':'9'})
    import app.modules.closing.service as service_module

    def explode(*args,**kwargs):raise RuntimeError('builder failed')
    monkeypatch.setattr(service_module,'build_day',explode)
    with pytest.raises(RuntimeError):service.publish('2026-11-02')
    after=store.active_ledger('2026-11-02')
    assert after['version']==before['version'] and after['totals']==before['totals']
    assert len(store.ledger_versions('2026-11-02'))==1

def test_raw_samples_and_runs_survive_closing(client):
    bundle=store.latest()
    digest=bundle['manifest']['sha256']
    service.publish('2026-11-02')
    assert store.load_raw(digest) is not None
    assert store.load(bundle['manifest']['run_id']) is not None
    assert store.find_ledger(service.closing_key('2026-11-02',bundle['manifest']['run_id'],'verifiable'))

def test_confirm_requires_active_version(client):
    service.publish('2026-11-02')
    result=service.confirm('2026-11-02',1)
    assert result['idempotent'] is False
    assert service.confirm('2026-11-02',1)['idempotent'] is True
    client.post('/api/runs',json={'threshold':'9'})
    service.publish('2026-11-02')
    with pytest.raises(RuleError) as error:service.confirm('2026-11-02',1)
    assert error.value.code=='STALE' and error.value.status==409
    assert len(store.confirmations('2026-11-02'))==1

def test_review_reports_same_caliber(client):
    service.publish('2026-11-02')
    view=service.review('2026-11-02')
    assert view['consistency']['comparable'] is True
    assert view['consistency']['same_caliber'] is True
    assert view['draft']['totals']==view['official']['totals']

def test_unknown_day_rejected(client):
    with pytest.raises(RuleError) as error:service.draft('2026-13-01')
    assert error.value.code=='DAY'
    assert client.get('/api/closing/draft?day=not-a-day').status_code==422

def test_confirmations_survive_failed_publish_and_restore(client):
    service.publish('2026-11-02')
    service.confirm('2026-11-02',1,'月初确认')
    client.post('/api/runs',json={'threshold':'9'})
    import app.modules.closing.service as service_module
    original=service_module.build_day

    def explode(*args,**kwargs):raise RuntimeError('publish failed')
    service_module.build_day=explode
    try:
        with pytest.raises(RuntimeError):service.publish('2026-11-02')
    finally:
        service_module.build_day=original
    assert [c['version'] for c in store.confirmations('2026-11-02')]==[1]
    assert store.active_ledger('2026-11-02')['version']==1
    service.publish('2026-11-02')
    assert store.active_ledger('2026-11-02')['version']==2
    service.restore('2026-11-02',1)
    assert [c['version'] for c in store.confirmations('2026-11-02')]==[1]
    assert store.active_ledger('2026-11-02')['totals']==store.ledger_versions('2026-11-02',with_payload=True)[-1]['totals']

def test_raw_sample_preserved(client):
    bundle=store.latest()
    digest=bundle['manifest']['sha256']
    assert store.load_raw(digest) is not None
    service.publish('2026-11-02')
    service.publish('2026-11-02')
    assert store.load_raw(digest) is not None
    assert store.load(bundle['manifest']['run_id'])['manifest']['sha256']==digest

def test_received_exactly_at_cutoff_is_accepted():
    handovers=with_received(CROSS_HANDOVERS,1,'2026-11-03T10:00:00+08:00')
    ledger=build_day(execute(CROSS_MIDNIGHT,handovers=handovers),'2026-11-02')
    assert ledger['late_handovers']==[]
    assert Decimal(ledger['totals']['uncovered_minutes'])==Decimal('0')

def test_midnight_boundary_half_open():
    bundle=execute(CROSS_MIDNIGHT,handovers=CROSS_HANDOVERS)
    first=build_day(bundle,'2026-11-02');second=build_day(bundle,'2026-11-03')
    assert Decimal(first['totals']['span_minutes'])+Decimal(second['totals']['span_minutes'])==Decimal('90')
    starts=[s['start'] for s in first['unknown_segments']]
    assert '2026-11-03T00:00:00+08:00' not in starts
    assert any(s['start']=='2026-11-03T00:00:00+08:00' for s in second['unknown_segments'])

def test_replay_after_newer_version_does_not_flip_back(client):
    first=service.publish('2026-11-02')
    client.post('/api/runs',json={'threshold':'9'})
    second=service.publish('2026-11-02')
    assert second['version']==2
    replay=service.publish('2026-11-02',store.latest()['manifest']['run_id'])
    assert replay['idempotent'] is True
    assert store.active_ledger('2026-11-02')['version']==2
    assert len(store.ledger_versions('2026-11-02'))==2
