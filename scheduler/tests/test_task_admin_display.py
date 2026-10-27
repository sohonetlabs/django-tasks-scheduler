from datetime import timedelta
from unittest.mock import PropertyMock, patch

import redis
from django.contrib import admin
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from scheduler.admin.task_admin import TaskAdmin
from scheduler.helpers.queues import get_queue
from scheduler.models import Task, TaskType
from scheduler.settings import logger
from scheduler.tests.testtools import SchedulerBaseCase, task_factory


class TestTaskAdminNextRunDisplay(TestCase):
    def setUp(self) -> None:
        self.admin = TaskAdmin(Task, admin.site)

    def test_next_run_returns_not_scheduled_when_scheduled_time_is_none(self) -> None:
        # Regression: TypeError when scheduled_time is None (issue #363)
        task = task_factory(TaskType.CRON, enabled=False)
        task.scheduled_time = None
        task.save(clean=False)

        result = self.admin.next_run(task)

        assert str(result) == "Not scheduled"

    def test_next_run_shows_a_past_time_without_saving_the_task(self) -> None:
        task = task_factory(TaskType.REPEATABLE)
        past = timezone.now() - timedelta(minutes=5)
        Task.objects.filter(pk=task.pk).update(scheduled_time=past)
        task.refresh_from_db()

        with self.assertNumQueries(0):
            result = self.admin.next_run(task)

        # next_run makes the time aware; testproject stores naive times.
        assert result.replace(tzinfo=None) == task.scheduled_time.replace(tzinfo=None)


class TestTaskAdminChangelistBrokerReads(SchedulerBaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.login(username="admin", password="admin")
        self.url = reverse("admin:scheduler_task_changelist")

    def test_changelist_reads_each_queue_once_for_the_whole_page(self) -> None:
        tasks = [task_factory(TaskType.REPEATABLE) for _ in range(3)]
        queue = get_queue(tasks[0].queue)

        with patch.object(Task, "rqueue", new_callable=PropertyMock, return_value=queue) as rqueue:
            res = self.client.get(self.url)

        assert res.status_code == 200
        assert rqueue.call_count == 1
        assert {task._is_scheduled for task in res.context["cl"].result_list} == {True}

    def test_changelist_query_count_does_not_grow_with_tasks(self) -> None:
        task_factory(TaskType.REPEATABLE)
        self.client.get(self.url)
        with CaptureQueriesContext(connection) as before:
            self.client.get(self.url)
        for _ in range(3):
            task_factory(TaskType.REPEATABLE)
        with CaptureQueriesContext(connection) as after:
            self.client.get(self.url)

        assert len(after) == len(before)

    def test_changelist_shows_each_task_s_own_state(self) -> None:
        scheduled = task_factory(TaskType.REPEATABLE)
        disabled = task_factory(TaskType.REPEATABLE, enabled=False)

        res = self.client.get(self.url)

        states = {task.pk: task._is_scheduled for task in res.context["cl"].result_list}
        assert states == {scheduled.pk: True, disabled.pk: False}

    def test_changelist_shows_unknown_state_when_the_broker_cannot_be_read(self) -> None:
        task = task_factory(TaskType.REPEATABLE)

        with (
            patch("redis.client.Pipeline.execute", side_effect=redis.ConnectionError("down")),
            self.assertLogs(logger, level="ERROR") as logs,
        ):
            res = self.client.get(self.url)

        assert len(logs.output) == 1
        assert logs.output[0].startswith(f"ERROR:scheduler:Could not inspect the tasks on queue {task.queue}")

        assert res.status_code == 200
        assert {t.pk: t._is_scheduled for t in res.context["cl"].result_list} == {task.pk: None}
