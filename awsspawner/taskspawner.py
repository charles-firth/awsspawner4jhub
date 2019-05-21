import logging
import os
import string
# from concurrent.futures import ThreadPoolExecutor
import socket
import boto3
import escapism
from jupyterhub.spawner import Spawner
from tornado import gen
from traitlets import (
    Integer,
    Unicode,
    Dict
)
from traitlets.config import LoggingConfigurable

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class EcsTaskSpawner(Spawner):
    """
    ECS Task Spawner
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        """ Creates and boots a new server to host the worker instance."""
        self.log.info("function create_new_instance %s" % self.user.name)
        self.ecs_client = boto3.client('ecs')
        self.ec2_client = boto3.client('ec2')

    # _executor = None

    strategy = Unicode(
        "ECSxEC2SpawnerHandler",
        config=True,
        help="""
        Indicates if the ECS Spawner mechanism must create an EC2 instance itself, or let ECS to choose one for us.
        """
    )

    strategy_parms = Dict(
        {},
        config=True,
        help="""
        Strategy parameters.
        """
    )

    ip = Unicode(
        '0.0.0.0',
        config=True,
        help="""
        The IP address (or hostname) the single-user server should listen on.
        """
    )

    # @property
    # def executor(self):
    #     """single global executor"""
    #     cls = self.__class__
    #     if cls._executor is None:
    #         cls._executor = ThreadPoolExecutor(1)
    #     return cls._executor

    def _get_spawner_handler(self):
        """
        Return the right handler based on the strategy
        :return SpawnerHandler: a handler
        """
        if self.strategy == 'ECSxEC2SpawnerHandler':
            return ECSxEC2SpawnerHandler(self, **self.strategy_parms)
        if self.strategy == 'ECSSpawnerHandler':
            return ECSSpawnerHandler(self, **self.strategy_parms)
        if self.strategy == 'EC2SpawnerHandler':
            return EC2SpawnerHandler(self, **self.strategy_parms)

        raise ValueError("Strategy not properly specified")

    @gen.coroutine
    def start(self):
        self.log.info("function start for user %s" % self.user.name)
        # handler = self._get_spawner_handler()

        # result = yield self.executor.submit(handler.start)

        return (yield self._get_spawner_handler().start())

    @gen.coroutine
    def stop(self, now=False):
        self.log.info("function stop called for %s" % self.user.name)

        return (yield self._get_spawner_handler().stop())

        self.clear_state()

    @gen.coroutine
    def poll(self):
        self.log.debug("function poll for user %s" % self.user.name)

        return (yield self._get_spawner_handler().poll())


class SpawnerHandler(LoggingConfigurable):
    """
    Generic Handler
    """

    def __init__(self, spawner, **kwargs):
        self.spawner = spawner
        self.user = spawner.user
        self.hub = spawner.hub
        self.ecs_client = spawner.ecs_client
        self.ec2_client = spawner.ec2_client

    def get_env(self):
        return self.spawner.get_env()

    @gen.coroutine
    def start(self):
        pass

    @gen.coroutine
    def stop(self):
        pass

    @gen.coroutine
    def poll(self):
        pass


class EC2SpawnerHandler(SpawnerHandler):
    """
        Using EC2
    """
    ec2_instance_template = Unicode(
        "",
        config=True,
        help="""
        Name of the EC2 Instance Template to be used when creaing a EC2 Instance.
        This property is used when ecs_task_on_ec2_instance is set to True.
        """
    )

    def __init__(self, spawner, ec2_instance_template, **kwargs):
        super().__init__(spawner, **kwargs)
        self.ec2_instance_template = ec2_instance_template

    @gen.coroutine
    def start(self):
        pass

    @gen.coroutine
    def stop(self):
        pass

    @gen.coroutine
    def poll(self):
        pass


class ECSSpawnerHandler(SpawnerHandler):
    """
        Using ECS Task:
    """
    ecs_task_definition = Unicode(
        "",
        config=True,
        help="""
            Name of the Task Definition to be used when running the task.
        """
    )

    def __init__(self, spawner, cluster_name, ecs_task_definition, **kwargs):
        super().__init__(spawner)
        self.cluster_name = cluster_name
        self.ecs_task_definition = ecs_task_definition

    @gen.coroutine
    def start(self):
        task = yield self.get_task()
        if task is None:
            ip_address = yield self._create_new_task()
            return ip_address, self.port
        raise ValueError('Not handled yet')

    @gen.coroutine
    def stop(self):
        task = yield self.get_task()

        # Only Stop the task
        self.ecs_client.stop_task(
            cluster=self.cluster_name,
            task=task['taskArn']
        )

    @gen.coroutine
    def poll(self):
        pass

    @gen.coroutine
    def get_task(self):
        tasks = self.ecs_client.list_tasks(
            cluster=self.cluster_name,
            startedBy=self._get_task_identifier(),
            desiredStatus='RUNNING'
        )
        if tasks and len(tasks['taskArns']) > 0:
            return self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=[
                    tasks['taskArns'][0]
                ]

            )['tasks'][0]
        else:
            return None

    def _get_task_identifier(self):
        """
        Return Task identifier
        :return:
        """
        return 'EcsTaskSpawner:' + self.user.name

    @gen.coroutine
    def _create_new_task(self):
        self.log.info("function create new task for user %s" % self.user.name)
        task_def_arn = yield self._get_task_definition()

        env = self.get_env()
        env['JPY_USER'] = self.user.name
        env['JPY_BASE_URL'] = self.user.server.base_url
        env['JPY_COOKIE_NAME'] = self.user.server.cookie_name

        container_env = self._expand_env(env)

        self.log.info("starting ecs task for user %s" % self.user.name)

        task = self.ecs_client.run_task(taskDefinition=task_def_arn,
                                        cluster=self.cluster_name,
                                        startedBy=self._get_task_identifier(),
                                        overrides={
                                            'containerOverrides': [
                                                {
                                                    'name': 'hello-world',
                                                    'environment': container_env
                                                }
                                            ]
                                        })['tasks'][0]

        waiter = self.ecs_client.get_waiter('tasks_running')
        waiter.wait(cluster=self.cluster_name, tasks=[task['taskArn']])

        self.log.info("ecs task up and running for %s" % self.user.name)

        raise ValueError("Still todo, get ip of the container")

    @gen.coroutine
    def _get_task_definition(self):
        """
        Return the Arn of the Task Definition to be used when creating the task
        :return:
        """
        self.log.info("function get task definition for user %s" % self.user.name)

        if self.ecs_task_definition != '':
            task_def = self.ecs_client.describe_task_definition(taskDefinition=self.ecs_task_definition)[
                'taskDefinition']
            return task_def['taskDefinitionArn']

        task_def = {
            'family': 'hello-world',
            'volumes': [],
            'containerDefinitions': [
                {
                    'memory': 1024,
                    'cpu': 0,
                    'essential': True,
                    'name': 'hello-world',
                    'image': 'jupyter/scipy-notebook:ae885c0a6226',
                    'portMappings': [
                        {
                            'containerPort': 8888,
                            'hostPort': 8888,
                            'protocol': 'tcp'
                        }
                    ],
                    'command': [
                        'start-notebook.sh',
                    ],
                }
            ]
        }

        response = self.ecs_client.register_task_definition(**task_def)
        task_def_arn = response['taskDefinition']['taskDefinitionArn']

        return task_def_arn

    def _expand_env(self, env):
        """
        Expand get_env to ECS task environment
        """
        result = []

        if env:
            for key in env.keys():
                entry = {
                    'name': key,
                    'value': env.get(key)
                }
                result.append(entry)

        return result

    def get_env(self):
        env = super().get_env()

        ip = socket.gethostbyname(socket.gethostname())

        env['JPY_HUB_API_URL'] = f'http://{os.environ.get("HUB_HOST_IP", ip)}:8081/jupyter/hub/api'
        env['JPY_HUB_PREFIX'] = self.hub.server.base_url

        env.update(dict(
            JPY_USER=self.user.name,
            JPY_COOKIE_NAME=self.user.server.cookie_name,
            JPY_BASE_URL=self.user.server.base_url,
            JPY_HUB_PREFIX=self.hub.server.base_url
        ))

        return env


class ECSxEC2SpawnerHandler(ECSSpawnerHandler):
    """
        Using single EC2 Instance for every ECS Task
    """
    ec2_instance_template = Unicode(
        "",
        config=True,
        help="""
        Name of the EC2 Instance Template to be used when creating a EC2 Instance
        """
    )

    ec2_instance_template_version = Unicode(
        "",
        config=True,
        help="""
        Version of the EC2 Instance Template to be used when creating a EC2 Instance
        """
    )

    port = Integer(
        8888,
        help="""
        Default port to 8888
        """
    )

    def __init__(self, spawner, ec2_instance_template=None,
                 ec2_instance_template_version='13',
                 port=8888, **kwargs):

        super().__init__(spawner, **kwargs)
        self.ec2_instance_template = ec2_instance_template
        vers = len(self.ec2_client.describe_launch_template_versions(LaunchTemplateName=self.ec2_instance_template))
        # Always use the latest version
        self.ec2_instance_template_version = vers
        if port:
            self.port = port

    @gen.coroutine
    def start(self):
        task = yield self.get_task()
        if task is None:
            ip_address = yield self._create_new_task()
            return ip_address, self.port
        # TODO

    @gen.coroutine
    def stop(self):
        task = yield self.get_task()
        if task:
            self.ecs_client.stop_task(cluster=self.cluster_name, task=task['taskArn'])
            # Stop the Instance Itself
            container_instance_arn = task['containerInstanceArn']
            container_instance = self.ecs_client.describe_container_instances(
                cluster=self.cluster_name,
                containerInstances=[
                    container_instance_arn
                ]
            )['containerInstances'][0]

            # TODO: Change this when having multiple users per instance
            self.ec2_client.stop_instances(InstanceIds=[container_instance['ec2InstanceId']])

            # self.ec2_client.terminate_instances(InstanceIds=[
            #     container_instance['ec2InstanceId']
            # ],
            #     DryRun=False
            # )

        else:
            self.log.info("No ECS task found to be stopped %s" % self.user.name)

    @gen.coroutine
    def poll(self):
        task = yield self.get_task()
        if task:
            return None  # Still running
        else:
            return 0

    @gen.coroutine
    def _create_new_task(self):
        self.log.info("function create new task for user %s" % self.user.name)
        task_def_arn = yield self._get_task_definition()

        selected_container_instance = yield self._get_user_instance()

        env = self.get_env()
        env['JPY_USER'] = self.user.name
        env['JPY_BASE_URL'] = self.user.server.base_url
        env['JPY_COOKIE_NAME'] = self.user.server.cookie_name

        container_env = self._expand_env(env)

        self.log.info("starting ecs task for user %s" % self.user.name)

        task = self.ecs_client.start_task(taskDefinition=task_def_arn,
                                          cluster=self.cluster_name,
                                          startedBy=self._get_task_identifier(),
                                          containerInstances=[selected_container_instance['containerInstanceArn']],
                                          overrides={
                                              'containerOverrides': [
                                                  {
                                                      'name': 'jupyter-instance',
                                                      'environment': container_env
                                                  }
                                              ]
                                          },
                                          )

        task = task['tasks'][0]

        waiter = self.ecs_client.get_waiter('tasks_running')
        waiter.wait(cluster=self.cluster_name, tasks=[task['taskArn']])

        self.log.info("ecs task up and running for %s" % self.user.name)

        return selected_container_instance['NetworkInterfaces'][0]['PrivateIpAddress']

    @gen.coroutine
    def _get_user_instance(self):
        self.log.info("function get user instance for user %s" % self.user.name)
        # For now - one instance per user. TODO: Multiple users per instance, based on instance resources.
        instance = yield self._get_container_instance()
        return instance or (yield self._create_instance())

    @gen.coroutine
    def _create_instance(self):
        self.log.info("function create instance for user %s" % self.user.name)
        environment_name = os.environ.get('HUB_ENVIRONMENT', 'OodleJupyterHub')
        ec2_name = environment_name + '-' + self.user.name

        instance = self.ec2_client.run_instances(
            MinCount=1,
            MaxCount=1,
            LaunchTemplate={
                'LaunchTemplateName': self.ec2_instance_template,
                'Version': self.ec2_instance_template_version
            },
            TagSpecifications=[
                {
                    'ResourceType': 'instance',
                    'Tags': [
                        {
                            'Key': 'Name',
                            'Value': ec2_name
                        },
                        {
                            'Key': 'Environment',
                            'Value': environment_name
                        },
                        {
                            'Key': 'Project',
                            'Value': os.environ.get('HUB_PROJECT', 'JupyterHUB-Project')
                        },
                    ]
                },
            ]
        )['Instances'][0]

        waiter = self.ec2_client.get_waiter('instance_status_ok')
        waiter.wait(InstanceIds=[instance['InstanceId']])

        instance = \
            self.ec2_client.describe_instances(InstanceIds=[instance['InstanceId']])['Reservations'][0]['Instances'][0]

        self.ec2_instance_info = instance
        return instance

    @gen.coroutine
    def _get_container_instance(self):
        """
        Look for container instance related to user name
        :return:
        """

        container_instances_arns = self.ecs_client.list_container_instances(cluster=self.cluster_name)[
            'containerInstanceArns']
        if len(container_instances_arns) < 1:
            return None
        container_instances = self.ecs_client.describe_container_instances(
            cluster=self.cluster_name,
            containerInstances=container_instances_arns)['containerInstances']

        for container_instance in container_instances:
            instance = self.ec2_client.describe_instances(
                InstanceIds=[container_instance['ec2InstanceId']])['Reservations'][0]['Instances'][0]

            if any(self.user.name in x['Value'] for x in instance['Tags']):
                instance_state = instance['State']['Name']
                if instance_state == "terminated":
                    continue
                self.log.info(f"found instance for user {self.user.name}\n{instance}")
                if not instance_state == 'running':
                    self.log.info(f"starting instance for user {self.user.name}\n{instance}")
                    self.ec2_client.start_instances(InstanceIds=[container_instance['ec2InstanceId']])
                return {**instance, **container_instance}

        return None

    def _expand_user_properties(self, template):
        # Make sure username and servername match the restrictions for DNS labels
        safe_chars = set(string.ascii_lowercase + string.digits)

        # Set servername based on whether named-server initialised
        servername = ''

        legacy_escaped_username = ''.join([s if s in safe_chars else '-' for s in self.user.name.lower()])
        safe_username = escapism.escape(self.user.name, safe=safe_chars, escape_char='-').lower()
        return template.format(
            userid=self.user.id,
            username=safe_username,
            legacy_escape_username=legacy_escaped_username,
            servername=servername
        )
