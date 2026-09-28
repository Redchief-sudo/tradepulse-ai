"""Position-value observation: broker cent rounding and coordinated re-reads.

Alpaca's account endpoint reports long_market_value rounded to the cent while
the positions endpoint reports exact marks, and the two are separate HTTP
responses. Real soak evidence (2026-09-28): 10 of 12 failed checks were pure
cent rounding (e.g. 3388.937475 vs 3388.94) and 2 were quote moves between
the calls (0.10138, 0.05368).
"""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from python_tests.test_forensic_corrections import NOW, account, position, repos
from tradepulse.broker.types import AlpacaPosition
from tradepulse.models import AssetClass
from tradepulse.valuation import marked_snapshot, observe_broker_valuation, position_value_observation


def test_exact_equality_is_equal():
    assert position_value_observation([D('3388.94')], D('3388.94')) == (D(0), 'equal_uncoordinated_observations')


def test_real_soak_cent_rounding_is_equal_after_broker_rounding():
    difference, status = position_value_observation([D('3388.937475')], D('3388.94'))
    assert difference == D('-0.002525')  # exact difference preserved for audit
    assert status == 'equal_after_broker_cent_rounding'


def test_per_position_cent_rounding_is_equal_after_broker_rounding():
    # total rounding gives 20.01; per-position rounding gives 20.02
    assert position_value_observation([D('10.006'), D('10.006')], D('20.02'))[1] == 'equal_after_broker_cent_rounding'
    assert position_value_observation([D('10.006'), D('10.006')], D('20.01'))[1] == 'equal_after_broker_cent_rounding'


@pytest.mark.parametrize('observed', ['0.10138', '0.05368', '0.01'])
def test_quote_moves_beyond_rounding_stay_different(observed):
    market = D('3391.48') + D(observed)
    assert position_value_observation([market], D('3391.48'))[1] == 'different_uncoordinated_observations'


def test_no_rounding_model_for_an_unrounded_broker_value():
    # A broker value that is not a whole-cent amount was not cent-rounded, so
    # no rounding model applies and only exact equality counts.
    assert position_value_observation([D('20.246913579')], D('20.246913578'))[1] == 'different_uncoordinated_observations'


async def test_cent_rounded_snapshot_has_same_invariants_as_exact_snapshot(tmp_path):
    r = await repos(tmp_path)
    p = replace(position(), market_value=D('3388.937475'))
    rounded = await marked_snapshot(r, account('3388.94'), [p], now=NOW)
    exact = await marked_snapshot(r, account('3388.937475'), [p], now=NOW)
    moved = await marked_snapshot(r, account('3388.84'), [p], now=NOW)
    assert rounded.position_value_observation_status == 'equal_after_broker_cent_rounding'
    assert rounded.position_value_observation_difference == D('-0.002525')
    assert rounded.reconciliation_results['mandatory_invariants'] == exact.reconciliation_results['mandatory_invariants']
    assert rounded.equity_reconciliation_status == exact.equity_reconciliation_status
    assert moved.position_value_observation_status == 'different_uncoordinated_observations'
    assert moved.reconciliation_results['mandatory_invariants'] == 'incomplete_or_mismatched'


def _pos(value):
    return AlpacaPosition('AAPL', AssetClass.EQUITY, D('9.977'), D('340.15'), D(value), D(1), D(0))


class ScriptedBroker:
    def __init__(self, marks):
        self.marks = list(marks)
        self.account_calls = 0
        self.position_calls = 0

    async def get_positions(self):
        value = self.marks[min(self.position_calls, len(self.marks) - 1)]
        self.position_calls += 1
        return [_pos(value)]

    async def get_account(self):
        self.account_calls += 1
        return self.account_calls


async def test_stable_marks_need_one_account_read():
    broker = ScriptedBroker(['3388.94', '3388.94'])
    acct, positions = await observe_broker_valuation(broker)
    assert (acct, broker.account_calls, broker.position_calls) == (1, 1, 2)
    assert positions[0].market_value == D('3388.94')


async def test_moving_marks_are_reread_until_they_bracket_the_account_read():
    broker = ScriptedBroker(['3391.48', '3391.58', '3391.58'])
    acct, positions = await observe_broker_valuation(broker)
    assert (acct, broker.account_calls, broker.position_calls) == (2, 2, 3)
    assert positions[0].market_value == D('3391.58')


async def test_persistently_moving_marks_return_the_last_observation_unhidden():
    broker = ScriptedBroker(['1', '2', '3', '4', '5'])
    acct, positions = await observe_broker_valuation(broker, attempts=3)
    assert (acct, broker.account_calls, broker.position_calls) == (3, 3, 4)
    assert positions[0].market_value == D(4)
