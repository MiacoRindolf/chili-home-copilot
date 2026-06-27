"""ORDER-CHUNKING venue-adapter wrapper (item 2) — DEFAULT OFF.

Splits a parent ``place_limit_order_gtc`` into N equal child blocks for queue priority,
each submitted as a DISTINCT order (a fresh ``client_order_id`` so the venue accepts it as
its own order and the shared idempotency store never collides them). Every child's
``order_id`` (the broker's truth) is collected into the returned result's ``chunk_order_ids``
so the live-runner / reconciler can fold all legs back onto the SINGLE parent trade row —
preserving the existing dedupe-on-broker_order_id + orphan-by-symbol+side+qty safety net.

Hard safety posture (the agentic rail's duplicate-fill / stranded-naked-long history):
  * Transparent delegation: EVERY other VenueAdapter method passes straight through to the
    wrapped base adapter (``__getattr__``), so only the entry-limit path is touched.
  * Default OFF + blocks<=1 ⇒ the wrapper is NEVER inserted (``maybe_wrap_chunking`` returns
    the base factory) ⇒ byte-identical single order. With the wrapper inserted but blocks==1,
    it submits exactly ONE order with the caller's client_order_id ⇒ also byte-identical.
  * Fail-CLOSED-to-single: any error building the split, or a base_size that can't be
    cleanly chunked, falls back to a SINGLE base ``place_limit_order_gtc`` with the caller's
    original kwargs (never a duplicate, never a partial multi-submit on the error path).
  * Single-writer preserved: the live_runner is still the only caller of place_*; the wrapper
    adds no out-of-band order placement and no background threads.

NOT enabled for production until dedupe/reconcile safety is proven on the agentic rail
(operator gate). For a small cash account the benefit is marginal (N× spread/commission).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Module-level so callers can read the parent->children mapping from the result dict.
CHUNK_RESULT_KEY = "chunk_order_ids"


def _split_base_size(total: float, blocks: int, *, increment: float | None) -> list[float]:
    """Split ``total`` into ``blocks`` near-equal pieces, each a multiple of ``increment``
    (when given) and each strictly positive. Any rounding remainder is added to the LAST
    block so the children sum EXACTLY to ``total`` (no over/under-fill vs the parent qty).
    Returns ``[total]`` (a single piece) when a clean ``blocks``-way split is not possible.

    See tests/test_momentum_order_path_dedupe.py for exhaustive split-exactness,
    determinism, distinct-cid, and fail-closed-to-single proofs."""
    if blocks <= 1 or total <= 0:
        return [total]
    inc = float(increment) if increment and increment > 0 else None
    if inc:
        # Work in integer increment-units so the sum is exact.
        import math as _math

        units_total = int(round(total / inc))
        if units_total < blocks:
            return [total]  # can't give every block at least one increment ⇒ single order
        per = units_total // blocks
        if per <= 0:
            return [total]
        pieces_units = [per] * blocks
        pieces_units[-1] += units_total - per * blocks  # remainder onto the last block
        pieces = [round(u * inc, 12) for u in pieces_units]
        if any(p <= 0 for p in pieces):
            return [total]
        return pieces
    # No increment constraint: equal float split, remainder onto the last piece.
    per = total / blocks
    if per <= 0:
        return [total]
    pieces = [per] * (blocks - 1)
    pieces.append(total - per * (blocks - 1))
    if any(p <= 0 for p in pieces):
        return [total]
    return pieces


def _fmt_size(q: float) -> str:
    """Format a base size like the venue adapters expect (trim trailing zeros)."""
    s = f"{q:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


class ChunkingVenueAdapter:
    """Protocol-preserving wrapper that splits ``place_limit_order_gtc`` into N child blocks.

    Only ``place_limit_order_gtc`` is overridden; all other VenueAdapter methods delegate to
    ``base`` unchanged via ``__getattr__``.
    """

    def __init__(self, base: Any, *, blocks: int):
        self._base = base
        # Clamp defensively (config already clamps 1..10).
        self._blocks = max(1, min(10, int(blocks)))

    # --- transparent delegation for the entire rest of the protocol --------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def is_enabled(self) -> bool:  # explicit (preflight reads it before __getattr__ kicks in)
        return bool(self._base.is_enabled())

    def _base_increment(self, product_id: str) -> Optional[float]:
        try:
            prod, _ = self._base.get_product(product_id)
            inc = getattr(prod, "base_increment", None) if prod is not None else None
            return float(inc) if inc and float(inc) > 0 else None
        except Exception:
            return None

    def place_limit_order_gtc(
        self,
        *,
        product_id: str,
        side: str,
        base_size: str,
        limit_price: str,
        client_order_id: Optional[str] = None,
        extended_hours: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit the parent as ``blocks`` distinct child orders. Returns the FIRST child's
        result enriched with ``chunk_order_ids`` (all child broker order_ids) so the caller
        reconciles every leg onto the one parent trade row. Falls back to a SINGLE base order
        on blocks<=1 or any split/parse error (fail-closed-to-single: never a duplicate)."""
        # Pass-through kwargs that some adapters accept (post_only / time_in_force / overnight).
        base_kwargs = dict(kwargs)

        def _single() -> dict[str, Any]:
            return self._base.place_limit_order_gtc(
                product_id=product_id,
                side=side,
                base_size=base_size,
                limit_price=limit_price,
                client_order_id=client_order_id,
                extended_hours=extended_hours,
                **base_kwargs,
            )

        if self._blocks <= 1:
            return _single()
        try:
            total = float(base_size)
        except (TypeError, ValueError):
            return _single()
        if total <= 0:
            return _single()
        pieces = _split_base_size(
            total, self._blocks, increment=self._base_increment(product_id)
        )
        if len(pieces) <= 1:
            # Could not cleanly split ⇒ exactly one order with the caller's cid (byte-identical).
            return _single()

        results: list[dict[str, Any]] = []
        child_order_ids: list[str] = []
        first: dict[str, Any] | None = None
        for i, piece in enumerate(pieces):
            # FRESH client_order_id per child: a distinct order the venue + idempotency store
            # treat as its own (no collision with siblings or a retry of the parent). Derive
            # from the caller's cid so the lineage is auditable; append a uuid4 for uniqueness.
            base_cid = client_order_id or f"chunk_{uuid.uuid4().hex[:12]}"
            child_cid = f"{base_cid}_c{i}_{uuid.uuid4().hex[:6]}"[:120]
            try:
                res = self._base.place_limit_order_gtc(
                    product_id=product_id,
                    side=side,
                    base_size=_fmt_size(piece),
                    limit_price=limit_price,
                    client_order_id=child_cid,
                    extended_hours=extended_hours,
                    **base_kwargs,
                ) or {}
            except Exception as exc:  # noqa: BLE001 — surface as a failed child, never crash the tick
                logger.warning(
                    "[chunking_adapter] child %d/%d submit raised for %s: %s",
                    i + 1, len(pieces), product_id, exc,
                )
                res = {"ok": False, "error": f"chunk_child_exception:{exc}"}
            results.append(res)
            oid = res.get("order_id")
            if oid:
                child_order_ids.append(str(oid))
            if first is None:
                first = res
        all_ok = bool(results) and all(bool(r.get("ok")) for r in results)
        # FAIL-CLOSED-TO-SINGLE on a partial submit: if ANY child failed, the children
        # that DID ack are a half-placed multi-leg resting at the broker. Best-effort
        # CANCEL each placed child so the broker is left with no stranded resting leg
        # (the agentic rail's stranded-naked-long history). This is belt-and-suspenders:
        # the placed oids stay in chunk_order_ids so the live-runner's late-fill sweep
        # ALSO tracks each to a terminal resolution even if a cancel loses a race.
        cancelled_child_ids: list[str] = []
        if not all_ok and child_order_ids:
            for oid in child_order_ids:
                try:
                    self._base.cancel_order(str(oid))
                    cancelled_child_ids.append(str(oid))
                except Exception as exc:  # noqa: BLE001 — never crash the tick on cleanup
                    logger.warning(
                        "[chunking_adapter] partial-submit cleanup cancel failed for %s: %s",
                        oid, exc,
                    )
        # The primary result mirrors the FIRST child (so the caller's existing
        # res.get("order_id") path stays valid) but carries the full child lineage for
        # reconciliation. ok = True only if EVERY child acked (so a partial multi-submit
        # is surfaced as an error → the existing ack-timeout/reconcile repairs the legs).
        primary = dict(first or {"ok": False, "error": "chunk_no_children"})
        primary[CHUNK_RESULT_KEY] = child_order_ids
        primary["chunk_results"] = results
        primary["chunk_blocks"] = len(pieces)
        primary["ok"] = all_ok
        if not primary["ok"]:
            primary["chunk_cancelled_order_ids"] = cancelled_child_ids
            if "error" not in primary:
                primary["error"] = "chunk_partial_submit"
        return primary


def maybe_wrap_chunking(factory: Callable[[], Any]) -> Callable[[], Any]:
    """Return a factory that wraps the base adapter in :class:`ChunkingVenueAdapter` IFF the
    chunking flag is ON and blocks>1; otherwise return the base ``factory`` UNCHANGED.

    DEFAULT OFF ⇒ byte-identical: with the flag off (or blocks<=1) the original factory is
    returned verbatim, so the live-runner gets the exact same adapter object it always did.
    """
    try:
        from ...config import settings  # local import: keep module IO-free at import

        if not bool(getattr(settings, "chili_momentum_order_chunking_enabled", False)):
            return factory
        blocks = int(getattr(settings, "chili_momentum_order_chunking_blocks", 1) or 1)
        if blocks <= 1:
            return factory
    except Exception:
        return factory

    def _wrapped() -> Any:
        return ChunkingVenueAdapter(factory(), blocks=blocks)

    return _wrapped


__all__ = ["ChunkingVenueAdapter", "maybe_wrap_chunking", "CHUNK_RESULT_KEY"]
