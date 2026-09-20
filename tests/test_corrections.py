"""Correction events recover true state without erasing history."""
from __future__ import annotations

import pytest

from app import queries, services
from app.errors import LifecycleError
from app.models import Event, EventType
from app.projection import rebuild_all


def _receive(session, cid="C-1", qty=100.0):
    return services.receive_sample(
        session, container_id=cid, batch_id="B1", material="原液",
        quantity=qty, unit="mL", location="低温库A", actor="alice",
    )


def test_correct_wrong_move_location_recovers_true_state(session):
    _receive(session)
    services.move(session, container_id="C-1", to_location="错误库位X", actor="alice")
    assert services._proj(session, "C-1").location == "错误库位X"

    services.correct_event(
        session, container_id="C-1", target_seq=2, actor="supervisor",
        reason="扫码扫到相邻库位，实际移入低温库B",
        corrected_event_type="MOVE",
        corrected_payload={
            "from_location": "低温库A", "to_location": "低温库B",
            "label_damaged": None,
        },
    )
    # truth recovered by replay, while both records remain on the chain
    assert services._proj(session, "C-1").location == "低温库B"
    trail = queries.correction_trail(session, "C-1")
    assert len(trail) == 1
    assert trail[0]["original_payload"]["to_location"] == "错误库位X"
    assert trail[0]["corrected_payload"]["to_location"] == "低温库B"
    assert trail[0]["reason"]


def test_correct_received_quantity_rebalances_chain(session):
    _receive(session, qty=100)
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=10, purpose="检验", actor="alice")
    # the real received volume was 90 mL; fix the RECEIVE event
    ev1 = session.query(Event)\
        .filter_by(container_id="C-1", seq=1).one()
    true_payload = dict(ev1.payload)
    true_payload["quantity"] = 90.0
    services.correct_event(
        session, container_id="C-1", target_seq=1, actor="supervisor",
        reason="接收计量错误，实际为90mL",
        corrected_event_type="RECEIVE", corrected_payload=true_payload,
    )
    evolution = queries.quantity_evolution(session, "C-1")
    assert evolution["final_on_hand_qty"] == pytest.approx(80.0)
    assert evolution["balances_consistent"] is True


def test_correction_is_itself_a_chained_event(session):
    _receive(session)
    services.move(session, container_id="C-1", to_location="X", actor="alice")
    corr = services.correct_event(
        session, container_id="C-1", target_seq=2, actor="sup",
        reason="r", corrected_event_type="MOVE",
        corrected_payload={"from_location": "低温库A", "to_location": "Y",
                           "label_damaged": None},
    )
    assert corr.event_type is EventType.CORRECTION
    from app.ledger import verify_container_chain
    assert verify_container_chain(session, "C-1") == []


def test_cannot_correct_destroy_event(session):
    from datetime import datetime, timedelta, timezone
    _receive(session)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    ev1 = session.query(Event)\
        .filter_by(container_id="C-1", seq=1).one()
    payload = dict(ev1.payload)
    payload["retention_until"] = past.isoformat()
    services.correct_event(
        session, container_id="C-1", target_seq=1, actor="sup", reason="r",
        corrected_event_type="RECEIVE", corrected_payload=payload,
    )
    req = services.request_destruction(session, container_id="C-1",
                                       requested_by="alice")
    services.verify_destruction(
        session, request_id=req.request_id, verifier_a="a", verifier_b="b",
        scanned_code_a="C-1", scanned_code_b="C-1")
    services.execute_destruction(session, request_id=req.request_id, actor="a")

    destroy_seq = max(
        ev.seq for ev in session.query(Event)
        .filter_by(container_id="C-1").all()
    )
    with pytest.raises(LifecycleError, match="不能被更正"):
        services.correct_event(
            session, container_id="C-1", target_seq=destroy_seq,
            actor="sup", reason="x", corrected_payload={},
        )


def test_cannot_correct_a_correction_target_via_second_correction_is_allowed(session):
    # correcting the SAME underlying event twice keeps the latest truth
    _receive(session)
    services.move(session, container_id="C-1", to_location="X", actor="alice")
    for loc in ("Y", "Z"):
        services.correct_event(
            session, container_id="C-1", target_seq=2, actor="sup",
            reason=f"真实库位{loc}", corrected_event_type="MOVE",
            corrected_payload={"from_location": "低温库A", "to_location": loc,
                               "label_damaged": None},
        )
    assert services._proj(session, "C-1").location == "Z"
    assert len(queries.correction_trail(session, "C-1")) == 2


def test_rebuild_all_recovers_state_from_ledger_alone(session):
    _receive(session, cid="MOM", qty=100)
    services.aliquot(
        session, parent_container_id="MOM",
        children=[{"container_id": "K1", "quantity": 30},
                  {"container_id": "K2", "quantity": 20}],
        storage_location="架2", actor="alice",
    )
    services.move(session, container_id="K1", to_location="架3", actor="alice")

    from app.models import Container, ContainerProjection
    # wipe projections AND child container registry rows, corrupt chain head
    session.query(ContainerProjection).delete()
    session.query(Container).filter(Container.container_id.in_(["K1", "K2"])).delete()
    mom = session.get(Container, "MOM")
    mom.last_seq, mom.last_event_hash = 0, None
    session.flush()

    rebuild_all(session)

    # children re-registered from the parent ALIQUOT payload, states restored
    assert session.get(Container, "K1") is not None
    assert services._proj(session, "MOM").quantity == pytest.approx(50)
    assert services._proj(session, "K1").quantity == pytest.approx(30)
    assert services._proj(session, "K1").location == "架3"
    assert session.get(Container, "MOM").last_event_hash is not None
