"""Shared fail-closed errors for historical replay input contracts."""

from __future__ import annotations


class ReplayInputContractError(RuntimeError):
    """A replay attempted to consume an input that was not causally sealed."""


class ReplayPipelineInputUnavailableError(ReplayInputContractError):
    """The momentum selection pipeline lacks a complete recorded input bundle."""


class ReplayScannerSnapshotUnavailableError(ReplayInputContractError):
    """A replay requested a scanner snapshot that was not captured and bound."""


class ReplayOhlcvInputUnavailableError(ReplayInputContractError):
    """A replay/captured decision requested OHLCV without exact durable evidence."""


class ReplayMicrostructureInputUnavailableError(ReplayInputContractError):
    """A replay/captured decision requested an unsealed microstructure window."""


class ReplayDecisionLocalMicrostructureCoverageUnavailableError(
    ReplayMicrostructureInputUnavailableError
):
    """One optional L2 read lacks proof, without corrupting the whole decision.

    The capture producer must have already persisted a coverage-gap artifact.
    Pipeline readers translate this error to their existing type-safe missing
    value and never fall back to a live provider or database.  Identity, clock,
    receipt, and exact-print failures continue to use the parent error and
    therefore reject the complete captured decision scope.
    """
