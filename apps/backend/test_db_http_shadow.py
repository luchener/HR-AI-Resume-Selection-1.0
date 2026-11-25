from unittest import TestCase

from db_http_shadow import compare_responses


class ShadowTests(TestCase):
    def test_message_only_mutation_envelopes_preserve_strict_comparison(self):
        left = {'request_id':'json:1','message':'deleted'}
        self.assertTrue(compare_responses(200,left,200,dict(left,request_id='db:1'))['equal'])
        for right in ({'message':'deleted'}, dict(left,message='other'), dict(left,message=True),
                      dict(left,message=''), dict(left,extra=True)):
            self.assertFalse(compare_responses(200,left,200,right)['equal'])

    def test_only_request_ids_are_excluded(self):
        self.assertTrue(compare_responses(200, {'request_id': 'json:1', 'data': {'v': 3}},
                                          200, {'request_id': 'db:2', 'data': {'v': 3}})['equal'])
        self.assertFalse(compare_responses(200, {'request_id': 'json:1', 'data': {}},
                                           200, {'data': {}})['equal'])

    def test_values_extra_fields_order_and_boolean_confusion_are_detected(self):
        for actual in ({'value': False}, {'value': 0}, {'value': 1, 'unexpected': True}, {'value': '1'}):
            self.assertFalse(compare_responses(200, {'request_id': 'j', 'data': {'value': True}},
                                               200, {'request_id': 'd', 'data': actual})['equal'])
        self.assertFalse(compare_responses(200, {'request_id': 'j', 'data': [1, 2]},
                                           200, {'request_id': 'd', 'data': [2, 1]})['equal'])

    def test_error_status_and_error_body_are_both_checked(self):
        expected = {'request_id': 'j', 'detail': 'denied'}
        self.assertFalse(compare_responses(401, expected, 200, dict(expected, request_id='d'))['equal'])
        self.assertFalse(compare_responses(401, expected, 401, dict(expected, detail='other'))['equal'])
        self.assertFalse(compare_responses(401, expected, True, expected)['equal'])

    def test_results_never_contain_values_or_field_names(self):
        result = compare_responses(200, {'request_id': 'j', 'data': {'private_key': 'SECRET_ONE'}},
                                   200, {'request_id': 'd', 'data': {'private_key': 'SECRET_TWO'}})
        self.assertNotIn('SECRET', str(result))
        self.assertNotIn('private_key', str(result))
