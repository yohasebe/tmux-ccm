"""Pure labels and guidance for records; never grants permission to act."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ActionSpec:
    key_hint: str
    description: str


@dataclass(frozen=True)
class RecordSpec:
    label: str
    category: str
    attention: str
    action_id: str


ACTIONS = MappingProxyType({
    'read': ActionSpec('Enter read', 'Read the record before deciding what to do.'),
    'open': ActionSpec('o open', 'Check the conversation and input box; do not send another copy.'),
    'discard': ActionSpec('d discard', 'Discard the record only; conversation and input box stay unchanged.'),
    'resend': ActionSpec('r resend', 'Send an expired message as a new message.'),
})

_EXPIRED = 'Expired before delivery'
_HELD = 'Waiting in input box'
_UNCONFIRMED = 'Delivery unconfirmed'
_QUEUED = 'Queued; not delivered yet'
RECORDS = MappingProxyType({
    'pending': RecordSpec(_QUEUED, 'message', 'info', 'read'),
    'expired': RecordSpec(_EXPIRED, 'message', 'warning', 'read'),
    'held': RecordSpec(_HELD, 'message', 'warning', 'open'),
    'notice-pending': RecordSpec(_QUEUED, 'notice', 'info', 'read'),
    'notice-expired': RecordSpec(_EXPIRED, 'notice', 'warning', 'read'),
    'notice-held': RecordSpec(_HELD, 'notice', 'warning', 'open'),
    'notice-uncertain': RecordSpec(_UNCONFIRMED, 'notice', 'warning', 'open'),
    'notice-attempted': RecordSpec(_UNCONFIRMED, 'notice', 'warning', 'open'),
    'notice-limited': RecordSpec('Not sent: hourly limit', 'notice', 'warning', 'read'),
    'notice-cancelled': RecordSpec('Cancelled before delivery', 'notice', 'warning', 'read'),
})
_UNKNOWN = RecordSpec('Unrecognized record; review diagnostic details.', 'unknown', 'warning', 'read')
_CATEGORIES = MappingProxyType({'message': 'Message', 'notice': 'Completion notice', 'unknown': 'Record'})


def record_spec(kind: str) -> RecordSpec:
    return RECORDS.get(kind, _UNKNOWN)


def record_heading(kind: str) -> str:
    spec = record_spec(kind)
    return f'{_CATEGORIES[spec.category]} · {spec.label}'


def record_title(kind: str, project: str) -> str:
    spec = record_spec(kind)
    return f'{_CATEGORIES[spec.category]} for {project} — {spec.label}'


def record_summary(counts: Mapping[str, int]) -> str:
    """Nonzero counts only. Callers select the states relevant to their check."""
    return '; '.join(f'{count} {record_spec(kind).label}'
                     for kind, count in counts.items() if count > 0)


def record_help(actions) -> str:
    """Render execution-layer hints; callers still validate every operation."""
    return ' · '.join(['↑↓ select'] + [ACTIONS[action].key_hint for action in actions] + ['q back'])
