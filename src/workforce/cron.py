"""Cron из 5 полей (минута час день месяц день-недели), время UTC (C-6, «Runtime работ»)."""

from datetime import datetime, timedelta, timezone


FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
MAX_SLOTS = 1000


class CronError(ValueError):
    pass


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
        except ValueError as error:
            raise CronError(f"bad step in {part!r}") from error
        if step < 1:
            raise CronError(f"bad step in {part!r}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, _, right = base.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError as error:
                raise CronError(f"bad range {part!r}") from error
        else:
            try:
                start = end = int(base)
            except ValueError as error:
                raise CronError(f"bad value {part!r}") from error
            if step_text:
                end = high
        if not low <= start <= end <= high:
            raise CronError(f"{part!r} is outside {low}..{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


class Cron:
    def __init__(self, expression: str):
        parts = expression.split() if isinstance(expression, str) else []
        if len(parts) != 5:
            raise CronError("cron must have 5 fields: minute hour day month weekday")
        self.expression = expression
        minute, hour, day, month, weekday = (_field(p, lo, hi) for p, (lo, hi) in zip(parts, FIELDS))
        self._minute, self._hour, self._month = minute, hour, month
        self._day, self._weekday = day, frozenset(d % 7 for d in weekday)  # 7 = воскресенье = 0
        self._day_any, self._weekday_any = parts[2] == "*", parts[4] == "*"

    def matches(self, moment: datetime) -> bool:
        moment = moment.astimezone(timezone.utc)
        if moment.minute not in self._minute or moment.hour not in self._hour or moment.month not in self._month:
            return False
        day_ok = moment.day in self._day
        weekday_ok = (moment.isoweekday() % 7) in self._weekday
        if self._day_any or self._weekday_any:
            return day_ok and weekday_ok
        return day_ok or weekday_ok  # оба поля заданы — как в классическом cron: «или»

    def slots(self, start: datetime, end: datetime) -> list[datetime]:
        """Минуты в (start, end], подходящие под выражение, по возрастанию."""
        moment = start.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = end.astimezone(timezone.utc)
        found: list[datetime] = []
        while moment <= end and len(found) < MAX_SLOTS:
            if self.matches(moment):
                found.append(moment)
            moment += timedelta(minutes=1)
        return found


def validate(expression: str) -> str | None:
    try:
        Cron(expression)
    except CronError as error:
        return str(error)
    return None
