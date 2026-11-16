from datetime import date,datetime,timedelta,timezone
from app.config import CLOSING_CUTOFF_HOUR,OPERATING_OFFSET
from app.errors import RuleError

LOCAL=timezone(timedelta(hours=OPERATING_OFFSET))

def parse_day(value):
    try:return date.fromisoformat(str(value))
    except ValueError:raise RuleError('DAY','业务日格式必须为 YYYY-MM-DD')

def day_window(value):
    """业务日窗口为当地 [00:00, 次日00:00)，关账截止为次日 10:00。"""
    d=parse_day(value)
    start=datetime(d.year,d.month,d.day,tzinfo=LOCAL)
    end=start+timedelta(days=1)
    cutoff=end+timedelta(hours=CLOSING_CUTOFF_HOUR)
    return {'day':d.isoformat(),'start':start,'end':end,'cutoff':cutoff}

def business_day(value):
    moment=datetime.fromisoformat(value)
    if moment.tzinfo is None:moment=moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL).date().isoformat()

def days_of_bundle(bundle):
    """批次实际跨越的业务日；跨零点批次会同时给出前后两天。"""
    days={business_day(r['at']) for r in bundle.get('contract',{}).get('rows',[])}
    for entries in bundle.get('inputs',{}).get('handovers',{}).values():
        for entry in entries:
            days.add(business_day(entry['start']))
            days.add(business_day(entry['end']))
    if not days:raise RuleError('EMPTY_DAY','批次没有可用于关账的采样或交接',422)
    return sorted(days)
