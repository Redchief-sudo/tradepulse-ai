"""The operator command needs closed/filled orders and complete pagination."""
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from test_rev149_open_orders import BASE, ORDERS_URL, _order

from tradepulse.broker import AlpacaClient
from tradepulse.broker.errors import AlpacaDataIntegrityError


@pytest.mark.parametrize('exclusive', [False, True])
async def test_history_paginates_all_statuses_with_overlapping_timestamps(exclusive):
    population = [_order(i, BASE + timedelta(seconds=i)) for i in range(510)]
    population[501]['status'] = 'filled'
    requests = []

    def serve(request):
        params = dict(request.url.params)
        requests.append(params)
        assert params['status'] == 'all'
        after = datetime.fromisoformat(params['after'])
        rows = [o for o in population if datetime.fromisoformat(o['submitted_at']) > after
                or (not exclusive and datetime.fromisoformat(o['submitted_at']) == after)]
        return httpx.Response(200, json=rows[:int(params['limit'])])
    pages = []
    with respx.mock:
        respx.get(ORDERS_URL).mock(side_effect=serve)
        async with AlpacaClient('key', 'secret', 'paper', 10) as broker:
            result = await broker.get_order_history(BASE, page_evidence=pages)
    assert len(result) == 510
    assert len(requests) == 2
    assert [p['terminal'] for p in pages] == [False, True]
    assert next(o for o in result if o.broker_order_id == 'order-00501').status == 'filled'


async def test_history_fails_closed_on_saturated_timestamp():
    population = [_order(i, BASE) for i in range(501)]
    with respx.mock:
        respx.get(ORDERS_URL).mock(return_value=httpx.Response(200, json=population[:500]))
        async with AlpacaClient('key', 'secret', 'paper', 10) as broker:
            with pytest.raises(AlpacaDataIntegrityError):
                await broker.get_order_history(BASE, page_evidence=[])


async def test_history_refuses_missing_timestamp_even_on_short_page():
    row = _order(1, BASE)
    row['submitted_at'] = None
    with respx.mock:
        respx.get(ORDERS_URL).mock(return_value=httpx.Response(200, json=[row]))
        async with AlpacaClient('key', 'secret', 'paper', 10) as broker:
            with pytest.raises(AlpacaDataIntegrityError):
                await broker.get_order_history(BASE, page_evidence=[])
