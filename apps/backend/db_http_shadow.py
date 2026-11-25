"""Compare captured API envelopes without logging bodies, tokens or user data.

Pure offline comparison. Callers capture responses themselves; this module never
dispatches/retries requests or duplicates writes. Only top-level request IDs are
excluded, with their presence/type checked. No other differences are hidden.
"""
from db_json_compare import json_equal


def compare_responses(expected_status, expected_body, actual_status, actual_body):
    def envelope(body):
        return (isinstance(body, dict) and isinstance(body.get('request_id'), str)
                and bool(body['request_id']) and ('data' in body or 'detail' in body or
                    (isinstance(body.get('message'), str) and bool(body['message']))))
    valid = (type(expected_status) is int and type(actual_status) is int
             and 100 <= expected_status <= 599 and 100 <= actual_status <= 599
             and envelope(expected_body) and envelope(actual_body))
    same_status = bool(valid and expected_status == actual_status)
    same_body = bool(valid and json_equal(
        {key: value for key, value in expected_body.items() if key != 'request_id'},
        {key: value for key, value in actual_body.items() if key != 'request_id'}))
    return {'equal': same_status and same_body, 'status_matches': same_status,
            'body_matches': same_body, 'valid_envelopes': bool(valid)}
