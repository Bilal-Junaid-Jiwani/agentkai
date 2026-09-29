"""Tests for goals + tracked items + the briefing prompt builder."""
from datetime import datetime

import pytest

from agentkai.goals import (GoalError, GoalStore, build_goal_briefing_prompt,
                            goal_tools)


@pytest.fixture()
def store(tmp_path):
    return GoalStore(db_path=tmp_path / "goals.db")


def test_goal_crud(store):
    g = store.create_goal("Ship v1", "launch the thing")
    assert g.id == 1 and g.status == "active"
    assert store.get_goal(1).title == "Ship v1"
    updated = store.update_goal(1, status="paused")
    assert updated.status == "paused"
    with pytest.raises(GoalError):
        store.update_goal(1, status="nope")
    with pytest.raises(GoalError):
        store.create_goal("   ")


def test_subgoals(store):
    parent = store.create_goal("Learn Urdu")
    child = store.create_goal("Alphabet", parent_id=parent.id)
    fetched = store.get_goal(parent.id)
    assert len(fetched.subgoals) == 1
    assert fetched.subgoals[0].id == child.id
    top = store.list_goals()
    assert [g.id for g in top] == [parent.id]  # top-level only
    with pytest.raises(GoalError):
        store.create_goal("Orphan", parent_id=999)


def test_activity_and_close(store):
    g = store.create_goal("Run daily")
    a = store.log_activity(g.id, "progress", "ran 5k")
    assert a.kind == "progress"
    assert len(store.recent_activity(g.id)) == 1
    with pytest.raises(GoalError):
        store.log_activity(g.id, "vibes", "x")
    closed = store.close_goal(g.id, "completed", "done!")
    assert closed.status == "completed"
    assert len(store.recent_activity(g.id)) == 2  # close note logged
    with pytest.raises(GoalError):
        store.close_goal(g.id, "maybe")


def test_breaks(store):
    g = store.create_goal("Side project")
    store.start_break(g.id, "busy week")
    assert store.get_goal(g.id).status == "paused"
    with pytest.raises(GoalError):
        store.start_break(g.id)  # already on break
    store.end_break(g.id)
    assert store.get_goal(g.id).status == "active"
    with pytest.raises(GoalError):
        store.end_break(g.id)  # no open break


def test_tracked_items(store):
    t = store.track_open("Flight PK-304", "reservation")
    assert t.status == "open" and t.kind == "reservation"
    assert len(store.list_tracked("open")) == 1
    closed = store.track_close(t.id, "landed on time")
    assert closed.status == "closed" and closed.evidence == "landed on time"
    assert store.list_tracked("open") == []
    with pytest.raises(GoalError):
        store.track_open("x", "teleport")
    with pytest.raises(GoalError):
        store.track_close(999)


def test_briefing_prompt_builder(store):
    g = store.create_goal("Write book")
    store.log_activity(g.id, "progress", "chapter 1 draft done")
    store.track_open("Parcel from DHL", "delivery")
    prompt = build_goal_briefing_prompt(store, now=datetime(2026, 9, 29, 9, 0))
    assert "[goal-briefing · 2026-09-29 09:00]" in prompt
    assert "Write book" in prompt
    assert "chapter 1 draft done" in prompt
    assert "Parcel from DHL" in prompt
    assert "goals_log_activity" in prompt
    # empty store still produces a sane prompt
    empty = GoalStore(db_path=store.db_path.parent / "empty.db")
    p2 = build_goal_briefing_prompt(empty)
    assert "0 active goal(s)" in p2


def test_goal_tools_risk_and_errors(store):
    tools = {t.name: t for t in goal_tools(store)}
    assert tools["goals_create"].risk == "medium"
    assert tools["goals_list"].risk == "low"
    assert tools["track_list"].risk == "low"
    out = tools["goals_create"].run(title="Learn piano")
    assert out["ok"] is True
    out = tools["goals_log_activity"].run(goal_id=out["id"], kind="nope",
                                          text="x")
    assert out.startswith("ERROR:")
    out = tools["track_open"].run(title="Dentist", kind="reminder")
    assert out["kind"] == "reminder"
    out = tools["track_close"].run(item_id=out["id"],
                                   evidence="done at 10am")
    assert out["status"] == "closed"
    listed = tools["goals_list"].run(status="active")
    assert any(g["title"] == "Learn piano" for g in listed["goals"])
