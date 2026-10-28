"""Bounded retries of one read source, never of ERP business writes."""
from functools import wraps
import time
from .api_source import SourceChanged


def stable_page(items):
    # Preserve every document and every detail, including duplicate rows.
    # Only page-level ordering is immaterial; never normalize business fields.
    import json
    return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')) for row in items)


def retry_source_read(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        for attempt in range(1, 4):
            try:
                return function(self, *args, **kwargs)
            except SourceChanged as error:
                self.progress({'source': function.__name__.lstrip('_'), 'current': 0,
                    'total': 0, 'retry_attempt': attempt,
                    'retry_reason': str(error)})
                if attempt == 3:
                    raise
                time.sleep(0.25 * attempt)
    return wrapped
