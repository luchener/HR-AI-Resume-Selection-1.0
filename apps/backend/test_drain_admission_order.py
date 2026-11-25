# -*- coding: utf-8 -*-
"""排空 admission 与进程登记的先后顺序（部署/回滚约束）。

复核关注点（P1）：`gunicorn_source_drain.on_starting` 会为 master 调
`Coordinator.process_started`，而该方法在 draining 状态下抛 `AdmissionClosed`。
所以**必须先 `resume` 再启动新后端**，否则新进程无法起来，后续步骤也不会自然执行。

这里用真实 `source_drain` 模块和临时 SQLite 登记库验证：
- begin 之后登记新进程被拒绝；
- resume 之后可以登记；
- 回滚分支同样必须先 resume。
"""
import tempfile
import unittest
from pathlib import Path

import source_drain


class DrainAdmissionOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'control.sqlite'
        self.coordinator = source_drain.Coordinator.initialize(self.path)

    def test_begin_blocks_new_process_registration(self):
        self.coordinator.begin()
        with self.assertRaises(source_drain.AdmissionClosed):
            self.coordinator.process_started('master')
        # 部署顺序要求：启动替换进程之前恢复 admission
        self.coordinator.resume()
        token = self.coordinator.process_started('master')
        self.assertTrue(token)

    def test_rollback_must_resume_before_restarting_old_image(self):
        """失败回滚同样受同一共享登记状态约束：只切回旧 tag 不够。"""
        token = self.coordinator.process_started('master')
        self.coordinator.process_exited(token)

        self.coordinator.begin()
        with self.assertRaises(source_drain.AdmissionClosed):
            self.coordinator.process_started('master')  # 旧镜像也起不来

        self.coordinator.resume()
        self.assertTrue(self.coordinator.process_started('master'))

    def test_status_exposes_draining_flag(self):
        self.assertFalse(self.coordinator.status()['draining'])
        self.coordinator.begin()
        status = self.coordinator.status()
        self.assertTrue(status['draining'])
        self.assertTrue(status['admission_drained'])
        self.coordinator.resume()
        self.assertFalse(self.coordinator.status()['draining'])

    def test_resume_is_required_even_with_no_outstanding_work(self):
        self.coordinator.begin()
        status = self.coordinator.status()
        self.assertEqual({key: status[key] for key in source_drain.KINDS},
                         {key: 0 for key in source_drain.KINDS})
        with self.assertRaises(source_drain.AdmissionClosed):
            self.coordinator.process_started('worker')

    def test_worker_registration_after_resume(self):
        self.coordinator.begin()
        self.coordinator.resume()
        master = self.coordinator.process_started('master')
        worker = self.coordinator.process_started('worker')
        self.assertNotEqual(master, worker)
        self.coordinator.process_exited(worker)
        self.coordinator.process_exited(master)


if __name__ == '__main__':
    unittest.main()
