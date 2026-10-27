"""Progress, derived from the work items TypeDB holds (docs/phase6-plan.md §12, decision D5).

Two measures, never one unweighted average:

- **Activity completion**: the duration-weighted mean of reported progress over items that have a duration
  (a planned start and a due date). An item's weight is its planned length in days, a stated proxy for
  effort, so a six-week drafting task counts for more than a two-day filing. Complete items count as 100%.
- **Milestone readiness**: point-in-time milestones and conditions achieved, out of all of them.

Contractual deadlines are excluded from both: they are limits, not work. Calculated in Python because
TypeQL can't subtract dates; every input is read from TypeDB.
"""

from datetime import date


def _d(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def activity_completion(items):
    """{"percent", "items", "weighted_days"} over duration-bearing items, or None if there are none."""
    total = weighted = 0
    count = 0
    for it in items:
        if it.get("type") in ("deadline", "workstream") or not it.get("planned_start") or not it.get("due"):
            continue
        days = (_d(it["due"]) - _d(it["planned_start"])).days + 1
        done = 100 if it.get("status") == "complete" else (it.get("progress") or 0)
        total += days
        weighted += days * done
        count += 1
    if not count:
        return None
    return {"percent": round(weighted / total), "items": count, "weighted_days": total}


def milestone_readiness(items):
    """{"achieved", "total"} over point-in-time items (no planned start), deadlines excluded."""
    points = [it for it in items if it.get("type") not in ("deadline", "workstream") and not it.get("planned_start")]
    return {"achieved": sum(1 for it in points if it.get("status") == "complete"), "total": len(points)}


def summary(items):
    """Both measures, plus the headline: activity completion where there is any, otherwise readiness."""
    completion = activity_completion(items)
    readiness = milestone_readiness(items)
    if completion:
        headline = completion["percent"]
        basis = f"Duration-weighted completion of {completion['items']} items with planned durations"
    elif readiness["total"]:
        headline = round(100 * readiness["achieved"] / readiness["total"])
        basis = f"Milestones achieved ({readiness['achieved']} of {readiness['total']}); no activities with durations"
    else:
        headline, basis = 0, "No work items yet"
    return {"percent": headline, "basis": basis, "activity_completion": completion, "milestone_readiness": readiness}
