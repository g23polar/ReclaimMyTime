"""T3.1: per-turn label resolution, local→absolute conversion, tag vocabulary, discard."""

import itertools
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.agent.staging import ToolError, TurnInputs, TurnStaging, normalize_tag
from app.db.models.staged import (
    ExceptionAdded,
    PinAdded,
    PreferencesUpdated,
    ProjectCreated,
    ProjectUpdated,
    parse_staged,
)
from app.db.solver_io import (
    ConstraintState,
    ExceptionState,
    PinState,
    PreferencesState,
    ProjectState,
)
from app.scheduler.models import Interval

TZ = "America/Los_Angeles"
THESIS = "11111111-1111-1111-1111-111111111111"
GYM = "22222222-2222-2222-2222-222222222222"
EXC = "33333333-3333-3333-3333-333333333333"
PIN = "44444444-4444-4444-4444-444444444444"
ALL_DAYS = {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
WEEKDAYS = {**ALL_DAYS, **{d: [{"start": "09:00", "end": "17:00"}] for d in ("mon", "tue")}}


_COUNTER = itertools.count(1)  # shared: ids stay unique across turns within a test


def _ids():
    return lambda: uuid.UUID(int=next(_COUNTER))


def _prefs() -> PreferencesState:
    return PreferencesState(
        timezone=TZ,
        schedulable_windows={"mon": [["08:00", "22:00"]]},
        day_diversity="mixed",
        buffer_minutes=15,
        week_start=1,
    )


def _state(prefs: PreferencesState | None = None) -> ConstraintState:
    span = Interval(
        start=datetime(2026, 11, 20, 17, tzinfo=UTC), end=datetime(2026, 11, 20, 18, tzinfo=UTC)
    )
    return ConstraintState(
        preferences=prefs,
        projects={
            THESIS: ProjectState(
                id=THESIS,
                kind="deadline",
                status="active",
                max_block_minutes=90,
                min_block_minutes=25,
                target_date=date(2026, 12, 15),
                estimated_total_hours=Decimal("40"),
                requirements=("deep-work",),
            ),
            GYM: ProjectState(
                id=GYM,
                kind="perennial",
                status="active",
                max_block_minutes=60,
                min_block_minutes=25,
                weekly_target_hours=Decimal("3"),
            ),
        },
        exceptions={EXC: ExceptionState(id=EXC, span=span, blocks_all=True, tags=())},
        pins={
            PIN: PinState(
                id=PIN, kind="forbid", span=span, project_id=None, created_from_block_id=None
            )
        },
    )


def _turn(*, prefs=True, pending=(), vocabulary=("deep-work", "outdoors")) -> TurnStaging:
    return TurnStaging(
        TurnInputs(
            committed=_state(_prefs() if prefs else None),
            project_ids=[THESIS, GYM],
            exception_ids=[EXC],
            pin_ids=[PIN],
            vocabulary=list(vocabulary),
            pending=list(pending),
            timezone=TZ,
        ),
        new_id=_ids(),
    )


def _create(**over):
    base = {
        "name": "Novel",
        "kind": "perennial",
        "target_date": None,
        "estimated_total_hours": None,
        "weekly_target_hours": 5,
        "max_block_minutes": 60,
        "min_block_minutes": None,
        "preferred_windows": None,
        "preferred_strict": False,
        "requirements": [],
        "new_tags": [],
        "interpretation": "Write the novel five hours a week.",
    }
    out = base | over
    if out["preferred_windows"] is not None:
        out["preferred_windows_set"] = True  # the wire flag; without it windows are dropped
    return out


def _update(**over):
    base = {
        "project": "P1",
        "name": None,
        "target_date": None,
        "estimated_total_hours": None,
        "weekly_target_hours": None,
        "max_block_minutes": None,
        "min_block_minutes": None,
        "preferred_windows": None,
        "clear_preferred_windows": False,
        "preferred_strict": None,
        "requirements": None,
        "new_tags": [],
        "status": None,
        "interpretation": "x",
    }
    return base | over


def _at(day, time):
    return {"day": day, "time": time}


# ---------------------------------------------------------------- labels


def test_labels_follow_input_order():
    t = _turn()
    assert t.projects == {"P1": THESIS, "P2": GYM}
    assert t.exceptions == {"E1": EXC}
    assert t.pins == {"N1": PIN}
    assert t.staged == [] and not t.changed


def test_new_project_gets_next_label_and_is_usable_in_the_same_turn():
    t = _turn()
    out = t.call("stage_project_create", _create())
    assert out == "Staged S1: new project P3 'Novel'."
    pid = t.projects["P3"]
    t.call(
        "stage_pin_add",
        {
            "kind": "pin",
            "project": "P3",
            "start": _at("2026-11-18", "07:00"),
            "end": _at("2026-11-18", "08:00"),
            "interpretation": "Novel on Wednesday at 7.",
        },
    )
    pin = t.mutations[1]
    assert isinstance(pin, PinAdded)
    assert str(pin.payload.project_id) == pid
    assert t.changed


def test_unknown_label_is_a_tool_error_listing_known_labels():
    with pytest.raises(ToolError, match=r"unknown project label 'P9'; known: \['P1', 'P2'\]"):
        _turn().call("stage_project_archive", {"project": "P9", "interpretation": "x"})


def test_pending_list_is_carried_with_labels():
    first = _turn()
    first.call("stage_project_create", _create())
    t = _turn(pending=first.mutations)
    assert [label for label, _ in t.staged] == ["S1"]
    assert "P3" in t.projects
    assert not t.changed  # nothing new this turn: no re-propose
    t.call("stage_project_create", _create(name="Piano"))
    assert [label for label, _ in t.staged] == ["S1", "S2"]
    assert "P4" in t.projects


# ---------------------------------------------------------------- local → absolute


def test_pin_span_is_converted_in_the_users_timezone():
    t = _turn()
    t.call(
        "stage_pin_add",
        {
            "kind": "forbid",
            "project": None,
            "start": _at("2026-11-18", "12:00"),
            "end": _at("2026-11-18", "24:00"),
            "interpretation": "Keep Wednesday afternoon free.",
        },
    )
    span = t.mutations[0].payload.span
    assert span.start == datetime(2026, 11, 18, 20, tzinfo=UTC)  # PST = UTC-8
    assert span.end == datetime(2026, 11, 19, 8, tzinfo=UTC)


def test_exception_across_dst_uses_each_endpoints_offset():
    t = _turn()
    t.call(
        "stage_exception_add",
        {
            "start": _at("2026-10-31", "00:00"),
            "end": _at("2026-11-02", "00:00"),
            "blocks_all": True,
            "blocks_tags": ["deep-work"],
            "reason": "Trip",
            "interpretation": "Away for the weekend.",
        },
    )
    m = t.mutations[0]
    assert isinstance(m, ExceptionAdded)
    assert m.payload.span.start == datetime(2026, 10, 31, 7, tzinfo=UTC)  # PDT
    assert m.payload.span.end == datetime(2026, 11, 2, 8, tzinfo=UTC)  # PST
    assert m.payload.blocks_tags == []  # blocks_all wins


def test_reversed_span_is_rejected():
    with pytest.raises(ToolError, match="start < end"):
        _turn().call(
            "stage_pin_add",
            {
                "kind": "forbid",
                "project": None,
                "start": _at("2026-11-18", "12:00"),
                "end": _at("2026-11-18", "11:00"),
                "interpretation": "x",
            },
        )


def test_onboarding_uses_request_timezone():
    t = _turn(prefs=False)
    t.call(
        "stage_pin_add",
        {
            "kind": "forbid",
            "project": None,
            "start": _at("2026-11-18", "00:00"),
            "end": _at("2026-11-18", "01:00"),
            "interpretation": "x",
        },
    )
    assert t.mutations[0].payload.span.start == datetime(2026, 11, 18, 8, tzinfo=UTC)


# ---------------------------------------------------------------- projects


def test_deadline_project_requires_its_fields():
    with pytest.raises(ToolError, match="target_date, estimated_total_hours required"):
        _turn().call("stage_project_create", _create(kind="deadline", weekly_target_hours=None))
    with pytest.raises(ToolError, match="weekly_target_hours must be null"):
        _turn().call(
            "stage_project_create",
            _create(kind="deadline", target_date="2026-12-01", estimated_total_hours=10),
        )


def test_deadline_project_and_windows_round_trip():
    t = _turn()
    t.call(
        "stage_project_create",
        _create(
            kind="deadline",
            target_date="2026-12-01",
            estimated_total_hours=10.04,
            weekly_target_hours=None,
            preferred_windows=WEEKDAYS,
        ),
    )
    m = t.mutations[0]
    assert isinstance(m, ProjectCreated)
    assert m.payload.project.estimated_total_hours == 10.0  # D19 rounding
    assert m.payload.project.preferred_windows["mon"] == [["09:00", "17:00"]]
    assert m.payload.project.preferred_windows["sun"] == []


def test_overlapping_windows_rejected():
    windows = {
        **ALL_DAYS,
        "mon": [{"start": "09:00", "end": "12:00"}, {"start": "11:00", "end": "13:00"}],
    }
    with pytest.raises(ToolError, match="non-overlapping"):
        _turn().call("stage_project_create", _create(preferred_windows=windows))


def test_update_only_sends_changed_fields():
    t = _turn()
    t.call("stage_project_update", _update(status="paused", max_block_minutes=45))
    m = t.mutations[0]
    assert isinstance(m, ProjectUpdated)
    assert m.payload.changes.model_fields_set == {"status", "max_block_minutes"}


def test_update_clear_windows_and_kind_mismatch():
    t = _turn()
    t.call("stage_project_update", _update(clear_preferred_windows=True))
    assert t.mutations[0].payload.changes.model_fields_set == {"preferred_windows"}
    with pytest.raises(ToolError, match="not valid for a deadline project"):
        t.call("stage_project_update", _update(weekly_target_hours=4))
    with pytest.raises(ToolError, match="nothing to change"):
        t.call("stage_project_update", _update())


def test_update_after_archive_in_same_turn_is_refused():
    t = _turn()
    t.call("stage_project_archive", {"project": "P2", "interpretation": "Drop gym."})
    with pytest.raises(ToolError, match="P2 is already removed by S1"):
        t.call("stage_project_update", _update(project="P2", max_block_minutes=30))


def test_schema_violation_is_a_tool_error_not_an_exception():
    with pytest.raises(ToolError, match="invalid input: max_block_minutes"):
        _turn().call("stage_project_create", _create(max_block_minutes=0))
    with pytest.raises(ToolError, match="unknown staging tool 'propose_schedule'"):
        _turn().call("propose_schedule", {})


def test_failed_call_records_nothing():
    t = _turn()
    with pytest.raises(ToolError):
        t.call("stage_project_create", _create(requirements=["nope"]))
    assert t.staged == [] and not t.changed and "P3" not in t.projects


# ---------------------------------------------------------------- tags


def test_normalize_tag():
    assert normalize_tag(" Deep_Work ") == normalize_tag("deep-work") == "deep-work"
    assert normalize_tag("deep  work") == "deep-work"


def test_requirement_outside_vocabulary_points_to_new_tags():
    with pytest.raises(ToolError, match="A new tag goes in new_tags"):
        _turn().call("stage_project_create", _create(requirements=["music"]))


def test_new_tag_is_accepted_and_named_in_interpretation():
    t = _turn()
    t.call("stage_project_create", _create(requirements=["outdoors"], new_tags=["music"]))
    m = t.mutations[0]
    assert m.payload.project.requirements == ["outdoors", "music"]
    assert m.interpretation.endswith("(new tags: music)")
    assert "music" in t.vocabulary()  # usable by later calls in this turn


def test_near_duplicate_new_tag_rejected():
    with pytest.raises(ToolError, match="duplicates tag 'deep-work'"):
        _turn().call("stage_project_create", _create(new_tags=["Deep Work"]))


def test_update_new_tags_extend_current_requirements():
    t = _turn()
    t.call("stage_project_update", _update(new_tags=["writing"]))
    assert t.mutations[0].payload.changes.requirements == ["deep-work", "writing"]


def test_exception_tags_must_exist():
    with pytest.raises(ToolError, match="unknown tags"):
        _turn().call(
            "stage_exception_add",
            {
                "start": _at("2026-11-18", "09:00"),
                "end": _at("2026-11-18", "10:00"),
                "blocks_all": False,
                "blocks_tags": ["music"],
                "reason": None,
                "interpretation": "x",
            },
        )


# ---------------------------------------------------------------- preferences


def test_first_preferences_need_every_field():
    with pytest.raises(ToolError, match="schedulable_windows is required"):
        _turn(prefs=False).call(
            "stage_preferences",
            {
                "schedulable_windows": None,
                "day_diversity": "mixed",
                "buffer_minutes": 10,
                "week_start": 1,
                "interpretation": "x",
            },
        )


def test_partial_preferences_keep_current_values_and_timezone():
    t = _turn()
    t.call(
        "stage_preferences",
        {
            "schedulable_windows": None,
            "day_diversity": None,
            "buffer_minutes": 5,
            "week_start": None,
            "interpretation": "Shorter gaps.",
        },
    )
    m = t.mutations[0]
    assert isinstance(m, PreferencesUpdated)
    p = m.payload.preferences
    assert (p.timezone, p.buffer_minutes, p.day_diversity, p.week_start) == (TZ, 5, "mixed", 1)
    assert p.schedulable_windows == {"mon": [["08:00", "22:00"]]}


# ---------------------------------------------------------------- removals + discard


def test_remove_existing_exception_and_pin():
    t = _turn()
    t.call("stage_exception_remove", {"exception": "E1", "interpretation": "x"})
    t.call("stage_pin_remove", {"pin": "N1", "interpretation": "x"})
    assert [m.kind for m in t.mutations] == ["exception_removed", "pin_removed"]
    assert str(t.mutations[0].payload.exception_id) == EXC


def test_discard_staged_project_cascades_to_dependents():
    t = _turn()
    t.call("stage_project_create", _create())  # S1, P3
    t.call("stage_project_update", _update(project="P3", max_block_minutes=30))  # S2
    t.call(
        "stage_pin_add",
        {
            "kind": "pin",
            "project": "P3",
            "start": _at("2026-11-18", "07:00"),
            "end": _at("2026-11-18", "08:00"),
            "interpretation": "x",
        },
    )  # S3
    t.call("stage_project_archive", {"project": "P2", "interpretation": "x"})  # S4
    assert t.call("discard_staged", {"staged": "S1"}) == "Discarded S1, S2, S3."
    assert [label for label, _ in t.staged] == ["S4"]
    assert "P3" not in t.projects
    # Labels are not reused after a discard.
    assert t.call("stage_project_create", _create(name="Piano")).startswith(
        "Staged S5: new project P4"
    )


def test_discard_unknown_label():
    with pytest.raises(ToolError, match=r"unknown staged label 'S1'; staged: \[\]"):
        _turn().call("discard_staged", {"staged": "S1"})


def test_discard_pending_item_marks_turn_changed():
    first = _turn()
    first.call("stage_project_create", _create())
    t = _turn(pending=first.mutations)
    t.call("discard_staged", {"staged": "S1"})
    assert t.staged == [] and t.changed


def test_pending_list_that_no_longer_applies_raises():
    # A pending update to a project that is gone from committed state: the handler must
    # not build a turn on it (T3.2 treats this as an expired pending changeset).
    stale = parse_staged(
        [
            {
                "kind": "project_updated",
                "source": "agent",
                "interpretation": None,
                "payload": {"project_id": str(uuid.uuid4()), "changes": {"max_block_minutes": 30}},
            }
        ]
    )
    with pytest.raises(Exception, match="unknown or archived"):
        _turn(pending=stale)


def test_repeated_removals_are_refused():
    # Review crf-0e2b: overlay ignores missing removal targets, but confirm applies each
    # mutation in order and would reject the second removal — making the changeset dead.
    t = _turn()
    t.call("stage_exception_remove", {"exception": "E1", "interpretation": "x"})
    t.call("stage_pin_remove", {"pin": "N1", "interpretation": "x"})
    t.call("stage_project_archive", {"project": "P2", "interpretation": "x"})
    with pytest.raises(ToolError, match="E1 is already removed by S1"):
        t.call("stage_exception_remove", {"exception": "E1", "interpretation": "x"})
    with pytest.raises(ToolError, match="N1 is already removed by S2"):
        t.call("stage_pin_remove", {"pin": "N1", "interpretation": "x"})
    with pytest.raises(ToolError, match="P2 is already removed by S3"):
        t.call("stage_project_archive", {"project": "P2", "interpretation": "x"})
    assert len(t.staged) == 3


def test_discarding_a_removal_makes_the_label_usable_again():
    t = _turn()
    t.call("stage_exception_remove", {"exception": "E1", "interpretation": "x"})
    t.call("discard_staged", {"staged": "S1"})
    t.call("stage_exception_remove", {"exception": "E1", "interpretation": "x"})
    assert [label for label, _ in t.staged] == ["S2"]


def test_removal_carried_in_pending_list_retires_the_label():
    first = _turn()
    first.call("stage_pin_remove", {"pin": "N1", "interpretation": "x"})
    with pytest.raises(ToolError, match="N1 is already removed by S1"):
        _turn(pending=first.mutations).call(
            "stage_pin_remove", {"pin": "N1", "interpretation": "x"}
        )


def test_near_duplicate_new_tags_in_one_call_rejected():
    # Review crf-2ae5: neither spelling exists yet, so only same-call additions can catch it.
    t = _turn()
    with pytest.raises(ToolError, match="tag 'Deep Writing' duplicates tag 'deep-writing'"):
        t.call("stage_project_create", _create(new_tags=["deep-writing", "Deep Writing"]))
    assert t.staged == []
