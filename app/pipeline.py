import json,hashlib,time
from decimal import Decimal
from app.config import SAMPLES,MAX_BYTES
from app.errors import RuleError
from app.manifest import make_manifest
from app.modules.intake.service import ingest
from app.modules.contract.service import normalize
from app.modules.contract.normalize import instant,number
from app.modules.model.service import partition
from app.modules.calculate.service import calculate
from app.modules.quarantine.service import quarantine
from app.modules.reconcile.service import reconcile

def _load(name):
    return json.loads((SAMPLES/name).read_text(encoding='utf-8'))

def _digest(value):
    canonical=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()

def _check_handovers(value):
    if not isinstance(value,dict) or not value:raise RuleError('HANDOVER','交接档案必须是按批号分组的对象')
    checked={}
    for shipment,entries in value.items():
        if not isinstance(shipment,str) or not shipment.strip():raise RuleError('HANDOVER','交接批号不能为空')
        if not isinstance(entries,list) or not entries:raise RuleError('HANDOVER','交接记录必须是非空数组')
        clean=[]
        for index,entry in enumerate(entries):
            if not isinstance(entry,dict):raise RuleError('HANDOVER','交接记录必须是对象')
            start=instant(str(entry.get('start','')));end=instant(str(entry.get('end','')))
            if end<=start:raise RuleError('INTERVAL','交接结束必须晚于开始')
            owner=str(entry.get('owner','')).strip()
            if not owner:raise RuleError('HANDOVER','交接责任方不能为空')
            record={'start':start.isoformat(),'end':end.isoformat(),'owner':owner}
            if entry.get('received_at') not in (None,''):
                record['received_at']=instant(str(entry['received_at'])).isoformat()
            clean.append(record)
        checked[shipment.strip()]=sorted(clean,key=lambda e:(e['start'],e['end'],e['owner']))
    return checked

def _check_calibrations(value):
    if not isinstance(value,dict) or not value:raise RuleError('CALIBRATION','校准档案必须是非空对象')
    checked={}
    for probe,offset in value.items():
        if not isinstance(probe,str) or not probe.strip():raise RuleError('CALIBRATION','探头编号不能为空')
        number_value=number(offset)
        if not Decimal('-80')<=number_value<=Decimal('80'):raise RuleError('CALIBRATION','校准偏移超出−80到80°C')
        checked[probe.strip()]=str(number_value)
    return checked

def _references(handovers,calibrations):
    return {'handovers':handovers,'calibrations':calibrations,
            'handovers_sha256':_digest(handovers),'calibrations_sha256':_digest(calibrations)}

def execute(raw,threshold='8',max_gap=15,handovers=None,calibrations=None):
    limit=number(threshold)
    if limit < -80 or limit > 80 or not 1<=max_gap<=120:raise RuleError('PARAMETER','阈值或缺测分钟超出范围')
    started=time.perf_counter()
    if handovers is None:handovers=_load('handovers.json')
    if calibrations is None:calibrations=_load('calibrations.json')
    handovers=_check_handovers(handovers);calibrations=_check_calibrations(calibrations)
    references=_references(handovers,calibrations)
    intake=ingest(raw);contract=normalize(intake['rows'],calibrations,handovers)
    model=partition(contract['rows'],handovers,max_gap);calc=calculate(model['segments'],str(limit))
    isolated=quarantine(intake['issues'],contract['issues'],model['issues'])
    checked=reconcile(intake,model,isolated,calc)
    manifest=make_manifest(raw,{'threshold':str(limit),'max_gap':max_gap},
        {'calibrations':references['calibrations_sha256'],'handovers':references['handovers_sha256']})
    manifest['elapsed_ms']=round((time.perf_counter()-started)*1000,3)
    return {'manifest':manifest,'inputs':references,'intake':intake,'contract':contract,'model':model,
            'calculate':calc,'quarantine':isolated,'reconcile':checked}

def sample_bytes(name='dirty'):
    if name not in ('dirty','clean','gb18030'):raise RuleError('SAMPLE','样例不存在',404)
    return (SAMPLES/(name+'.csv')).read_bytes()
