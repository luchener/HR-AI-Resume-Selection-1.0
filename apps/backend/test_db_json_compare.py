import unittest
from db_json_compare import json_equal

class JsonComparisonTests(unittest.TestCase):
    def test_boolean_numeric_drift_rejected_recursively(self):
        self.assertFalse(json_equal({'a':[True,{'b':False}]},{'a':[1,{'b':0}]}))
        self.assertFalse(json_equal(True,1))
        self.assertTrue(json_equal({'n':1},{'n':1.0}))
        self.assertTrue(json_equal({'b':False},{'b':False}))

if __name__ == '__main__':
    unittest.main()
