import hashlib,json
from decimal import Decimal

from app import store
from app.config import RULE_VERSION
from app.errors import RuleError
from app.modules.closing.calendar import days_of_bundle, parse_day
from app.modules.closing.ledger import POLICY_LABELS, POLICIES, build_day, compare_ledgers, same_content


def closing_key(day,run_id,policy):
    canonical=json.dumps({'day':day,'run_id':run_id,'policy':policy,'rule':RULE_VERSION},
                         sort_keys=True,separators=(',',':'))
    return 'close:%s:%s'%('v1',hashlib.sha256(canonical.encode()).hexdigest()[:32])


def _bundle(run_id):
    bundle=store.load(run_id) if run_id else store.latest()
    if not bundle:raise RuleError('NOT_FOUND','批次不存在',404)
    return bundle


def days_for(run_id=None):
    bundle=_bundle(run_id)
    return days_of_bundle(bundle)


def _previous(day):
    return store.active_ledger(day)


def _guard_policy(policy):
    if policy not in POLICIES:raise RuleError('POLICY','关账策略只能是 %s'%'/'.join(POLICIES))


def draft(day,run_id=None,policy='verifiable'):
    """只读预演：与发布使用同一构建函数，展示与正式账同口径。"""
    _guard_policy(policy)
    bundle=_bundle(run_id)
    ledger=build_day(bundle,parse_day(day).isoformat(),policy)
    previous=_previous(ledger['day'])
    ledger['changes']=compare_ledgers(previous,ledger)
    ledger['previous_version']=previous['version'] if previous else None
    ledger['publishable']=_publishable(ledger,policy)
    ledger['blockers']=_blockers(ledger,policy)
    ledger['matches_official']=bool(previous and same_content(previous,ledger))
    return ledger


def review(day,run_id=None,policy='verifiable'):
    """复核页所需的一次性视图：候选账、正式账、差异与同口径校验。"""
    resolved=parse_day(day).isoformat()
    preview=draft(resolved,run_id,policy)
    official=store.active_ledger(resolved)
    consistency=_consistency(preview,official)
    versions=store.ledger_versions(resolved)
    previous=next((v for v in versions if not v['active']),None)
    return {
        'day':resolved,
        'draft':preview,
        'official':official,
        'changes':preview['changes'],
        'consistency':consistency,
        'versions':versions,
        'previous_version':previous['version'] if previous else None,
        'confirmations':store.confirmations(resolved),
        'late_handovers':preview['late_handovers'],
        'uncovered':preview['uncovered_segments'],
        'unknown':preview['unknown_segments'],
    }


def _consistency(preview,official):
    if official is None:
        return {'comparable':False,'same_caliber':None,
                'note':'该业务日尚未发布正式账，当前为预演口径'}
    fields=('span_minutes','covered_minutes','normal_minutes','excess_minutes',
            'degree_minutes','unknown_minutes','uncovered_minutes')
    equal=all(preview['totals'][field]==official['totals'][field] for field in fields)
    return {'comparable':True,'same_caliber':equal,
            'fields':{field:{'draft':preview['totals'][field],'official':official['totals'][field],
                             'equal':preview['totals'][field]==official['totals'][field]} for field in fields},
            'note':'预演与正式账使用同一构建函数；不一致说明正式账来自另一批次或参数' if not equal else '预演与正式账逐项一致'}


def _blockers(ledger,policy):
    blockers=[]
    if not ledger['batches']:blockers.append({'code':'EMPTY','detail':'该业务日没有可计入的批次'})
    if not ledger['checks']['balanced']:blockers.append({'code':'UNBALANCED','detail':'分钟或不变量不平'})
    if policy=='hold':
        if Decimal(ledger['totals']['unknown_minutes']):blockers.append({'code':'UNKNOWN','detail':'仍存在缺测分钟，须等补齐'})
        if Decimal(ledger['totals']['uncovered_minutes']):blockers.append({'code':'UNCOVERED','detail':'仍存在未覆盖分钟，须等交接补齐'})
    return blockers


def _publishable(ledger,policy):
    if not ledger['batches']:return False
    if policy=='hold' and (Decimal(ledger['totals']['unknown_minutes']) or Decimal(ledger['totals']['uncovered_minutes'])):
        return False
    return True


def _withdraw_reason(ledger,policy):
    missing=[]
    if Decimal(ledger['totals']['unknown_minutes']):missing.append({'code':'UNKNOWN','minutes':ledger['totals']['unknown_minutes'],'detail':'存在缺测未覆盖分钟'})
    if Decimal(ledger['totals']['uncovered_minutes']):missing.append({'code':'UNCOVERED','minutes':ledger['totals']['uncovered_minutes'],'detail':'存在无责任方或责任争议分钟'})
    reasons=sorted({segment['reason'] for segment in ledger['uncovered_segments']})
    return {'policy':policy,'policy_label':POLICY_LABELS[policy],'missing':missing,'uncovered_reasons':reasons}


def publish(day,run_id=None,policy='verifiable'):
    """幂等发布：同一 (day,run_id,policy,规则版本) 只产生一个版本，分钟数不累加。"""
    _guard_policy(policy)
    bundle=_bundle(run_id)
    resolved=parse_day(day).isoformat()
    key=closing_key(resolved,bundle['manifest']['run_id'],policy)

    def finalize(previous,version):
        ledger=build_day(bundle,resolved,policy)
        ledger['version']=version
        ledger['previous_version']=previous['version'] if previous else None
        ledger['changes']=compare_ledgers(previous,ledger)
        ledger['publishable']=True
        if not ledger['checks']['balanced']:
            raise RuleError('UNBALANCED','分日账本未自洽，拒绝发布',409,{'checks':ledger['checks']})
        if not _publishable(ledger,policy):
            raise RuleError('NEEDS_EVIDENCE','仍有未证实分钟，按当前策略应保留旧账',409,
                            {'reason':_withdraw_reason(ledger,policy),'changes':ledger['changes'],
                             'blockers':_blockers(ledger,policy)})
        if previous and same_content(previous,ledger):
            return None
        return ledger

    ledger,replayed=store.publish_ledger(resolved,key,bundle['manifest']['run_id'],policy,finalize)
    ledger['idempotent']=replayed
    return ledger


def confirm(day,version,note=''):
    parse_day(day)
    return store.confirm(parse_day(day).isoformat(),int(version),note)


def restore(day,target_version):
    parse_day(day)
    ledger,replayed=store.restore_ledger(parse_day(day).isoformat(),int(target_version))
    ledger['idempotent']=replayed
    return ledger


def overview(day=None):
    if day is None:return {'days':store.ledger_days(),'active':store.active_ledgers()}
    resolved=parse_day(day).isoformat()
    active=store.active_ledger(resolved)
    versions=store.ledger_versions(resolved)
    return {'day':resolved,'active':active,'versions':versions,'confirmations':store.confirmations(resolved)}


def diff(day):
    resolved=parse_day(day).isoformat()
    versions=store.ledger_versions(resolved,with_payload=True)
    if len(versions)<2:return {'day':resolved,'from_version':None,'to_version':versions[0]['version'] if versions else None,'changed':False,'batches':[],'totals':{}}
    current,previous=versions[0],versions[1]
    out=compare_ledgers(previous,current)
    out['day']=resolved
    return out


def verify(day):
    resolved=parse_day(day).isoformat()
    ledger=store.active_ledger(resolved)
    if not ledger:raise RuleError('NOT_FOUND','该业务日尚未发布账本',404)
    checks=ledger['checks']
    return {'day':resolved,'version':ledger['version'],'balanced':checks['balanced'],
            'basis':checks['basis'],'minute_delta':checks['minute_delta'],
            'owner_excess_delta':checks['owner_excess_delta'],
            'official_totals':ledger['totals'],'confirmed':bool(store.confirmations(resolved) and any(c['version']==ledger['version'] for c in store.confirmations(resolved)))}
