import boto3
import json
import logging
import os
import random
import time
import unittest
import uuid

from copy import deepcopy
from unittest.mock import Mock, MagicMock, patch


logging.getLogger('botocore').setLevel(logging.WARNING)

os.environ["STAGE"] = "test"
os.environ["autotest"] = "True"

from sosw.components import dynamo_db
from sosw.labourer import Labourer
from sosw.managers.task import TaskManager
from sosw.test.variables import TEST_TASK_CLIENT_CONFIG
from sosw.test.helpers_test import extract_call_params


class task_manager_UnitTestCase(unittest.TestCase):
    TEST_CONFIG = TEST_TASK_CLIENT_CONFIG

    LABOURER = Labourer(id='some_function', arn='arn:aws:lambda:us-west-2:000000000000:function:some_function')


    def setUp(self):
        """
        We keep copies of main parameters here, because they may differ from test to test and cleanup needs them.
        This is responsibility of the test author to update these values if required from test.
        """

        self.patcher = patch("sosw.app.get_config")
        self.get_config_patch = self.patcher.start()

        self.config = deepcopy(self.TEST_CONFIG)

        self.labourer = deepcopy(self.LABOURER)

        self.HASH_KEY = ('task_id', 'S')
        self.RANGE_KEY = ('labourer_id', 'S')
        self.table_name = self.config['dynamo_db_config']['table_name']

        with patch('boto3.client'):
            self.manager = TaskManager(custom_config=self.config)

        self.manager.dynamo_db_client = MagicMock(spec=dynamo_db.DynamoDbClient)
        self.manager.ecology_client = MagicMock()
        self.manager.ecology_client.get_labourer_status.return_value = 2
        self.manager.lambda_client = MagicMock()


    def tearDown(self):
        self.patcher.stop()


    def test_get_db_field_name(self):
        self.assertEqual(self.manager.get_db_field_name('task_id'), self.HASH_KEY[0], "Configured field name failed")
        self.assertEqual(self.manager.get_db_field_name('some_name'), 'some_name', "Default column name failed")


    # Need to patch time in order to avoid accidental failures.
    @patch('time.time', MagicMock(return_value=455533200.0))
    def test_mark_task_invoked__calls_dynamo(self):
        self.manager.get_labourers = MagicMock(return_value=[self.labourer])
        self.manager.register_labourers()

        greenfield = round(time.time() - random.randint(0, 1000))
        delta = self.manager.config['greenfield_invocation_delta']

        task = {
            self.HASH_KEY[0]:  f"task_id_{self.labourer.id}_256",  # Task ID
            self.RANGE_KEY[0]: self.labourer.id,  # Worker ID
            'greenfield':      greenfield
        }

        # Do the actual tested job
        self.manager.mark_task_invoked(self.labourer, task)

        # Check the dynamo_client was called with correct payload to update
        self.manager.dynamo_db_client.update.assert_called_once()

        call_args, call_kwargs = self.manager.dynamo_db_client.update.call_args

        self.assertEqual(call_args[0],
                         {
                             self.HASH_KEY[0]: f"task_id_{self.labourer.id}_256"
                         }), "The key of task is missing"
        self.assertEqual(call_kwargs['attributes_to_increment'], {'attempts': 1}), "Attempts counter not increased"

        gf = call_kwargs['attributes_to_update']['greenfield']
        self.assertEqual(round(gf, -2), round(time.time() + delta, -2)), "Greenfield was not updated"


    def test_invoke_task__validates_task(self):
        self.assertRaises(AttributeError, self.manager.invoke_task, labourer=self.labourer), "Missing task and task_id"
        self.assertRaises(AttributeError, self.manager.invoke_task, labourer=self.labourer, task_id='qwe',
                          task={1: 2}), "Both task and task_id."


    def test_invoke_task__calls__mark_task_invoked(self):
        self.manager.mark_task_invoked = MagicMock()
        self.manager.is_valid_task = MagicMock(return_value=True)
        self.manager.get_task_by_id = MagicMock(return_value={})

        self.manager.invoke_task(task_id='qwe', labourer=self.labourer)
        self.manager.mark_task_invoked.assert_called_once()


    def test_invoke_task__calls__get_task_by_id(self):
        self.manager.is_valid_task = MagicMock(return_value=True)
        self.manager.mark_task_invoked = MagicMock()
        self.manager.get_task_by_id = MagicMock(return_value={})

        self.manager.invoke_task(task_id='qwe', labourer=self.labourer)
        self.manager.is_valid_task.assert_called_once()
        self.manager.get_task_by_id.assert_called_once()


    def test_invoke_task__calls__lambda_client(self):
        self.manager.is_valid_task = MagicMock(return_value=True)
        self.manager.get_labourers = MagicMock(return_value=[self.labourer])
        self.manager.register_labourers()

        task = {
            self.HASH_KEY[0]:  f"task_id_{self.labourer.id}_256",  # Task ID
            self.RANGE_KEY[0]: self.labourer.id,  # Worker ID
            'payload':         {'foo': 23}
        }

        self.manager.get_task_by_id = MagicMock(return_value=task)

        self.manager.invoke_task(task_id=f'task_id_{self.labourer}_256', labourer=self.labourer)

        self.manager.lambda_client.invoke.assert_called_once()

        call_args, call_kwargs = self.manager.lambda_client.invoke.call_args

        self.assertEqual(call_kwargs['FunctionName'], self.labourer.arn)
        # self.assertEqual(call_kwargs['Payload'], json.dumps(task['payload']))


    def test_invoke_task__not_calls__lambda_client_if_raised_conditional_exception(self):
        self.manager.register_labourers()

        task = {
            self.HASH_KEY[0]:  f"task_id_{self.labourer.id}_256",  # Task ID
            self.RANGE_KEY[0]: self.labourer.id,  # Worker ID
            'created_at':      1000,
            'payload':         {'foo': 23}
        }


        class ConditionalCheckFailedException(Exception):
            pass


        self.manager.get_task_by_id = MagicMock(return_value=task)
        self.manager.mark_task_invoked = MagicMock()
        self.manager.mark_task_invoked.side_effect = ConditionalCheckFailedException("Boom")

        self.manager.invoke_task(task_id=f'task_id_{self.labourer}_256', labourer=self.labourer)

        self.manager.lambda_client.invoke.assert_not_called()
        self.assertEqual(self.manager.stats['concurrent_task_invocations_skipped'], 1)


    def test_invoke_task__with_explicit_task__not_calls_get_task_by_id(self):
        self.manager.get_task_by_id = MagicMock()
        self.manager.is_valid_task = MagicMock(return_value=True)
        self.manager.mark_task_invoked = MagicMock()

        self.manager.invoke_task(labourer=self.LABOURER, task={1:2})
        self.manager.is_valid_task.assert_called_once()
        self.manager.get_task_by_id.assert_not_called()


    def test_register_labourers(self):
        with patch('time.time') as t:
            t.return_value = 123

            labourers = self.manager.register_labourers()

        lab = labourers[0]
        invoke_time = 123 + self.manager.config['greenfield_invocation_delta']

        self.assertEqual(lab.get_attr('start'), 123)
        self.assertEqual(lab.get_attr('invoked'), invoke_time)
        self.assertEqual(lab.get_attr('expired'), invoke_time - lab.duration - lab.cooldown)
        self.assertEqual(lab.get_attr('health'), 2)
        self.assertEqual(lab.get_attr('max_attempts'), 3)


    def test_register_labourers__calls_register_task_manager(self):

        self.manager.register_labourers()
        self.manager.ecology_client.register_task_manager.assert_called_once_with(self.manager)


    def test_get_count_of_running_tasks_for_labourer(self):

        labourer = self.manager.register_labourers()[0]
        self.manager.dynamo_db_client.get_by_query.return_value = 3

        self.assertEqual(self.manager.get_count_of_running_tasks_for_labourer(labourer=labourer), 3)
        self.manager.dynamo_db_client.get_by_query.assert_called_once()

        call_args, call_kwargs = self.manager.dynamo_db_client.get_by_query.call_args
        self.assertTrue(call_kwargs['return_count'])


    def test_get_labourers(self):
        self.config['labourers'] = {
            'some_lambda':  {'foo': 'bar', 'arn': '123'},
            'some_lambda2': {'foo': 'baz'},
        }

        with patch('boto3.client'):
            self.task_client = TaskManager(custom_config=self.config)

        result = self.task_client.get_labourers()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].foo, 'bar')
        self.assertEqual(result[0].arn, '123')
        self.assertEqual(result[1].foo, 'baz')


    def test_archive_task(self):
        task_id = '918273'
        task = {
            'labourer_id': 'some_lambda', 'task_id': task_id, 'payload': '{}', 'completed_at': '1551962375',
            'closed_at':   '111'
        }

        # Mock
        self.manager.dynamo_db_client = MagicMock()
        self.manager.get_task_by_id = Mock(return_value=task)

        # Call
        self.manager.archive_task(task_id)

        # Check calls
        expected_completed_task = task.copy()
        expected_completed_task['labourer_id_task_status'] = 'some_lambda_1'
        self.manager.dynamo_db_client.put.assert_called_once_with(expected_completed_task, table_name=self.TEST_CONFIG[
            'sosw_closed_tasks_table'])
        self.manager.dynamo_db_client.delete.assert_called_once_with({'task_id': task_id})


    def test__jsonify_payload_of_task(self):
        TESTS = [
            ({'foo': 'some_lambda', 'payload': '{"bar": 42}'}, {'foo': 'some_lambda', 'payload': '{"bar": 42}'}),
            ({'foo': 'some_lambda', 'payload': {'bar': 42}}, {'foo': 'some_lambda', 'payload': '{"bar": 42}'}),
            ({'foo': {'a': 1}}, {'foo': {'a': 1}}),
        ]

        for test, expected in TESTS:
            self.assertEqual(self.manager._jsonify_payload_of_task(test), expected)


    def test_move_task_to_retry_table(self):
        task_id = '123'
        TEST = {'labourer_id': 'some_lambda', 'task_id': task_id, 'payload': '{"bar": 42}'}
        delay = 350


        with patch('time.time') as t:
            t.return_value = 1000
            self.manager.move_task_to_retry_table(TEST, delay)

        params = extract_call_params(self.manager.dynamo_db_client.put.call_args, dynamo_db.DynamoDbClient.put)
        # print(params)

        desired_time = params['row'].pop('desired_launch_time')
        self.assertEqual(desired_time, 1000 + delay, "Delay was not added to the current time.")

        self.assertDictEqual(TEST, params['row'], "Task for retry table doesn't match original.")
        self.assertEqual(params['table_name'], self.config['sosw_retry_tasks_table'], "Retry writes to invalid table.")


    def test_move_task_to_retry_table__dumps_payload(self):
        TEST = {'labourer_id': 'foo', 'task_id': 123, 'payload': {'bar': 42}}

        self.manager.move_task_to_retry_table(TEST, 1)

        params = extract_call_params(self.manager.dynamo_db_client.put.call_args, dynamo_db.DynamoDbClient.put)

        self.assertEqual(json.dumps(TEST['payload']), params['row']['payload'], "Payload was JSON-nified")


    def test_get_tasks_to_retry_for_labourer(self):

        with patch('time.time') as t:
            t.return_value = 123
            labourer = self.manager.register_labourers()[0]

        TASK = {'labourer_id': 'some_lambda', 'task_id': str(uuid.uuid4()), 'greenfield': 122}

        # Requires Labourer
        self.assertRaises(TypeError, self.manager.get_tasks_to_retry_for_labourer)

        self.manager.dynamo_db_client.get_by_query.return_value = [TASK]

        r = self.manager.get_tasks_to_retry_for_labourer(labourer=labourer)

        self.manager.dynamo_db_client.get_by_query.assert_called_once()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]['task_id'], TASK['task_id'])


    def test_get_tasks_to_retry_for_labourer__respects_greenfield(self):

        with patch('time.time') as t:
            t.return_value = 123
            labourer = self.manager.register_labourers()[0]

        self.manager.get_tasks_to_retry_for_labourer(labourer=labourer, limit=1)

        call_args, call_kwargs = self.manager.dynamo_db_client.get_by_query.call_args
        self.assertEqual(call_kwargs['keys']['desired_launch_time'], '123')
        self.assertEqual(call_kwargs['comparisons']['desired_launch_time'], '<=')


    def test_get_tasks_to_retry_for_labourer__limit(self):

        with patch('time.time') as t:
            t.return_value = 123
            labourer = self.manager.register_labourers()[0]

        TASK = {'labourer_id': 'some_lambda', 'task_id': str(uuid.uuid4()), 'greenfield': 122}
        mock_get_by_query = lambda **kwargs: [TASK for _ in range(kwargs.get('max_items', 42))]

        self.manager.dynamo_db_client.get_by_query.side_effect = mock_get_by_query

        r = self.manager.get_tasks_to_retry_for_labourer(labourer=labourer, limit=1)

        self.manager.dynamo_db_client.get_by_query.assert_called_once()
        self.assertEqual(len(r), 1)


    def test_get_oldest_greenfield_for_labourer__no_queued_tasks(self):

        self.manager.dynamo_db_client.get_by_query.return_value = []

        result = self.manager.get_oldest_greenfield_for_labourer(labourer=self.LABOURER)

        self.assertEqual(result, 0 + self.manager.config['greenfield_task_step'])


    def test_get_newest_greenfield_for_labourer__no_queued_tasks(self):

        self.manager.dynamo_db_client.get_by_query.return_value = []

        result = self.manager.get_newest_greenfield_for_labourer(labourer=self.LABOURER)

        self.assertEqual(result, 0 + self.manager.config['greenfield_task_step'])


    def test_create_task(self):

        TASK = dict(labourer=self.LABOURER, payload={'foo': 42})
        self.manager.get_newest_greenfield_for_labourer = MagicMock(return_value=5000)

        with patch('time.time') as t:
            t.return_value = 1234567
            self.manager.create_task(**TASK)

        self.manager.dynamo_db_client.put.assert_called_once()

        call_args, call_kwargs = self.manager.dynamo_db_client.put.call_args
        arg = call_args[0]
        # print('########')
        # print(arg, call_kwargs)

        self.assertEqual(str(arg['labourer_id']), str(self.LABOURER.id))
        self.assertEqual(str(arg['created_at']), str(1234567))
        self.assertEqual(str(arg['greenfield']), str(6000))
        self.assertEqual(str(arg['attempts']), str(0))
        self.assertEqual(str(arg['payload']), '{"foo": 42}')

        for field in self.manager.config['dynamo_db_config']['required_fields']:
            self.assertIn(field, arg.keys())


    def test_create_task__combine_complex_payload(self):
        TASK = dict(labourer=self.LABOURER, payload={'foo': 42}, shops=[1, 3], lloyd='green ninja')
        self.manager.get_newest_greenfield_for_labourer = MagicMock(return_value=5000)

        self.manager.create_task(**TASK)

        self.manager.dynamo_db_client.put.assert_called_once()

        call_args, call_kwargs = self.manager.dynamo_db_client.put.call_args
        payload = call_args[0]['payload']
        payload = json.loads(payload)
        # print('########')
        # print(payload)

        self.assertEqual(payload['foo'], 42)
        self.assertEqual(payload['shops'], [1, 3])
        self.assertEqual(payload['lloyd'], 'green ninja')


    def test_construct_payload_for_task(self):
        TESTS = [
            (dict(payload={'foo': 42}), {'foo': 42}),  # Dictionary
            (dict(payload='{"foo": 42}'), {'foo': 42}),  # JSON
            (dict(payload={'foo': 42}, shops=[1, 3]), {'foo': 42, 'shops': [1, 3]}),  # Combine custom attrs
            (dict(bar="foo"), {'bar': "foo"}),  # Missing initial payload
            (dict(bar={"foo": 3}), {'bar': {"foo": 3}}),  # Missing initial payload
        ]

        for test, expected in TESTS:
            self.assertEqual(self.manager.construct_payload_for_task(**test), json.dumps(expected))


    def test_get_average_labourer_duration__calls_dynamo_twice(self):
        """
        This is am important test for other ones of this method.
        If for some reason the DynamoMock is called not twice, then the side_effects don't imitate
        real data and tests will be unpredictable.
        """

        some_labourer = self.manager.register_labourers()[0]

        self.manager.get_average_labourer_duration(some_labourer)
        self.assertEqual(self.manager.dynamo_db_client.get_by_query.call_count, 2)


    def test_get_average_labourer_duration__calculates_average(self):

        NOW = 10000
        START = NOW + self.manager.config['greenfield_invocation_delta']

        some_labourer = self.manager.register_labourers()[0]
        some_labourer.max_duration = 900

        CLOSED = [
            {
                'task_id':      '123', 'labourer_id': 'some_function', 'attempts': 1, 'greenfield': START - 1000,
                'completed_at': NOW - 500
            },  # Duration 500
            {
                'task_id':      '124', 'labourer_id': 'some_function', 'attempts': 1, 'greenfield': START - 2000,
                'completed_at': NOW - 1700
            },  # Duration 300
            {
                'task_id':      '125', 'labourer_id': 'some_function', 'attempts': 1, 'greenfield': START - 2000,
                'completed_at': NOW - 1700
            },  # Duration 300
        ]

        FAILED = [
            {'task_id': '235', 'labourer_id': 'some_function', 'attempts': 3, 'greenfield': START - 3000},
            {'task_id': '236', 'labourer_id': 'some_function', 'attempts': 4, 'greenfield': START - 3000},
            {'task_id': '237', 'labourer_id': 'some_function', 'attempts': 3, 'greenfield': START - 4000},

        ]

        self.manager.dynamo_db_client.get_by_query.side_effect = [CLOSED, FAILED]

        count_failed = sum(x['attempts'] for x in FAILED)

        expected = round((500 + 300 + 300 +  # closed
                          (some_labourer.get_attr('max_duration') * count_failed))  # failed
                         / (len(CLOSED) + count_failed))  # total number of closed + failed

        self.assertEqual(expected, self.manager.get_average_labourer_duration(some_labourer))


    def test_validate_task__good(self):
        TESTS = [
            ({'task_id': '235', 'labourer_id': 'foo', 'created_at': 5000, 'greenfield': 1000}, True),
            ({'task_id': 235, 'labourer_id': 'foo', 'created_at': 5000, 'greenfield': 1000}, True),
            ({'task_id': '235', 'labourer_id': 'foo', 'created_at': 5000, 'greenfield': 1000, 'bar': 42}, True),
        ]

        for test, expected in TESTS:
            self.assertEqual(self.manager.is_valid_task(test), expected)


    def test_validate_task__bad(self):
        _ = self.manager.get_db_field_name
        TASK = {'task_id': '235', 'labourer_id': 'foo', 'created_at': 5000, 'greenfield': 1000, 'bar': 42}

        for field in [_('task_id'), _('labourer_id'), _('created_at')]:
            test = deepcopy(TASK)
            test.pop(field)

            self.assertFalse(self.manager.is_valid_task(test))


    def test_health_metrics_received(self):
        TEST_CFG = {
            'some_function': {
                'arn':                          'arn:aws:lambda:us-west-2:0000000000:function:some_function',
                'max_simultaneous_invocations': 10,
                'health_metrics':               {
                    'SomeDBCPU': {
                        'Name':                        'CPUUtilization',
                        'Namespace':                   'AWS/RDS',
                        'Period':                      60,
                        'Statistics':                  ['Average'],
                        'Dimensions':                  [
                            {
                                'Name':  'DBInstanceIdentifier',
                                'Value': 'YOUR-DB'
                            },
                        ],

                        # These is the mapping of how the Labourer should "feel" about this metric.
                        # See EcologyManager.ECO_STATUSES.
                        # This is just a mapping ``ECO_STATUS: value`` using ``feeling_comparison_operator``.
                        'feelings':                    {
                            3: 50,
                            4: 25,
                        },
                        'feeling_comparison_operator': '<='
                    },
                },
            }
        }

