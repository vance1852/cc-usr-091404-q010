"""Automatic usage restriction after freeze-thaw / exposure limits exceeded."""
from __future__ import annotations

import pytest

from app import queries, services
from app.errors import LifecycleError


def _receive(session, cid="C-1", **kw):
    params = dict(
        container_id=cid, batch_id="B1", material="原液", quantity=100.0,
        unit="mL", location="低温库A", actor="alice",
        max_freeze_thaw=2, exposure_limit_min=60.0,
    )
    params.update(kw)
    return services.receive_sample(session, **params)


def test_freeze_thaw_limit_blocks_normal_checkout(session):
    _receive(session)
    # two checkouts+returns to frozen storage => 2 freeze-thaw cycles (at limit, ok)
    for _ in range(2):
        services.checkout(session, container_id="C-1", holder="bob",
                          quantity=5, purpose="检验", actor="alice")
        services.return_sample(session, container_id="C-1", returned_qty=5,
                               consumed_qty=0, to_location="低温库A", actor="bob")
    proj = services._proj(session, "C-1")
    assert proj.freeze_thaw_count == 2
    assert proj.usage_restricted is None

    # third cycle crosses the limit
    services.checkout(session, container_id="C-1", holder="bob",
                      quantity=5, purpose="检验", actor="alice")
    services.return_sample(session, container_id="C-1", returned_qty=5,
                           consumed_qty=0, to_location="低温库A", actor="bob")
    proj = services._proj(session, "C-1")
    assert proj.freeze_thaw_count == 3
    assert "冻融次数" in proj.usage_restricted

    with pytest.raises(LifecycleError, match="用途已被自动限制"):
        services.checkout(session, container_id="C-1", holder="bob",
                          quantity=1, purpose="检验", actor="alice")


def test_restricted_sample_still_usable_for_investigation(session):
    _receive(session)
    services.record_temperature_exposure(
        session, container_id="C-1", duration_min=61, actor="alice"
    )
    proj = services._proj(session, "C-1")
    assert "温度暴露" in proj.usage_restricted
    # investigation use remains permitted
    ev = services.checkout(session, container_id="C-1", holder="qa",
                           quantity=2, purpose="INVESTIGATION", actor="alice")
    assert ev.payload["purpose"] == "INVESTIGATION"


def test_exposure_accumulates_across_events(session):
    _receive(session)
    services.record_temperature_exposure(
        session, container_id="C-1", duration_min=30, actor="alice")
    services.record_temperature_exposure(
        session, container_id="C-1", duration_min=30, actor="alice")
    assert services._proj(session, "C-1").usage_restricted is None
    services.record_temperature_exposure(
        session, container_id="C-1", duration_min=1,
        crossed_freeze_thaw=True, actor="alice")
    proj = services._proj(session, "C-1")
    assert proj.cumulative_exposure_min == pytest.approx(61)
    assert proj.freeze_thaw_count == 1
    assert "温度暴露" in proj.usage_restricted


def test_status_surfaces_restriction(session):
    _receive(session)
    services.record_temperature_exposure(
        session, container_id="C-1", duration_min=90, actor="alice")
    status = queries.container_status(session, "C-1")
    assert status["usage_restricted"]
    assert status["cumulative_exposure_min"] == 90
