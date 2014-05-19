"""
End to end test of event exports.
"""

import gzip
import os
import logging
import tempfile
import textwrap
import time

from luigi.s3 import S3Client, S3Target

from edx.analytics.tasks.tests.acceptance import AcceptanceTestCase
from edx.analytics.tasks.url import url_path_join


log = logging.getLogger(__name__)


class EventExportAcceptanceTest(AcceptanceTestCase):
    """Validate data flow for bulk export of events for research purposes."""

    INPUT_FILE = 'event_export_tracking.log'
    PROD_SERVER_NAME = 'prod-edxapp-001'
    EDGE_SERVER_NAME = 'prod-edge-edxapp-002'
    NUM_REDUCERS = 1

    def setUp(self):
        super(EventExportAcceptanceTest, self).setUp()

        # The name of an existing job flow to run the test on
        assert('job_flow_name' in self.config)
        # The git URL of the repository to checkout analytics-tasks from.
        assert('tasks_repo' in self.config)
        # The branch of the analytics-tasks repository to test. Note this can differ from the branch that is currently
        # checked out and running this code.
        assert('tasks_branch' in self.config)
        # Where to store logs generated by analytics-tasks.
        assert('tasks_log_path' in self.config)
        # The user to connect to the job flow over SSH with.
        assert('connection_user' in self.config)
        # Where analytics-tasks should output data, should be a URL pointing to a directory.
        assert('tasks_output_url' in self.config)
        # Allow for parallel execution of the test by specifying a different identifier. Using an identical identifier
        # allows for old virtualenvs to be reused etc, which is why a random one is not simply generated with each run.
        assert('identifier' in self.config)

        url = self.config['tasks_output_url']
        identifier = self.config['identifier']

        self.s3_client = S3Client()

        self.test_root = url_path_join(url, identifier, 'event_export')

        self.s3_client.remove(self.test_root, recursive=True)

        self.test_src = url_path_join(self.test_root, 'src')
        self.test_out = url_path_join(self.test_root, 'out')
        self.test_config = url_path_join(self.test_root, 'config', 'default.yaml')

        self.input_paths = {
            'prod': url_path_join(self.test_src, self.PROD_SERVER_NAME, 'tracking.log-20140515.gz'),
            'edge': url_path_join(self.test_src, self.EDGE_SERVER_NAME, 'tracking.log-20140516-12345456.gz')
        }

        self.upload_data()
        self.write_config()

    def upload_data(self):
        src = os.path.join(self.data_dir, 'input', self.INPUT_FILE)

        with tempfile.NamedTemporaryFile(suffix='.gz') as temp_file:
            gzip_file = gzip.open(temp_file.name, 'wb')
            try:
                with open(src, 'r') as input_file:
                    for line in input_file:
                        gzip_file.write(line)
            finally:
                gzip_file.close()

            temp_file.flush()

            # Upload test data file
            for dst in self.input_paths.values():
                self.s3_client.put(temp_file.name, dst)

    def write_config(self):
        with S3Target(self.test_config).open('w') as target_file:
            target_file.write(
                textwrap.dedent(
                    """
                    ---
                    environments:
                      prod:
                        servers:
                          - {server_1}
                      edge:
                        servers:
                          - {server_2}
                    organizations:
                      edX:
                        recipient: automation@example.com
                      AcceptanceX:
                        recipient: automation@example.com
                    """
                    .format(
                        server_1=self.PROD_SERVER_NAME,
                        server_2=self.EDGE_SERVER_NAME
                    )
                )
            )

    def test_event_log_exports_using_manifest(self):
        with tempfile.NamedTemporaryFile() as temp_config_file:
            temp_config_file.write(
                textwrap.dedent(
                    """
                    [manifest]
                    threshold = 1
                    """
                )
            )
            temp_config_file.flush()

            self.launch_task(config=temp_config_file.name)
            self.validate_output()

    def launch_task(self, config=None):
        command = [
            os.getenv('REMOTE_TASK'),
            '--job-flow-name', self.config.get('job_flow_name'),
            '--branch', self.config.get('tasks_branch'),
            '--repo', self.config.get('tasks_repo'),
            '--remote-name', self.config.get('identifier'),
            '--wait',
            '--log-path', self.config.get('tasks_log_path'),
            '--user', self.config.get('connection_user'),
        ]

        if config:
            command.extend(['--override-config', config])

        command.extend(
            [
                'EventExportTask',
                '--local-scheduler',
                '--source', self.test_src,
                '--output-root', self.test_out,
                '--delete-output-root',
                '--config', self.test_config,
                '--environment', 'prod',
                '--environment', 'edge',
                '--interval', '2014-05',
                '--n-reduce-tasks', str(self.NUM_REDUCERS),
            ]
        )

        self.call_subprocess(command)

    def validate_output(self):
        # TODO: a lot of duplication here
        comparisons = [
            ('2014-05-15_edX.log', url_path_join(self.test_out, 'edX', self.PROD_SERVER_NAME, '2014-05-15_edX.log')),
            ('2014-05-16_edX.log', url_path_join(self.test_out, 'edX', self.PROD_SERVER_NAME, '2014-05-16_edX.log')),
            ('2014-05-15_edX.log', url_path_join(self.test_out, 'edX', self.EDGE_SERVER_NAME, '2014-05-15_edX.log')),
            ('2014-05-16_edX.log', url_path_join(self.test_out, 'edX', self.EDGE_SERVER_NAME, '2014-05-16_edX.log')),
            ('2014-05-15_AcceptanceX.log', url_path_join(self.test_out, 'AcceptanceX', self.EDGE_SERVER_NAME, '2014-05-15_AcceptanceX.log')),
            ('2014-05-15_AcceptanceX.log', url_path_join(self.test_out, 'AcceptanceX', self.PROD_SERVER_NAME, '2014-05-15_AcceptanceX.log')),
        ]

        for local_file_name, remote_url in comparisons:
            with open(os.path.join(self.data_dir, 'output', local_file_name), 'r') as local_file:
                remote_target = S3Target(remote_url)

                # Files won't appear in S3 instantaneously, wait for the files to appear.
                # TODO: exponential backoff
                found = False
                for _i in range(30):
                    if remote_target.exists():
                        found = True
                        break
                    else:
                        time.sleep(2)

                if not found:
                    self.fail('Unable to find expected output file {0}'.format(remote_url))

                with remote_target.open('r') as remote_file:
                    local_contents = local_file.read()
                    remote_contents = remote_file.read()

                    self.assertEquals(local_contents, remote_contents)
