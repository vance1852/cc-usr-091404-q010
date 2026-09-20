"""Conservation laws: checkout/return/aliquot quantities and single custody."""
from __future__ import annotations

import pytest

from app import queries, services
from app.errors import LifecycleError


def _receive(session, cid="C-1", qty=100.0, **kw):
    params = dict(
        container_id=cid, batch_id="B1", material="原液", quantity=qty,
        unit="mL", location="低温库A", actor="alice",
    )
    params.update(kw)
    return services.receive_sample(session, **params)


def test_checkout_then_full_return_conserves(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=40, purpose="检验", actor="alice")
    services.return_sample(session, container_id="C-1", returned_qty=40,
                           consumed_qty=0, to_location="低温库A", actor="bob")
    proj = services._proj(session, "C-1")
    assert proj.quantity == pytest.approx(100.0)
    assert proj.checked_out_qty == pytest.approx(0.0)
    assert proj.custodian is None
    assert proj.freeze_thaw_count == 1


def test_checkout_partial_consumption_conserves(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=40, purpose="检验", actor="alice")
    services.return_sample(session, container_id="C-1", returned_qty=35,
                           consumed_qty=5, to_location="低温库A", actor="bob")
    proj = services._proj(session, "C-1")
    assert proj.quantity == pytest.approx(95.0)
    assert proj.checked_out_qty == pytest.approx(0.0)


def test_cannot_return_more_than_checked_out(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=10, purpose="检验", actor="alice")
    with pytest.raises(LifecycleError, match="数量不守恒"):
        services.return_sample(session, container_id="C-1", returned_qty=9,
                               consumed_qty=2, to_location="低温库A", actor="bob")


def test_cannot_checkout_more_than_on_hand(session):
    _receive(session, qty=10)
    with pytest.raises(LifecycleError, match="超过在库余量"):
        services.checkout(session, container_id="C-1", holder="bob",
                          quantity=11, purpose="检验", actor="alice")


def test_single_custodian_enforced(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=10, purpose="检验", actor="alice")
    with pytest.raises(LifecycleError, match="只能有一个有效持有人"):
        services.checkout(session, container_id="C-1", holder="carol",
                          quantity=5, purpose="检验", actor="alice")


def test_cannot_move_while_checked_out(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=10, purpose="检验", actor="alice")
    with pytest.raises(LifecycleError, match="归还前不得移动"):
        services.move(session, container_id="C-1", to_location="低温库B", actor="alice")


def test_exactly_one_effective_location_after_return(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=10, purpose="检验", actor="alice")
    services.return_sample(session, container_id="C-1", returned_qty=10,
                           consumed_qty=0, to_location="低温库A", actor="bob")
    status = queries.container_status(session, "C-1")
    assert status["location"] == "低温库A"
    assert status["custodian"] is None
    assert status["checked_out_qty"] == 0


def test_aliquot_conserves_parent_volume(session):
    _receive(session, cid="MOM", qty=100)
    services.aliquot(
        session, parent_container_id="MOM",
        children=[{"container_id": "K1", "quantity": 30},
                  {"container_id": "K2", "quantity": 25.5}],
        storage_location="架2", actor="alice",
    )
    mom = services._proj(session, "MOM")
    k1 = services._proj(session, "K1")
    k2 = services._proj(session, "K2")
    assert mom.quantity == pytest.approx(44.5)
    assert k1.quantity == pytest.approx(30)
    assert k2.quantity == pytest.approx(25.5)
    assert k1.parent_container_id == "MOM"


def test_aliquot_cannot_exceed_parent_volume(session):
    _receive(session, cid="MOM", qty=10)
    with pytest.raises(LifecycleError, match="分装量不守恒"):
        services.aliquot(
            session, parent_container_id="MOM",
            children=[{"container_id": "K1", "quantity": 11}],
            storage_location="架2", actor="alice",
        )


def test_duplicate_child_id_rejected_and_no_partial_events(session):
    _receive(session, cid="MOM", qty=100)
    with pytest.raises(LifecycleError, match="子容器标识重复"):
        services.aliquot(
            session, parent_container_id="MOM",
            children=[{"container_id": "K1", "quantity": 10},
                      {"container_id": "K1", "quantity": 10}],
            storage_location="架2", actor="alice",
        )
    # rolled back by caller's session boundary; within this session nothing
    # was appended for the parent either
    from app.models import Event
    assert session.query(Event).filter_by(container_id="MOM").count() == 1


def test_partial_return_then_final_settlement(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=30, purpose="检验", actor="alice")
    services.return_sample(session, container_id="C-1", returned_qty=20,
                           consumed_qty=0, to_location="低温库A", actor="bob")
    proj = services._proj(session, "C-1")
    # still partly checked out: custodian & effective location stay at point of use
    assert proj.checked_out_qty == pytest.approx(10)
    assert proj.custodian == "bob"
    services.return_sample(session, container_id="C-1", returned_qty=5,
                           consumed_qty=5, to_location="低温库A", actor="bob")
    proj = services._proj(session, "C-1")
    assert proj.quantity == pytest.approx(95)
    assert proj.custodian is None


def test_quantity_evolution_balances_match_projection(session):
    _receive(session)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=40, purpose="检验", actor="alice")
    services.return_sample(session, container_id="C-1", returned_qty=30,
                           consumed_qty=10, to_location="低温库A", actor="bob")
    evolution = queries.quantity_evolution(session, "C-1")
    assert evolution["balances_consistent"] is True
    assert evolution["final_on_hand_qty"] == pytest.approx(90)
    assert evolution["conservation_violations"] == []
