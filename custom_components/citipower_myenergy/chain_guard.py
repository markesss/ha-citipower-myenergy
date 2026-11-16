"""One writer at a time for an NMI's statistics, a trail of recent writes, and the repair notice.

The cost and per-period statistics are rewritten from stored readings by two callers that can
run together: the poll (the coordinator) and the options-flow recalculation. Each takes its
starting running total from the stored row before the rows it writes. If the other has queued
rows that are not yet stored, that total is stale and the rewritten rows no longer join the
ones before them (seen live: a period's cost total dropped by about $266 at the start of the
poll's window). So every public write in ``statistics.py`` takes this guard's lock for its whole
run and waits for the recorder before and after writing.

The guard also keeps the last few writes (for the diagnostics download) and the statistics that
failed a chain check, and raises one Repairs notice per entry while any of them is still broken.
Everything stays inside Home Assistant: nothing here is sent anywhere, and nothing here names
the NMI (a statistic is shown by the part of its id after the NMI, e.g. ``e1_off_peak_cost``).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

TRAIL_LENGTH = 20
ISSUE_PREFIX = "chain_break"
_KEY = f"{DOMAIN}_write_guards"


@dataclass(slots=True)
class WriteRecord:
    """One statistic's write: what was done, and from which running total."""

    when: str
    who: str  # "poll" or "recalculate"
    statistic: str  # the id without the domain and NMI, e.g. "e1_off_peak_cost"
    repair_from: str | None  # the local day a rewrite started from, if it was a rewrite
    supply_mode: str | None
    start_total: float | None  # the running total the first written row was chained from
    rows: int
    join_repaired: bool  # a stored row that did not join the one before it was found first
    verified: bool | None  # the chain check after the write; None if nothing was written


class ChainGuard:
    """The lock, write trail and broken-chain notice for one NMI."""

    def __init__(self, hass: HomeAssistant, nmi: str) -> None:
        """Create the guard; ``entry_id`` is filled in by the coordinator."""
        self._hass = hass
        self._prefix = f"{DOMAIN}:{nmi.lower()}_"
        self.lock = asyncio.Lock()
        self.entry_id: str | None = None
        self.trail: deque[WriteRecord] = deque(maxlen=TRAIL_LENGTH)
        self.broken: dict[str, str] = {}  # statistic label -> local hour that does not join

    # --- Naming -------------------------------------------------------------------

    def label(self, statistic_id: str) -> str:
        """Return a statistic's id without the domain and NMI."""
        if statistic_id.startswith(self._prefix):
            return statistic_id[len(self._prefix) :]
        return statistic_id.split(":", 1)[-1]

    @property
    def issue_id(self) -> str:
        """Return the Repairs issue id (one per entry; never contains the NMI)."""
        return f"{ISSUE_PREFIX}_{self.entry_id or 'unassigned'}"

    # --- Trail --------------------------------------------------------------------

    def record(
        self,
        *,
        who: str,
        statistic_id: str,
        repair_from: str | None,
        supply_mode: str | None,
        start_total: float | None,
        rows: int,
        join_repaired: bool,
        verified: bool | None,
    ) -> None:
        """Add a write to the trail (the oldest falls off after TRAIL_LENGTH)."""
        self.trail.append(
            WriteRecord(
                when=dt_util.utcnow().isoformat(timespec="seconds"),
                who=who,
                statistic=self.label(statistic_id),
                repair_from=repair_from,
                supply_mode=supply_mode,
                start_total=None if start_total is None else round(start_total, 6),
                rows=rows,
                join_repaired=join_repaired,
                verified=verified,
            )
        )

    def as_diagnostics(self) -> dict[str, Any]:
        """Return the trail and broken statistics for the diagnostics download."""
        return {
            "recent_writes": [asdict(record) for record in self.trail],
            "not_joined": dict(self.broken),
        }

    # --- Broken chains and the Repairs notice -----------------------------------------

    def report(self, statistic_id: str, bad_start: float | None) -> None:
        """Record a chain check: ``bad_start`` is the first row that fails to join, or None.

        The notice is raised while any checked statistic fails and cleared by the check that
        finds the last one joined again.
        """
        label = self.label(statistic_id)
        if bad_start is None:
            if self.broken.pop(label, None) is None:
                return
        else:
            local = dt_util.as_local(dt_util.utc_from_timestamp(bad_start))
            self.broken[label] = local.strftime("%d %b %Y %H:%M")
        self._sync_issue()

    def reset(self) -> None:
        """Forget every failed check and drop the notice (before a repair re-checks)."""
        self.broken.clear()
        self._sync_issue()

    def _sync_issue(self) -> None:
        if not self.broken:
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)
            return
        label, hour = next(iter(self.broken.items()))
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self.issue_id,
            is_fixable=self.entry_id is not None,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PREFIX,
            translation_placeholders={"statistic": label, "hour": hour},
            data={"entry_id": self.entry_id},
        )


def guard_for(hass: HomeAssistant, nmi: str) -> ChainGuard:
    """Return the guard shared by everything that writes an NMI's statistics."""
    guards: dict[str, ChainGuard] = hass.data.setdefault(_KEY, {})
    key = nmi.lower()
    if key not in guards:
        guards[key] = ChainGuard(hass, nmi)
    return guards[key]


def local_hour_text(moment: datetime) -> str:
    """Return a moment as the local hour shown to the user."""
    return dt_util.as_local(moment).strftime("%d %b %Y %H:%M")
