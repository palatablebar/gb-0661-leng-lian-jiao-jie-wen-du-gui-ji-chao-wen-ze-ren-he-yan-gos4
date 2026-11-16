from app import store
from app.modules.batches.review import validate_review

def persist(bundle,raw=None):return store.save(bundle,raw)
def approve(rid,version):
    bundle=store.latest()
    if not bundle or bundle['manifest']['run_id']!=rid:
        from app.errors import RuleError
        raise RuleError('NOT_FOUND','批次不存在或不是当前批次',404)
    validate_review(bundle)
    return store.review(rid,version)
