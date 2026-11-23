"""Plain-text digests built from the care log. No model calls: what staff read is what was logged."""
from datetime import datetime

from . import store

LABELS = {"attention": "Needs a look", "no_answer": "No answer", "open": "Open request"}


def when(event):
    return datetime.fromisoformat(event["at"]).strftime("%a %H:%M")


def line(event):
    # Summaries only: email leaves the robot, so the resident's own words stay on the dashboard.
    return f"- [{LABELS.get(event['status'], event['status'])}] {when(event)} {event['summary']}"


def staff_digest(log, device, day):
    """(subject, body) for the morning email: the exceptions list, then yesterday's numbers."""
    name = device.get("name") or device.get("serial") or "Ember"
    items = log.exceptions(day)
    start, end = store.day_bounds(day, log.clock)
    yesterday = log.metrics(start - 86400, start)
    subject = f"Ember {name}: {len(items)} item{'s' if len(items) != 1 else ''} to check today" if items \
        else f"Ember {name}: nothing to check today"
    parts = [f"Ember care digest for {name}, {day:%A %d %B}.", ""]
    if items:
        parts += ["To check:"] + [line(e) for e in items]
    else:
        parts.append("Nothing needs a visit so far today.")
    counts = ", ".join(f"{kind.replace('_', ' ')}: {sum(statuses.values())}"
                       for kind, statuses in sorted(yesterday["by_kind"].items())) or "no events"
    parts += ["", f"Yesterday: {counts}.",
              "", "Mark items handled on the robot's dashboard (/care). Ember suggests; staff decide."]
    return subject, "\n".join(parts)
