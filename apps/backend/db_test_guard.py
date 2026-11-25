"""Connection guard for destructive synthetic database rehearsals only."""
from sqlalchemy.engine import make_url


def review_test_url(value):
    try:
        url = make_url(value)
        valid = (url.drivername in ('postgresql', 'postgresql+psycopg')
                 and not url.query and url.database == 'review_test'
                 and url.host in ('127.0.0.1', 'localhost') and url.port == 15439
                 and url.username == 'review_bootstrap')
    except Exception:
        raise ValueError('Invalid synthetic review database URL') from None
    if not valid:
        raise ValueError('Only the isolated loopback review database is allowed')
    return url
