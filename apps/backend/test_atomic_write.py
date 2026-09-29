# -*- coding: utf-8 -*-
"""原子写（临时文件 + os.replace）及其 Windows 重试 测试。unittest，零依赖。"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import store as store_mod


class AtomicReplaceTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="atomic-")
        self.path = os.path.join(self._dir, "rec.json")

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_normal_write(self):
        store_mod._write_json(self.path, {"a": 1})
        self.assertEqual(store_mod._read_json(self.path), {"a": 1})

    def test_retries_on_permission_error(self):
        """回归：Windows 上杀毒/索引器瞬时占用 .tmp 会让 os.replace 抛 WinError 5。

        旧实现直接把这个 PermissionError 冒到路由层（/api/v1/archives/<id>/tags 随机 500、
        登录失败计数写不进去）。现在应退避重试后成功。
        """
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(5, "拒绝访问。")
            return real_replace(src, dst)

        with mock.patch("store.os.replace", side_effect=flaky), mock.patch("store.time.sleep") as sleeper:
            store_mod._write_json(self.path, {"a": 2})

        self.assertEqual(calls["n"], 3, "应重试到第 3 次成功")
        self.assertEqual(sleeper.call_count, 2, "前两次失败各退避一次")
        self.assertEqual(store_mod._read_json(self.path), {"a": 2})

    def test_raises_after_retries_exhausted(self):
        """重试用尽后仍要抛出真实错误，不能假装写成功。"""
        with mock.patch("store.os.replace", side_effect=PermissionError(5, "拒绝访问。")), mock.patch("store.time.sleep"):
            with self.assertRaises(PermissionError):
                store_mod._write_json(self.path, {"a": 3})
        self.assertFalse(os.path.exists(self.path))

    def test_no_leftover_tmp_file(self):
        """成功后不留 .tmp 残骸。"""
        store_mod._write_json(self.path, {"a": 4})
        self.assertEqual([f for f in os.listdir(self._dir) if f.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
