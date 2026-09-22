"""Complete activity capture with durable boundary resumption and late-fee audit.

Alpaca can post CFEE rows with IDs preceding the last FILL. A tail cursor alone
cannot prove completeness. Resume that cursor, then also audit the unfiltered
history; neither a wall-clock delay nor an ID cutoff establishes fee finality.
"""
from hashlib import sha256

from tradepulse.persistence.codec import encode_payload

from .asset_fees import AssetFeeIntegrityError


async def activity_population(broker, cursor=None):
    tail_pages = []
    tail = None
    if cursor:
        tail = await broker.get_activities(activity_type=None, after_id=cursor, page_evidence=tail_pages)
    pages = []
    full = await broker.get_activities(activity_type=None, page_evidence=pages)
    if not pages or not pages[-1]['terminal'] or any(p['terminal'] for p in pages[:-1]):
        raise AssetFeeIntegrityError('ACTIVITY_PAGINATION_INCOMPLETE')
    raw = [dict(a.raw) for a in full]
    by_id = {r['id']: r for r in raw}
    if len(by_id) != len(raw):
        raise AssetFeeIntegrityError('ACTIVITY_PAGINATION_REPEATED')
    if cursor and cursor not in by_id:
        raise AssetFeeIntegrityError('ACTIVITY_CURSOR_BOUNDARY_MISSING')
    if tail is not None:
        if not tail_pages or not tail_pages[-1]['terminal']:
            raise AssetFeeIntegrityError('ACTIVITY_RESUME_INCOMPLETE')
        if any(by_id.get(a.activity_id) != dict(a.raw) for a in tail):
            raise AssetFeeIntegrityError('ACTIVITY_CAPTURE_CHANGED_DURING_PAGINATION')
    proof = {'method': 'resume_boundary_and_full_history_audit', 'resumed_from': cursor,
             'pages': pages, 'resume_pages': tail_pages,
             'activity_ids': [r['id'] for r in raw], 'complete': True,
             'population_hash': sha256(encode_payload(raw).encode()).hexdigest()}
    return raw, proof


def validate_pagination(raw, proof):
    if not isinstance(proof, dict) or proof.get('complete') is not True:
        raise AssetFeeIntegrityError('ACTIVITY_PAGINATION_INCOMPLETE')
    if proof.get('population_hash') != sha256(encode_payload(raw).encode()).hexdigest():
        raise AssetFeeIntegrityError('ACTIVITY_POPULATION_HASH_MISMATCH')
    if proof.get('activity_ids') != [r['id'] for r in raw]:
        raise AssetFeeIntegrityError('ACTIVITY_PAGE_POPULATION_MISMATCH')
    pages = proof.get('pages', [])
    if not pages:
        raise AssetFeeIntegrityError('ACTIVITY_PAGINATION_INCOMPLETE')
    offset = 0
    boundary = None
    for i, page in enumerate(pages):
        request = page['request']
        limit = int(request['page_size'])
        ids = page['activity_ids']
        segment = raw[offset:offset+len(ids)]
        if (request.get('direction') != 'asc' or request.get('page_token') != boundary
                or any(k in request for k in ('after', 'until', 'activity_types'))
                or ids != [r['id'] for r in segment] or not 1 <= limit <= 100
                or len(ids) > limit or page['terminal'] != (len(ids) < limit)
                or page['response_hash'] != sha256(encode_payload(segment).encode()).hexdigest()
                or page['terminal'] != (i == len(pages)-1)):
            raise AssetFeeIntegrityError('ACTIVITY_PAGINATION_INVALID')
        offset += len(ids)
        boundary = ids[-1] if ids else boundary
    if offset != len(raw):
        raise AssetFeeIntegrityError('ACTIVITY_PAGINATION_INCOMPLETE')
