"""Differences between Graphiti versions the gate has to bridge, so it can run on older commits."""

from __future__ import annotations

from graphiti_core.graphiti_types import GraphitiClients


def make_clients(**kwargs) -> GraphitiClients:
    """GraphitiClients with a no-op tracer where the version has one (added after 0.2x)."""
    if 'tracer' in GraphitiClients.model_fields:
        from graphiti_core.tracer import NoOpTracer

        kwargs.setdefault('tracer', NoOpTracer())
    return GraphitiClients(**kwargs)


async def build_indices(driver) -> None:
    """Index setup moved from a module function onto the driver in later versions."""
    if hasattr(driver, 'build_indices_and_constraints'):
        await driver.build_indices_and_constraints()
    else:
        from graphiti_core.utils.maintenance.graph_data_operations import (
            build_indices_and_constraints,
        )

        await build_indices_and_constraints(driver)
