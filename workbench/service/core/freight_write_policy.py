"""Server-side guardrail for explicitly authorized ERP logistics writes.

The global write switch only enables the ERP transport.  A separate policy
must authorize either a finite batch or all eligible differences before any
request can leave the service.  Missing configuration always fails closed.
"""
from dataclasses import dataclass


def _text(value):
    return str(value or '').strip()


@dataclass(frozen=True)
class FreightWritePolicy:
    max_per_job: int = 0
    allowlist: frozenset = frozenset()
    allow_all: bool = False
    write_all: bool = False

    def __post_init__(self):
        if isinstance(self.max_per_job, bool) or not isinstance(self.max_per_job, int):
            raise ValueError('物流写入每任务上限必须是整数')
        if self.max_per_job < 0 or self.max_per_job > 1000:
            raise ValueError('物流写入每任务上限必须在 0 至 1000 之间')
        normalized = frozenset(_text(item) for item in self.allowlist if _text(item))
        object.__setattr__(self, 'allowlist', normalized)
        if self.write_all and (self.max_per_job > 0 or normalized):
            raise ValueError('物流全量写入不能同时配置每任务上限或订单白名单')

    @property
    def configured(self):
        """True only when a finite boundary or explicit full mode is present."""
        return self.write_all or self.max_per_job > 0 or bool(self.allowlist)

    @property
    def scope(self):
        if self.write_all:
            return 'all'
        if self.max_per_job > 0 or self.allowlist:
            return 'bounded'
        return 'disabled'

    @classmethod
    def from_config(cls, max_per_job=None, allowlist=None, scope=None):
        raw_limit = _text(max_per_job)
        try:
            limit = int(raw_limit) if raw_limit else 0
        except ValueError:
            raise ValueError('物流写入每任务上限必须是整数') from None
        if isinstance(allowlist, str):
            allowed = frozenset(part.strip() for part in allowlist.split(',') if part.strip())
        elif allowlist:
            allowed = frozenset(_text(part) for part in allowlist if _text(part))
        else:
            allowed = frozenset()
        requested_scope = _text(scope).lower()
        if requested_scope not in {'', 'bounded', 'all'}:
            raise ValueError('物流写入范围必须是 bounded 或 all')
        if requested_scope == 'all':
            return cls(allow_all=True, write_all=True,
                       max_per_job=limit, allowlist=allowed)
        if not limit and not allowed:
            return cls.deny()
        return cls(max_per_job=limit, allowlist=allowed, allow_all=not bool(allowed))

    @classmethod
    def deny(cls):
        return cls()

    @classmethod
    def unbounded_for_injected_sender(cls):
        """Compatibility policy for directly injected test senders only."""
        return cls(allow_all=True, write_all=True)

    @classmethod
    def thaw(cls, value):
        if not isinstance(value, dict):
            return cls.deny()
        try:
            allowlist = value.get('allowlist')
            return cls(max_per_job=value.get('max_per_job', 0),
                       allowlist=frozenset(allowlist or ()),
                       allow_all=value.get('allow_all') is True,
                       write_all=(value.get('scope') == 'all' or
                                  value.get('write_all') is True))
        except (TypeError, ValueError):
            # A damaged durable task must never widen into a write policy.
            return cls.deny()

    def freeze(self):
        return {'max_per_job': self.max_per_job,
                'allowlist': sorted(self.allowlist),
                'allow_all': self.allow_all,
                'write_all': self.write_all,
                'scope': self.scope,
                'configured': self.configured}

    def select(self, changes):
        """Return deterministic selected/deferred targets and diagnostics."""
        candidates = list(changes)
        if not self.configured:
            return [], candidates, {
                'reason': 'write_policy_missing',
                'allowlist_deferred': 0,
                'limit_deferred': len(candidates),
            }
        if self.write_all:
            return candidates, [], {
                'reason': 'all',
                'allowlist_deferred': 0,
                'limit_deferred': 0,
            }
        if self.allow_all:
            eligible = candidates
            allowlist_deferred = []
        else:
            eligible = [target for target in candidates
                        if _text(target.get('tradeid')) in self.allowlist]
            allowlist_deferred = [target for target in candidates
                                  if _text(target.get('tradeid')) not in self.allowlist]
        limit = self.max_per_job if self.max_per_job > 0 else len(eligible)
        selected = eligible[:limit]
        limit_deferred = eligible[limit:]
        deferred_ids = {id(target) for target in allowlist_deferred + limit_deferred}
        deferred = [target for target in candidates if id(target) in deferred_ids]
        return selected, deferred, {
            'reason': 'bounded',
            'allowlist_deferred': len(allowlist_deferred),
            'limit_deferred': len(limit_deferred),
        }
