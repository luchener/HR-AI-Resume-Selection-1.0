# -*- coding: utf-8 -*-
"""原子写（临时文件 + os.replace）及其 Windows 重试、同进程并发 测试。unittest，零依赖。"""
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import auth as auth_mod
import config
import notifications as notifications_mod
import resume_original as resume_original_mod
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


# 最小合法 PNG 头（只需通过 save_email_image 的 magic 校验）
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _hammer(threads: int, rounds: int, work) -> list:
    """让 threads 个线程在同一个屏障后同时开跑 work(idx, round)，返回异常摘要。"""
    errors: list = []
    gate = threading.Barrier(threads)

    def runner(idx: int) -> None:
        try:
            gate.wait(timeout=15)
            for rnd in range(rounds):
                work(idx, rnd)
        except BaseException as exc:  # noqa: BLE001 - 测试要把任何异常都记下来
            errors.append("%s: %s" % (type(exc).__name__, exc))

    pool = [threading.Thread(target=runner, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=60)
    return errors


class AtomicWriteConcurrencyTest(unittest.TestCase):
    """回归：临时文件名只带 pid 时，同进程并发写同一文件会互相 rename 掉对方的 .tmp。

    2026-10-14 上线验证时由 test_hr_analysis 的并发用例在容器里抓到，表现为
    `os.replace` 抛 FileNotFoundError（另一个线程已经把同名 .tmp rename 走了）。
    生产触发面：同一账号"批量分析多份简历"= 一个请求内多线程并行，每份都要预占配额
    → 都写同一个 usage/<user_id>.json。

    这些用例必须**不依赖**外部锁：原子写的正确性不能建立在"锁一定覆盖到每一次写"上。
    """

    THREADS = 16
    ROUNDS = 25

    def _assert_clean_dir(self, directory: str) -> None:
        leftovers = sorted(p.name for p in Path(directory).glob("*.tmp"))
        self.assertEqual(leftovers, [], "并发写完后不许残留临时文件：%s" % leftovers)

    def test_auth_write_json_concurrent_same_path(self):
        tmpdir = tempfile.mkdtemp(prefix="atomic-auth-")
        try:
            path = os.path.join(tmpdir, "usage", "u1.json")
            errors = _hammer(
                self.THREADS,
                self.ROUNDS,
                lambda i, j: auth_mod._write_json(path, {"thread": i, "round": j}),
            )
            self.assertEqual(errors, [], "同进程并发写同一文件不允许抛 FileNotFoundError")
            with open(path, encoding="utf-8") as fh:
                json.load(fh)  # 必须是完整 JSON，不能是半写文件
            self._assert_clean_dir(os.path.dirname(path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_store_write_json_concurrent_same_path(self):
        tmpdir = tempfile.mkdtemp(prefix="atomic-store-")
        try:
            path = os.path.join(tmpdir, "resumes", "r1.json")
            errors = _hammer(
                self.THREADS,
                self.ROUNDS,
                lambda i, j: store_mod._write_json(path, {"thread": i, "round": j}),
            )
            self.assertEqual(errors, [], "同进程并发写同一文件不允许抛 FileNotFoundError")
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
            self._assert_clean_dir(os.path.dirname(path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_email_image_concurrent(self):
        tmpdir = tempfile.mkdtemp(prefix="atomic-img-")
        try:
            with mock.patch.object(config, "EMAIL_IMAGES_DIR", tmpdir):
                errors = _hammer(
                    self.THREADS,
                    4,
                    lambda i, j: notifications_mod.save_email_image(_PNG, "pic-%d-%d.png" % (i, j)),
                )
            self.assertEqual(errors, [], "并发存图不允许因为临时文件撞名而失败")
            self._assert_clean_dir(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_original_concurrent_same_resume(self):
        """同一份简历并发留存原件。

        这条只断言"不许崩、不许残留、落盘内容完好"，不断言每一次都成功：
        `save()` 本身就设计成失败降级（返回 {}，原件只是少一层保底），
        而 Windows 上并发 `os.replace` 同一目标会抛 PermissionError（POSIX 的 rename
        是原子的、不会因此失败）。本次回归要盯的是**临时文件撞名**这一类，
        不是"所有写者都必须赢"。
        """
        tmpdir = tempfile.mkdtemp(prefix="atomic-orig-")
        try:
            with mock.patch.object(config, "RESUME_ORIGINALS_DIR", tmpdir):
                results: list = []
                lock = threading.Lock()
                payloads = [b"%PDF-1.7\n" + bytes([i, j]) * 8 for i in range(self.THREADS) for j in range(4)]

                def work(i: int, j: int) -> None:
                    meta = resume_original_mod.save(
                        "same-resume-id", b"%PDF-1.7\n" + bytes([i, j]) * 8, "resume.pdf", "application/pdf"
                    )
                    with lock:
                        results.append((i, j, meta))

                errors = _hammer(self.THREADS, 4, work)
                self.assertEqual(errors, [], "并发留存原件不允许把异常抛到调用方")
                self.assertEqual(len(results), self.THREADS * 4, "每个写者都应当返回结果")
                self.assertTrue(any(meta for _, _, meta in results), "至少要有一次写成功")
                stored = os.path.join(tmpdir, "same-resume-id.pdf")
                self.assertTrue(os.path.exists(stored), "目标文件必须存在")
                with open(stored, "rb") as fh:
                    written = fh.read()
                self.assertIn(
                    written,
                    payloads,
                    "落盘内容必须来自某一次完整写入，不能是半写拼接",
                )
                self._assert_clean_dir(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
