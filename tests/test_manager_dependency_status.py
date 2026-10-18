from dataclasses import replace

import pytest

from core.manager_dependency_status import (DependencyObservation, DependencyState,
    DependencyRoute, DependencyStatusSnapshot, project_dependency)


def observation(**changes):
    return replace(DependencyObservation('runner', 'Local runner'), **changes)


@pytest.mark.parametrize('state', list(DependencyState))
@pytest.mark.parametrize('available', [False, True])
def test_only_supplied_idle_routes_are_actionable(state, available):
    row = observation(state=state, blocking_reason='Needs existing recovery review.',
        route=DependencyRoute.OWNED_RUNTIME, route_available=available)
    view = project_dependency(row)
    assert view.action_enabled is (available and state is not DependencyState.BUSY)
    assert bool(view.disabled_reason) is not view.action_enabled
    assert 'Needs existing recovery review.' in view.detail


def test_unknown_and_reported_ready_do_not_infer_setup_authority():
    assert not project_dependency(observation()).action_enabled
    assert not project_dependency(observation(state=DependencyState.READY)).action_enabled
    assert 'No dated observation' in project_dependency(observation()).detail


@pytest.mark.parametrize('changes', [dict(key=''), dict(component=''), dict(state='ready'),
    dict(route='repair-everything'), dict(route_available=1), dict(route_available=True),
    dict(detail='x' * 2049), dict(state=DependencyState.BLOCKED), dict(state=DependencyState.RESTART)])
def test_invalid_or_unbounded_observations_refuse(changes):
    with pytest.raises(ValueError):
        observation(**changes)


def test_snapshot_is_bounded_and_unique():
    with pytest.raises(ValueError):
        DependencyStatusSnapshot('same', (observation(), observation()))
    with pytest.raises(ValueError):
        DependencyStatusSnapshot('too-many', tuple(observation(key=str(n)) for n in range(33)))
    with pytest.raises(ValueError):
        DependencyStatusSnapshot('mutable', [])


@pytest.mark.parametrize('route', list(DependencyRoute))
def test_supported_workspace_routes_remain_reviews(route):
    view = project_dependency(observation(route=route, route_available=True))
    assert view.action_enabled and view.action_text.startswith('Review ')
