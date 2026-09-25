"""Runtime model profiles change future sampling without rewriting Work state."""

import pytest
from pydantic import ValidationError

from yuki_participation.autonomy_parameters import AutonomyParameters
from yuki_participation.controller import Controller
from yuki_participation.models import Scope


def test_live_profile_changes_intrinsic_rate_without_mutating_snapshot() -> None:
    controller = Controller(Scope(conversation_id="group", generation=1), 100)
    controller.initialize_human_activity(100, ((90.0, 1),), 90.0)
    before_state = controller.state.model_dump_json()
    before_rate = controller.intrinsic_opportunity(100)

    controller.set_parameters(AutonomyParameters(intrinsic_interval_seconds=7200))

    assert controller.intrinsic_opportunity(100) == pytest.approx(before_rate * 2)
    assert controller.state.model_dump_json() == before_state


@pytest.mark.parametrize(
    "payload",
    [
        {"intrinsic_interval_seconds": 0},
        {"quiet_group_floor": 0.03},
        {"human_activity_half_saturation": 0},
        {"source_opening_seconds": -1},
        {"unknown_parameter": 1},
    ],
)
def test_invalid_profile_is_rejected(payload: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        AutonomyParameters.model_validate(payload)
