from __future__ import absolute_import
from __future__ import with_statement

from datetime import datetime, timedelta
from functools import wraps

from celery import task
from celery.task import current
from celery.app import app_or_default
from celery.task import task as task_dec
from celery.exceptions import RetryTaskError
from celery.execute import send_task
from celery.result import EagerResult
from celery.schedules import crontab, crontab_parser, ParseException
from celery.utils import uuid
from celery.utils.timeutils import parse_iso8601, timedelta_seconds

from celery.tests.utils import Case, with_eager_tasks, WhateverIO


def return_True(*args, **kwargs):
    # Task run functions can't be closures/lambdas, as they're pickled.
    return True


return_True_task = task_dec()(return_True)


def raise_exception(self, **kwargs):
    raise Exception("%s error" % self.__class__)


class MockApplyTask(task.Task):
    applied = 0

    def run(self, x, y):
        return x * y

    @classmethod
    def apply_async(self, *args, **kwargs):
        self.applied += 1


@task.task(name="c.unittest.increment_counter_task", count=0)
def increment_counter(increment_by=1):
    increment_counter.count += increment_by or 1
    return increment_counter.count


@task.task(name="c.unittest.raising_task")
def raising():
    raise KeyError("foo")


@task.task(max_retries=3, iterations=0)
def retry_task(arg1, arg2, kwarg=1, max_retries=None, care=True):
    current.iterations += 1
    rmax = current.max_retries if max_retries is None else max_retries

    retries = current.request.retries
    if care and retries >= rmax:
        return arg1
    else:
        return current.retry(countdown=0, max_retries=rmax)


@task.task(max_retries=3, iterations=0)
def retry_task_noargs(**kwargs):
    current.iterations += 1

    retries = kwargs["task_retries"]
    if retries >= 3:
        return 42
    else:
        return current.retry(countdown=0)


@task.task(max_retries=3, iterations=0, base=MockApplyTask)
def retry_task_mockapply(arg1, arg2, kwarg=1, **kwargs):
    current.iterations += 1

    retries = kwargs["task_retries"]
    if retries >= 3:
        return arg1
    else:
        kwargs.update(kwarg=kwarg)
    return current.retry(countdown=0)


class MyCustomException(Exception):
    """Random custom exception."""


@task.task(max_retries=3, iterations=0, accept_magic_kwargs=True)
def retry_task_customexc(arg1, arg2, kwarg=1, **kwargs):
    current.iterations += 1

    retries = kwargs["task_retries"]
    if retries >= 3:
        return arg1 + kwarg
    else:
        try:
            raise MyCustomException("Elaine Marie Benes")
        except MyCustomException, exc:
            kwargs.update(kwarg=kwarg)
            return current.retry(countdown=0, exc=exc)


class TestTaskRetries(Case):

    def test_retry(self):
        retry_task.__class__.max_retries = 3
        retry_task.iterations = 0
        result = retry_task.apply([0xFF, 0xFFFF])
        self.assertEqual(result.get(), 0xFF)
        self.assertEqual(retry_task.iterations, 4)

        retry_task.__class__.max_retries = 3
        retry_task.iterations = 0
        result = retry_task.apply([0xFF, 0xFFFF], {"max_retries": 10})
        self.assertEqual(result.get(), 0xFF)
        self.assertEqual(retry_task.iterations, 11)

    def test_retry_no_args(self):
        retry_task_noargs.__class__.max_retries = 3
        retry_task_noargs.iterations = 0
        result = retry_task_noargs.apply()
        self.assertEqual(result.get(), 42)
        self.assertEqual(retry_task_noargs.iterations, 4)

    def test_retry_kwargs_can_be_empty(self):
        with self.assertRaises(RetryTaskError):
            retry_task_mockapply.retry(args=[4, 4], kwargs=None)

    def test_retry_not_eager(self):
        retry_task_mockapply.request.called_directly = False
        exc = Exception("baz")
        try:
            retry_task_mockapply.retry(args=[4, 4], kwargs={"task_retries": 0},
                                       exc=exc, throw=False)
            self.assertTrue(retry_task_mockapply.__class__.applied)
        finally:
            retry_task_mockapply.__class__.applied = 0

        try:
            with self.assertRaises(RetryTaskError):
                retry_task_mockapply.retry(
                    args=[4, 4], kwargs={"task_retries": 0},
                    exc=exc, throw=True)
            self.assertTrue(retry_task_mockapply.__class__.applied)
        finally:
            retry_task_mockapply.__class__.applied = 0

    def test_retry_with_kwargs(self):
        retry_task_customexc.__class__.max_retries = 3
        retry_task_customexc.iterations = 0
        result = retry_task_customexc.apply([0xFF, 0xFFFF], {"kwarg": 0xF})
        self.assertEqual(result.get(), 0xFF + 0xF)
        self.assertEqual(retry_task_customexc.iterations, 4)

    def test_retry_with_custom_exception(self):
        retry_task_customexc.__class__.max_retries = 2
        retry_task_customexc.iterations = 0
        result = retry_task_customexc.apply([0xFF, 0xFFFF], {"kwarg": 0xF})
        with self.assertRaises(MyCustomException):
            result.get()
        self.assertEqual(retry_task_customexc.iterations, 3)

    def test_max_retries_exceeded(self):
        retry_task.__class__.max_retries = 2
        retry_task.iterations = 0
        result = retry_task.apply([0xFF, 0xFFFF], {"care": False})
        with self.assertRaises(retry_task.MaxRetriesExceededError):
            result.get()
        self.assertEqual(retry_task.iterations, 3)

        retry_task.__class__.max_retries = 1
        retry_task.iterations = 0
        result = retry_task.apply([0xFF, 0xFFFF], {"care": False})
        with self.assertRaises(retry_task.MaxRetriesExceededError):
            result.get()
        self.assertEqual(retry_task.iterations, 2)


class TestCeleryTasks(Case):

    def test_unpickle_task(self):
        import pickle

        @task_dec
        def xxx():
            pass

        self.assertIs(pickle.loads(pickle.dumps(xxx)), xxx.app.tasks[xxx.name])

    def createTask(self, name):
        return task.task(__module__=self.__module__, name=name)(return_True)

    def test_AsyncResult(self):
        task_id = uuid()
        result = retry_task.AsyncResult(task_id)
        self.assertEqual(result.backend, retry_task.backend)
        self.assertEqual(result.id, task_id)

    def assertNextTaskDataEqual(self, consumer, presult, task_name,
            test_eta=False, test_expires=False, **kwargs):
        next_task = consumer.fetch()
        task_data = next_task.decode()
        self.assertEqual(task_data["id"], presult.id)
        self.assertEqual(task_data["task"], task_name)
        task_kwargs = task_data.get("kwargs", {})
        if test_eta:
            self.assertIsInstance(task_data.get("eta"), basestring)
            to_datetime = parse_iso8601(task_data.get("eta"))
            self.assertIsInstance(to_datetime, datetime)
        if test_expires:
            self.assertIsInstance(task_data.get("expires"), basestring)
            to_datetime = parse_iso8601(task_data.get("expires"))
            self.assertIsInstance(to_datetime, datetime)
        for arg_name, arg_value in kwargs.items():
            self.assertEqual(task_kwargs.get(arg_name), arg_value)

    def test_incomplete_task_cls(self):

        class IncompleteTask(task.Task):
            name = "c.unittest.t.itask"

        with self.assertRaises(NotImplementedError):
            IncompleteTask().run()

    def test_task_kwargs_must_be_dictionary(self):
        with self.assertRaises(ValueError):
            increment_counter.apply_async([], "str")

    def test_task_args_must_be_list(self):
        with self.assertRaises(ValueError):
            increment_counter.apply_async("str", {})

    def test_regular_task(self):
        T1 = self.createTask("c.unittest.t.t1")
        self.assertIsInstance(T1, task.BaseTask)
        self.assertTrue(T1.run())
        self.assertTrue(callable(T1),
                "Task class is callable()")
        self.assertTrue(T1(),
                "Task class runs run() when called")

        consumer = T1.get_consumer()
        with self.assertRaises(NotImplementedError):
            consumer.receive("foo", "foo")
        consumer.discard_all()
        self.assertIsNone(consumer.fetch())

        # Without arguments.
        presult = T1.delay()
        self.assertNextTaskDataEqual(consumer, presult, T1.name)

        # With arguments.
        presult2 = T1.apply_async(kwargs=dict(name="George Costanza"))
        self.assertNextTaskDataEqual(consumer, presult2, T1.name,
                name="George Costanza")

        # send_task
        sresult = send_task(T1.name, kwargs=dict(name="Elaine M. Benes"))
        self.assertNextTaskDataEqual(consumer, sresult, T1.name,
                name="Elaine M. Benes")

        # With eta.
        presult2 = T1.apply_async(kwargs=dict(name="George Costanza"),
                            eta=datetime.utcnow() + timedelta(days=1),
                            expires=datetime.utcnow() + timedelta(days=2))
        self.assertNextTaskDataEqual(consumer, presult2, T1.name,
                name="George Costanza", test_eta=True, test_expires=True)

        # With countdown.
        presult2 = T1.apply_async(kwargs=dict(name="George Costanza"),
                                  countdown=10, expires=12)
        self.assertNextTaskDataEqual(consumer, presult2, T1.name,
                name="George Costanza", test_eta=True, test_expires=True)

        # Discarding all tasks.
        consumer.discard_all()
        T1.apply_async()
        self.assertEqual(consumer.discard_all(), 1)
        self.assertIsNone(consumer.fetch())

        self.assertFalse(presult.successful())
        T1.backend.mark_as_done(presult.id, result=None)
        self.assertTrue(presult.successful())

        publisher = T1.get_publisher()
        self.assertTrue(publisher.exchange)

    def test_context_get(self):
        request = self.createTask("c.unittest.t.c.g").request
        request.foo = 32
        self.assertEqual(request.get("foo"), 32)
        self.assertEqual(request.get("bar", 36), 36)
        request.clear()

    def test_task_class_repr(self):
        task = self.createTask("c.unittest.t.repr")
        self.assertIn("class Task of", repr(task.app.Task))

    def test_after_return(self):
        task = self.createTask("c.unittest.t.after_return")
        task.request.chord = return_True_task.s()
        task.after_return("SUCCESS", 1.0, "foobar", (), {}, None)
        task.request.clear()

    def test_send_task_sent_event(self):
        T1 = self.createTask("c.unittest.t.t1")
        app = T1.app
        conn = app.broker_connection()
        chan = conn.channel()
        app.conf.CELERY_SEND_TASK_SENT_EVENT = True
        dispatcher = [None]

        class Pub(object):
            channel = chan

            def delay_task(self, *args, **kwargs):
                dispatcher[0] = kwargs.get("event_dispatcher")

        try:
            T1.apply_async(publisher=Pub())
        finally:
            app.conf.CELERY_SEND_TASK_SENT_EVENT = False
            chan.close()
            conn.close()

        self.assertTrue(dispatcher[0])

    def test_get_publisher(self):
        connection = app_or_default().broker_connection()
        p = increment_counter.get_publisher(connection, auto_declare=False,
                                            exchange="foo")
        self.assertEqual(p.exchange.name, "foo")
        p = increment_counter.get_publisher(connection, auto_declare=False,
                                            exchange_type="fanout")
        self.assertEqual(p.exchange.type, "fanout")

    def test_update_state(self):

        @task_dec
        def yyy():
            pass

        tid = uuid()
        yyy.update_state(tid, "FROBULATING", {"fooz": "baaz"})
        self.assertEqual(yyy.AsyncResult(tid).status, "FROBULATING")
        self.assertDictEqual(yyy.AsyncResult(tid).result, {"fooz": "baaz"})

        yyy.request.id = tid
        yyy.update_state(state="FROBUZATING", meta={"fooz": "baaz"})
        self.assertEqual(yyy.AsyncResult(tid).status, "FROBUZATING")
        self.assertDictEqual(yyy.AsyncResult(tid).result, {"fooz": "baaz"})

    def test_repr(self):

        @task_dec
        def task_test_repr():
            pass

        self.assertIn("task_test_repr", repr(task_test_repr))

    def test_has___name__(self):

        @task_dec
        def yyy2():
            pass

        self.assertTrue(yyy2.__name__)

    def test_get_logger(self):
        t1 = self.createTask("c.unittest.t.t1")
        logfh = WhateverIO()
        logger = t1.get_logger(logfile=logfh, loglevel=0)
        self.assertTrue(logger)

        t1.request.loglevel = 3
        logger = t1.get_logger(logfile=logfh, loglevel=None)
        self.assertTrue(logger)


class TestTaskSet(Case):

    @with_eager_tasks
    def test_function_taskset(self):
        subtasks = [return_True_task.s(i) for i in range(1, 6)]
        ts = task.TaskSet(subtasks)
        res = ts.apply_async()
        self.assertListEqual(res.join(), [True, True, True, True, True])

    def test_counter_taskset(self):
        increment_counter.count = 0
        ts = task.TaskSet(tasks=[
            increment_counter.s(),
            increment_counter.s(increment_by=2),
            increment_counter.s(increment_by=3),
            increment_counter.s(increment_by=4),
            increment_counter.s(increment_by=5),
            increment_counter.s(increment_by=6),
            increment_counter.s(increment_by=7),
            increment_counter.s(increment_by=8),
            increment_counter.s(increment_by=9),
        ])
        self.assertEqual(ts.total, 9)

        consumer = increment_counter.get_consumer()
        consumer.purge()
        consumer.close()
        taskset_res = ts.apply_async()
        subtasks = taskset_res.subtasks
        taskset_id = taskset_res.taskset_id
        consumer = increment_counter.get_consumer()
        for subtask in subtasks:
            m = consumer.fetch().payload
            self.assertDictContainsSubset({"taskset": taskset_id,
                                           "task": increment_counter.name,
                                           "id": subtask.id}, m)
            increment_counter(
                    increment_by=m.get("kwargs", {}).get("increment_by"))
        self.assertEqual(increment_counter.count, sum(xrange(1, 10)))

    def test_named_taskset(self):
        prefix = "test_named_taskset-"
        ts = task.TaskSet([return_True_task.subtask([1])])
        res = ts.apply(taskset_id=prefix + uuid())
        self.assertTrue(res.taskset_id.startswith(prefix))


class TestTaskApply(Case):

    def test_apply_throw(self):
        with self.assertRaises(KeyError):
            raising.apply(throw=True)

    def test_apply_with_CELERY_EAGER_PROPAGATES_EXCEPTIONS(self):
        raising.app.conf.CELERY_EAGER_PROPAGATES_EXCEPTIONS = True
        try:
            with self.assertRaises(KeyError):
                raising.apply()
        finally:
            raising.app.conf.CELERY_EAGER_PROPAGATES_EXCEPTIONS = False

    def test_apply(self):
        increment_counter.count = 0

        e = increment_counter.apply()
        self.assertIsInstance(e, EagerResult)
        self.assertEqual(e.get(), 1)

        e = increment_counter.apply(args=[1])
        self.assertEqual(e.get(), 2)

        e = increment_counter.apply(kwargs={"increment_by": 4})
        self.assertEqual(e.get(), 6)

        self.assertTrue(e.successful())
        self.assertTrue(e.ready())
        self.assertTrue(repr(e).startswith("<EagerResult:"))

        f = raising.apply()
        self.assertTrue(f.ready())
        self.assertFalse(f.successful())
        self.assertTrue(f.traceback)
        with self.assertRaises(KeyError):
            f.get()


@task.periodic_task(run_every=timedelta(hours=1))
def my_periodic():
    pass


class TestPeriodicTask(Case):

    def test_must_have_run_every(self):
        with self.assertRaises(NotImplementedError):
            type("Foo", (task.PeriodicTask, ), {"__module__": __name__})

    def test_remaining_estimate(self):
        self.assertIsInstance(
            my_periodic.run_every.remaining_estimate(datetime.utcnow()),
            timedelta)

    def test_is_due_not_due(self):
        due, remaining = my_periodic.run_every.is_due(datetime.utcnow())
        self.assertFalse(due)
        # This assertion may fail if executed in the
        # first minute of an hour, thus 59 instead of 60
        self.assertGreater(remaining, 59)

    def test_is_due(self):
        p = my_periodic
        due, remaining = p.run_every.is_due(
                datetime.utcnow() - p.run_every.run_every)
        self.assertTrue(due)
        self.assertEqual(remaining,
                         timedelta_seconds(p.run_every.run_every))

    def test_schedule_repr(self):
        p = my_periodic
        self.assertTrue(repr(p.run_every))


@task.periodic_task(run_every=crontab())
def every_minute():
    pass


@task.periodic_task(run_every=crontab(minute="*/15"))
def quarterly():
    pass


@task.periodic_task(run_every=crontab(minute=30))
def hourly():
    pass


@task.periodic_task(run_every=crontab(hour=7, minute=30))
def daily():
    pass


@task.periodic_task(run_every=crontab(hour=7, minute=30,
                                      day_of_week="thursday"))
def weekly():
    pass


def patch_crontab_nowfun(cls, retval):

    def create_patcher(fun):

        @wraps(fun)
        def __inner(*args, **kwargs):
            prev_nowfun = cls.run_every.nowfun
            cls.run_every.nowfun = lambda: retval
            try:
                return fun(*args, **kwargs)
            finally:
                cls.run_every.nowfun = prev_nowfun

        return __inner

    return create_patcher


class test_crontab_parser(Case):

    def test_parse_star(self):
        self.assertEqual(crontab_parser(24).parse('*'), set(range(24)))
        self.assertEqual(crontab_parser(60).parse('*'), set(range(60)))
        self.assertEqual(crontab_parser(7).parse('*'), set(range(7)))

    def test_parse_range(self):
        self.assertEqual(crontab_parser(60).parse('1-10'),
                          set(range(1, 10 + 1)))
        self.assertEqual(crontab_parser(24).parse('0-20'),
                          set(range(0, 20 + 1)))
        self.assertEqual(crontab_parser().parse('2-10'),
                          set(range(2, 10 + 1)))

    def test_parse_groups(self):
        self.assertEqual(crontab_parser().parse('1,2,3,4'),
                          set([1, 2, 3, 4]))
        self.assertEqual(crontab_parser().parse('0,15,30,45'),
                          set([0, 15, 30, 45]))

    def test_parse_steps(self):
        self.assertEqual(crontab_parser(8).parse('*/2'),
                          set([0, 2, 4, 6]))
        self.assertEqual(crontab_parser().parse('*/2'),
                          set(i * 2 for i in xrange(30)))
        self.assertEqual(crontab_parser().parse('*/3'),
                          set(i * 3 for i in xrange(20)))

    def test_parse_composite(self):
        self.assertEqual(crontab_parser(8).parse('*/2'), set([0, 2, 4, 6]))
        self.assertEqual(crontab_parser().parse('2-9/5'), set([2, 7]))
        self.assertEqual(crontab_parser().parse('2-10/5'), set([2, 7]))
        self.assertEqual(crontab_parser().parse('2-11/5,3'), set([2, 3, 7]))
        self.assertEqual(crontab_parser().parse('2-4/3,*/5,0-21/4'),
                set([0, 2, 4, 5, 8, 10, 12, 15, 16,
                     20, 25, 30, 35, 40, 45, 50, 55]))
        self.assertEqual(crontab_parser().parse('1-9/2'),
                set([1, 3, 5, 7, 9]))

    def test_parse_errors_on_empty_string(self):
        with self.assertRaises(ParseException):
            crontab_parser(60).parse('')

    def test_parse_errors_on_empty_group(self):
        with self.assertRaises(ParseException):
            crontab_parser(60).parse('1,,2')

    def test_parse_errors_on_empty_steps(self):
        with self.assertRaises(ParseException):
            crontab_parser(60).parse('*/')

    def test_parse_errors_on_negative_number(self):
        with self.assertRaises(ParseException):
            crontab_parser(60).parse('-20')

    def test_expand_cronspec_eats_iterables(self):
        self.assertEqual(crontab._expand_cronspec(iter([1, 2, 3]), 100),
                         set([1, 2, 3]))

    def test_expand_cronspec_invalid_type(self):
        with self.assertRaises(TypeError):
            crontab._expand_cronspec(object(), 100)

    def test_repr(self):
        self.assertIn("*", repr(crontab("*")))

    def test_eq(self):
        self.assertEqual(crontab(day_of_week="1, 2"),
                         crontab(day_of_week="1-2"))
        self.assertEqual(crontab(minute="1", hour="2", day_of_week="5"),
                         crontab(minute="1", hour="2", day_of_week="5"))
        self.assertNotEqual(crontab(minute="1"), crontab(minute="2"))
        self.assertFalse(object() == crontab(minute="1"))
        self.assertFalse(crontab(minute="1") == object())


class test_crontab_remaining_estimate(Case):

    def next_ocurrance(self, crontab, now):
        crontab.nowfun = lambda: now
        return now + crontab.remaining_estimate(now)

    def test_next_minute(self):
        next = self.next_ocurrance(crontab(),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 11, 14, 31))

    def test_not_next_minute(self):
        next = self.next_ocurrance(crontab(),
                                   datetime(2010, 9, 11, 14, 59, 15))
        self.assertEqual(next, datetime(2010, 9, 11, 15, 0))

    def test_this_hour(self):
        next = self.next_ocurrance(crontab(minute=[5, 42]),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 11, 14, 42))

    def test_not_this_hour(self):
        next = self.next_ocurrance(crontab(minute=[5, 10, 15]),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 11, 15, 5))

    def test_today(self):
        next = self.next_ocurrance(crontab(minute=[5, 42], hour=[12, 17]),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 11, 17, 5))

    def test_not_today(self):
        next = self.next_ocurrance(crontab(minute=[5, 42], hour=[12]),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 12, 12, 5))

    def test_weekday(self):
        next = self.next_ocurrance(crontab(minute=30,
                                           hour=14,
                                           day_of_week="sat"),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 18, 14, 30))

    def test_not_weekday(self):
        next = self.next_ocurrance(crontab(minute=[5, 42],
                                           day_of_week="mon-fri"),
                                   datetime(2010, 9, 11, 14, 30, 15))
        self.assertEqual(next, datetime(2010, 9, 13, 0, 5))


class test_crontab_is_due(Case):

    def setUp(self):
        self.now = datetime.utcnow()
        self.next_minute = 60 - self.now.second - 1e-6 * self.now.microsecond

    def test_default_crontab_spec(self):
        c = crontab()
        self.assertEqual(c.minute, set(range(60)))
        self.assertEqual(c.hour, set(range(24)))
        self.assertEqual(c.day_of_week, set(range(7)))

    def test_simple_crontab_spec(self):
        c = crontab(minute=30)
        self.assertEqual(c.minute, set([30]))
        self.assertEqual(c.hour, set(range(24)))
        self.assertEqual(c.day_of_week, set(range(7)))

    def test_crontab_spec_minute_formats(self):
        c = crontab(minute=30)
        self.assertEqual(c.minute, set([30]))
        c = crontab(minute='30')
        self.assertEqual(c.minute, set([30]))
        c = crontab(minute=(30, 40, 50))
        self.assertEqual(c.minute, set([30, 40, 50]))
        c = crontab(minute=set([30, 40, 50]))
        self.assertEqual(c.minute, set([30, 40, 50]))

    def test_crontab_spec_invalid_minute(self):
        with self.assertRaises(ValueError):
            crontab(minute=60)
        with self.assertRaises(ValueError):
            crontab(minute='0-100')

    def test_crontab_spec_hour_formats(self):
        c = crontab(hour=6)
        self.assertEqual(c.hour, set([6]))
        c = crontab(hour='5')
        self.assertEqual(c.hour, set([5]))
        c = crontab(hour=(4, 8, 12))
        self.assertEqual(c.hour, set([4, 8, 12]))

    def test_crontab_spec_invalid_hour(self):
        with self.assertRaises(ValueError):
            crontab(hour=24)
        with self.assertRaises(ValueError):
            crontab(hour='0-30')

    def test_crontab_spec_dow_formats(self):
        c = crontab(day_of_week=5)
        self.assertEqual(c.day_of_week, set([5]))
        c = crontab(day_of_week='5')
        self.assertEqual(c.day_of_week, set([5]))
        c = crontab(day_of_week='fri')
        self.assertEqual(c.day_of_week, set([5]))
        c = crontab(day_of_week='tuesday,sunday,fri')
        self.assertEqual(c.day_of_week, set([0, 2, 5]))
        c = crontab(day_of_week='mon-fri')
        self.assertEqual(c.day_of_week, set([1, 2, 3, 4, 5]))
        c = crontab(day_of_week='*/2')
        self.assertEqual(c.day_of_week, set([0, 2, 4, 6]))

    def seconds_almost_equal(self, a, b, precision):
        for index, skew in enumerate((+0.1, 0, -0.1)):
            try:
                self.assertAlmostEqual(a, b + skew, precision)
            except AssertionError:
                if index + 1 >= 3:
                    raise
            else:
                break

    def test_crontab_spec_invalid_dow(self):
        with self.assertRaises(ValueError):
            crontab(day_of_week='fooday-barday')
        with self.assertRaises(ValueError):
            crontab(day_of_week='1,4,foo')
        with self.assertRaises(ValueError):
            crontab(day_of_week='7')
        with self.assertRaises(ValueError):
            crontab(day_of_week='12')

    def test_every_minute_execution_is_due(self):
        last_ran = self.now - timedelta(seconds=61)
        due, remaining = every_minute.run_every.is_due(last_ran)
        self.assertTrue(due)
        self.seconds_almost_equal(remaining, self.next_minute, 1)

    def test_every_minute_execution_is_not_due(self):
        last_ran = self.now - timedelta(seconds=self.now.second)
        due, remaining = every_minute.run_every.is_due(last_ran)
        self.assertFalse(due)
        self.seconds_almost_equal(remaining, self.next_minute, 1)

    # 29th of May 2010 is a saturday
    @patch_crontab_nowfun(hourly, datetime(2010, 5, 29, 10, 30))
    def test_execution_is_due_on_saturday(self):
        last_ran = self.now - timedelta(seconds=61)
        due, remaining = every_minute.run_every.is_due(last_ran)
        self.assertTrue(due)
        self.seconds_almost_equal(remaining, self.next_minute, 1)

    # 30th of May 2010 is a sunday
    @patch_crontab_nowfun(hourly, datetime(2010, 5, 30, 10, 30))
    def test_execution_is_due_on_sunday(self):
        last_ran = self.now - timedelta(seconds=61)
        due, remaining = every_minute.run_every.is_due(last_ran)
        self.assertTrue(due)
        self.seconds_almost_equal(remaining, self.next_minute, 1)

    # 31st of May 2010 is a monday
    @patch_crontab_nowfun(hourly, datetime(2010, 5, 31, 10, 30))
    def test_execution_is_due_on_monday(self):
        last_ran = self.now - timedelta(seconds=61)
        due, remaining = every_minute.run_every.is_due(last_ran)
        self.assertTrue(due)
        self.seconds_almost_equal(remaining, self.next_minute, 1)

    @patch_crontab_nowfun(hourly, datetime(2010, 5, 10, 10, 30))
    def test_every_hour_execution_is_due(self):
        due, remaining = hourly.run_every.is_due(
                datetime(2010, 5, 10, 6, 30))
        self.assertTrue(due)
        self.assertEqual(remaining, 60 * 60)

    @patch_crontab_nowfun(hourly, datetime(2010, 5, 10, 10, 29))
    def test_every_hour_execution_is_not_due(self):
        due, remaining = hourly.run_every.is_due(
                datetime(2010, 5, 10, 9, 30))
        self.assertFalse(due)
        self.assertEqual(remaining, 60)

    @patch_crontab_nowfun(quarterly, datetime(2010, 5, 10, 10, 15))
    def test_first_quarter_execution_is_due(self):
        due, remaining = quarterly.run_every.is_due(
                            datetime(2010, 5, 10, 6, 30))
        self.assertTrue(due)
        self.assertEqual(remaining, 15 * 60)

    @patch_crontab_nowfun(quarterly, datetime(2010, 5, 10, 10, 30))
    def test_second_quarter_execution_is_due(self):
        due, remaining = quarterly.run_every.is_due(
                            datetime(2010, 5, 10, 6, 30))
        self.assertTrue(due)
        self.assertEqual(remaining, 15 * 60)

    @patch_crontab_nowfun(quarterly, datetime(2010, 5, 10, 10, 14))
    def test_first_quarter_execution_is_not_due(self):
        due, remaining = quarterly.run_every.is_due(
                            datetime(2010, 5, 10, 10, 0))
        self.assertFalse(due)
        self.assertEqual(remaining, 60)

    @patch_crontab_nowfun(quarterly, datetime(2010, 5, 10, 10, 29))
    def test_second_quarter_execution_is_not_due(self):
        due, remaining = quarterly.run_every.is_due(
                            datetime(2010, 5, 10, 10, 15))
        self.assertFalse(due)
        self.assertEqual(remaining, 60)

    @patch_crontab_nowfun(daily, datetime(2010, 5, 10, 7, 30))
    def test_daily_execution_is_due(self):
        due, remaining = daily.run_every.is_due(
                datetime(2010, 5, 9, 7, 30))
        self.assertTrue(due)
        self.assertEqual(remaining, 24 * 60 * 60)

    @patch_crontab_nowfun(daily, datetime(2010, 5, 10, 10, 30))
    def test_daily_execution_is_not_due(self):
        due, remaining = daily.run_every.is_due(
                datetime(2010, 5, 10, 7, 30))
        self.assertFalse(due)
        self.assertEqual(remaining, 21 * 60 * 60)

    @patch_crontab_nowfun(weekly, datetime(2010, 5, 6, 7, 30))
    def test_weekly_execution_is_due(self):
        due, remaining = weekly.run_every.is_due(
                datetime(2010, 4, 30, 7, 30))
        self.assertTrue(due)
        self.assertEqual(remaining, 7 * 24 * 60 * 60)

    @patch_crontab_nowfun(weekly, datetime(2010, 5, 7, 10, 30))
    def test_weekly_execution_is_not_due(self):
        due, remaining = weekly.run_every.is_due(
                datetime(2010, 5, 6, 7, 30))
        self.assertFalse(due)
        self.assertEqual(remaining, 6 * 24 * 60 * 60 - 3 * 60 * 60)
