from fastapi import APIRouter,Request,Body
from app import store
from app.errors import RuleError
from app.modules.closing import service
from app.modules.closing.calendar import parse_day
from app.web import render

router=APIRouter()

def _day(payload):
    day=payload.get('day')
    if not day:raise RuleError('DAY','必须指定业务日')
    return parse_day(day).isoformat()

def _run_id(payload):
    run_id=payload.get('run_id')
    return str(run_id) if run_id else None

@router.get('/closing')
def view(request:Request,day:str|None=None,policy:str='verifiable'):
    bundle=store.latest()
    days=service.days_for(bundle['manifest']['run_id']) if bundle else []
    selected=parse_day(day).isoformat() if day else (days[0] if days else None)
    review=None;notice=None
    if bundle and selected:
        try:review=service.review(selected,bundle['manifest']['run_id'],policy)
        except RuleError as error:
            if error.code!='NO_REFERENCES':raise
            notice=error.message
    return render(request,'closing',review=review,days=days,selected=selected,policy=policy,notice=notice)

@router.get('/api/closing/days')
def days(run_id:str|None=None):return {'days':service.days_for(run_id)}

@router.get('/api/closing')
def official(day:str):
    return service.overview(parse_day(day).isoformat())

@router.get('/api/closing/review')
def review(day:str,run_id:str|None=None,policy:str='verifiable'):
    return service.review(parse_day(day).isoformat(),run_id,policy)

@router.get('/api/closing/draft')
def preview(day:str,run_id:str|None=None,policy:str='verifiable'):
    return service.draft(day,run_id,policy)

@router.post('/api/closing/publish')
def publish(payload:dict=Body(default={})):
    return service.publish(_day(payload),_run_id(payload),payload.get('policy','verifiable'))

@router.post('/api/closing/confirm')
def confirm(payload:dict=Body(default={})):
    version=payload.get('version')
    if not isinstance(version,int):raise RuleError('VERSION','版本必须为整数')
    return service.confirm(_day(payload),version,str(payload.get('note','')))

@router.post('/api/closing/restore')
def restore(payload:dict=Body(default={})):
    version=payload.get('version')
    if not isinstance(version,int):raise RuleError('VERSION','版本必须为整数')
    return service.restore(_day(payload),version)

@router.get('/api/closing/diff')
def diff(day:str):return service.diff(parse_day(day).isoformat())

@router.get('/api/closing/verify')
def verify(day:str):return service.verify(parse_day(day).isoformat())
