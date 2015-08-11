import pwd
import os
from tempfile import mkdtemp
from datetime import timedelta

from docker.errors import APIError

from dockerspawner import DockerSpawner
from textwrap import dedent
from traitlets import (
    Integer,
    Unicode,
)
from tornado import gen

from escapism import escape

import git


class CustomDockerSpawner(DockerSpawner):
    def __init__(self, **kwargs):
        super(CustomDockerSpawner, self).__init__(**kwargs)

    _git_executor = None
    @property
    def git_executor(self):
        """single global git executor"""
        cls = self.__class__
        if cls._git_executor is None:
            cls._git_executor = ThreadPoolExecutor(1)
        return cls._git_executor

    _git_client = None
    @property
    def git_client(self):
        """single global git client instance"""
        cls = self.__class__
        if cls._git_client is None:
            cls._git_client = git.Git()
        return cls._git_client

    def _git(self, method, *args, **kwargs):
        """wrapper for calling git methods
        
        to be passed to ThreadPoolExecutor
        """
        m = getattr(self.git_client, method)
        return m(*args, **kwargs)

    def git(self, method, *args, **kwargs):
        """Call a git method in a background thread
        
        returns a Future
        """
        return self.executor.submit(self._git, method, *args, **kwargs)

    #def get_state(self):
    #    return {}

    @property
    def repo_url(self):
        return self.user.last_repo_url

    _escaped_repo_url = None
    @property
    def escaped_repo_url(self):
        if self._escaped_repo_url is None:
            trans = str.maketrans(':/-.', "____")
            self._escaped_repo_url = self.repo_url.translate(trans)
        return self._escaped_repo_url

    @property
    def container_name(self):
        return "{}-{}".format(self.container_prefix,
                              self.escaped_name,
                              #self.escaped_repo_url,
                              #self.repo_sha
        )

    @gen.coroutine
    def get_container(self):
        if not self.container_id:
            return None

        self.log.debug("Getting container (%s)", self.container_id)
        try:
            container = yield self.docker(
                'inspect_container', self.container_id
            )
            self.container_id = container['Id']
        except APIError as e:
            if e.response.status_code == 404:
                self.log.info("Container '%s' is gone", self.container_id)
                container = None
                # my container is gone, forget my id
                self.container_id = ''
            else:
                raise
        return container

    @gen.coroutine
    def start(self, image=None):
        dockerfile_names = ['Dockerfile', '.nbrunnerdockerfile']
        """start the single-user server in a docker container"""
        tmp_dir = mkdtemp(suffix='everware')
        yield self.git('clone', self.repo_url, tmp_dir, '--depth=1')
        # is this blocking?
        # use the username, git repo URL and HEAD commit sha to derive
        # the image name
        repo = git.Repo(tmp_dir)
        self.repo_sha = repo.rev_parse("HEAD")

        dockerfile_list = [d for d in os.listdir(tmp_dir) if d in dockerfile_names]
        assert len(dockerfile_list) > 0, "No Dockerfile is found at the repository {}".format(self.repo_url)
        dockerfile = sorted(dockerfile_list)[-1]
        image_name = "everware/{}-{}-{}".format(self.user.name,
                                                self.escaped_repo_url,
                                                self.repo_sha)

        self.log.debug("Building image {}, dockerfile: {}".format(image_name, dockerfile))

        build_log = yield self.docker('build',
                                      path=tmp_dir,
                                      tag=image_name,
                                      dockerfile=dockerfile,
                                      rm=True)

        self.log.debug("".join(str(line) for line in build_log))
        self.log.info("Built docker image {}".format(image_name))

        images = yield self.docker('images', image_name)
        self.log.debug(images)

        yield super(CustomDockerSpawner, self).start(
            image=image_name,
        )

    def _env_default(self):
        env = super(CustomDockerSpawner, self)._env_default()

        env.update({'JPY_GITHUBURL': self.repo_url})

        return env
