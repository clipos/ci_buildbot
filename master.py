# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright © 2019 ANSSI. All rights reserved.

"""CLIP OS Project Buildbot continuous integration master node configuration"""

import datetime
import json
import os
import string
import sys

from typing import Dict, Any, Optional

import clipos

from buildbot.plugins import changes, schedulers, util, worker
from buildbot.schedulers.forcesched import oneCodebase
from clipos.commons import line  # utility functions and stuff


#
# PRIVATE SETUP SETTINGS RELATIVE TO THE BUILDBOT MASTER DEPLOYMENT
#
# There are some private settings that depends directly on the way this
# Buildbot master instance has been deployed. Usually these settings come from
# the environment variables of the Docker container instance running the
# Buildbot master (e.g. BUILDBOT_MASTER_PB_PORT which indicates the port number
# on which Buildbot is expected to listen).
# However, since the environment may be exposed in some of the builds and since
# some of these variables may contain sensitive information (e.g. the password
# to the Buildbot database), a "setup_settings.json" file is expected to be
# created by the Docker container entrypoint script which is also in charge of
# stripping those values from the environment. This avoids exposing such
# settings in the Buildbot local workers.
#

# If the provided filepath cannot be found, the following class will load
# default dummy settings that will correspond to a local deployment for testing
# purposes (mainly for debugging purposes for developers).
setup = clipos.buildmaster.SetupSettings("setup_settings.json")


#
# BUILDBOT MASTER CONFIGURATION DATA STRUCTURE INITIALIZATION
#
# This is the dictionary that the buildmaster pays attention to. The variable
# MUST be named BuildmasterConfig. A shorter alias is defined for typing
# convenience below.
#

# Also define a shorter alias ("c") to save typing:
BuildmasterConfig = c = setup.buildmaster_config_base()


#
# WORKERS
#
# The 'workers' list defines the set of recognized workers. Each element is a
# Worker object, specifying a unique worker name and password.
#

# This worker MUST only be used for build jobs requiring access to the
# Docker socket. The builders tied to this worker should be carefully reviewed
# as some nasty stuff can be done when a user has access to the Docker daemon:
docker_operations_worker = worker.LocalWorker('_docker-client-localworker')

# Most of the Docker latent workers are there to ensure that the CLIP OS
# toolkit is functional on most of the popular Linux distributions. It makes
# few sense to build each time the CLIP OS builds on all the workers, instead
# we chose a reference Docker worker flavor (on which we are going to do all
# the builds) and we leave the rest of the flavors for weekly builds just to
# make sure that the CLIP OS toolkit is functional on those flavors of worker.
reference_worker_flavor = 'debian-sid'
unprivileged_reference_workers = []   # changed below
privileged_reference_workers = []  # changed below

# All the Docker latent workers defined and usable by the CLIP OS project
# buildbot:
all_clipos_docker_latent_workers = []
for flavor in clipos.workers.DockerLatentWorker.FLAVORS:
    # Generate both privileged and unprivileged versions of container workers:
    for privileged in [False, True]:
        worker = clipos.workers.DockerLatentWorker(
            flavor=flavor,
            privileged=privileged,
            container_network_mode=setup.docker_worker_containers_network_mode,
            docker_host=setup.docker_host_uri,
            buildmaster_host_for_dockerized_workers=setup.buildmaster_host_for_dockerized_workers,

            # By default, max_builds is unlimited, reduce this:
            max_builds=5,  # TODO: any better heuristic?
        )
        all_clipos_docker_latent_workers.append(worker)

        if flavor == reference_worker_flavor:
            if privileged:
                privileged_reference_workers.append(worker)
            else:
                unprivileged_reference_workers.append(worker)

c['workers'] = [
    # The worker for the Docker operations (create Docker images to be used as
    # Docker latent workers):
    docker_operations_worker,

    # All the Docker latent workers for CLIP OS build (the build envs):
    *all_clipos_docker_latent_workers,
]


#
# BUILDERS
#
# The 'builders' list defines the Builders, which tell Buildbot how to perform
# a build: what steps, and which workers can execute them.  Note that any
# particular build will only take place on one worker.
#

# Builders that build the CLIP OS Dockerized build environment images to be
# then used by the clipos.workers.DockerLatentWorker:
docker_buildenv_image_builders = []
for flavor, props in clipos.workers.DockerLatentWorker.FLAVORS.items():
    docker_buildenv_image_builders.append(
        util.BuilderConfig(
            name='docker-worker-image env:{}'.format(flavor),  # keep this short
            description=line(
                """Build the Docker image to use as a Buildbot Docker latent
                worker based upon a {} userland.""").format(
                    props['description']),
            tags=['docker-worker-image', "docker-env:{}".format(flavor)],

            # Temporary: Always build on the worker that have access to the
            # Docker socket:
            workernames=[
                docker_operations_worker.name,
            ],

            factory=clipos.build_factories.BuildDockerImage(
                flavor=flavor, buildmaster_setup=setup),
        )
    )

# The builder that generates the repo dir and git lfs archive artifacts from
# scratch:
repo_sync_builder = util.BuilderConfig(
    name='repo-sync',
    description=line(
        """Synchronize the CLIP OS source tree and produce a tarball from the
        ".repo" directory contents. This tarball is then archived as a reusable
        artifact for the other builders. This is done in order to speed up the
        other builds and avoid overloading the Git repositories server with
        constant repo syncs."""),
    tags=['repo-sync', 'update-artifacts'],

    # The reference CLIP OS build environemnt latent Docker worker that is NOT
    # privileged:
    workernames=[
        worker.name for worker in all_clipos_docker_latent_workers
        if worker.flavor == reference_worker_flavor and not worker.privileged
    ],

    factory=clipos.build_factories.RepoSyncFromScratchAndArchive(
        # Pass on the buildmaster setup settings
        buildmaster_setup=setup,
    ),
)

# CLIP OS complete build on all the Docker latent worker flavors:
clipos_on_all_flavors_builders = []
for flavor in clipos.workers.DockerLatentWorker.FLAVORS:
    builder_name = "clipos"
    if flavor != reference_worker_flavor:
        builder_name += ' env:{}'.format(flavor)
    builder = util.BuilderConfig(
        name=builder_name,
        tags=['clipos', 'docker-env:{}'.format(flavor)],
        workernames=[
            worker.name for worker in all_clipos_docker_latent_workers
            if worker.flavor == flavor and worker.privileged
        ],
        factory=clipos.build_factories.ClipOsProductBuildBuildFactory(
            # Pass on the buildmaster setup settings
            buildmaster_setup=setup,
        ),
    )
    # Keep a reference on the reference builder environment for the nightly
    # scheduler:
    if flavor == reference_worker_flavor:
        reference_clipos_builder = builder
    clipos_on_all_flavors_builders.append(builder)

clipos_ondemand_builder = util.BuilderConfig(
    name='clipos ondemand',
    tags=['clipos', 'on-demand',
          'docker-env:{}'.format(reference_worker_flavor)],
    workernames=[w.name for w in privileged_reference_workers],
    factory=clipos.build_factories.ClipOsProductBuildBuildFactory(
        # Pass on the buildmaster setup settings
        buildmaster_setup=setup,
    ),
)


c['builders'] = [
    # All the CLIP OS Dockerized build env image builders:
    *docker_buildenv_image_builders,

    # Repo sync test
    repo_sync_builder,

    # CLIP OS builds from scratch
    *clipos_on_all_flavors_builders,   # CLIP OS images

    # CLIP OS builds on demand
    clipos_ondemand_builder,           # CLIP OS images
]



#
# SCHEDULERS
#
# Configure the Schedulers, which decide how to react to incoming changes.
#

repo_sync_nightly_sched = schedulers.Nightly(
    name='repo-sync-nightly-update',
    builderNames=[
        repo_sync_builder.name,
    ],
    dayOfWeek='1,2,3,4,5',  # only work days: from Monday (1) to Friday (5)
    hour=0, minute=0,  # at 00:00
    codebases={"": {
        "repository": setup.clipos_manifest_git_url,
        "branch": "master",
    }},
    properties={
        "cleanup_workspace": True,
    },
)

# CLIP OS builds schedulers:
clipos_incremental_build_intraday_sched = schedulers.Nightly(
    name='clipos-master-intraday-incremental-build',
    builderNames=[
        reference_clipos_builder.name,
    ],
    dayOfWeek='1,2,3,4,5',  # only work days: from Monday (1) to Friday (5)
    hour=12, minute=30,  # at 12:30 (i.e. during lunch)
    codebases={"": {
        "repository": setup.clipos_manifest_git_url,
        "branch": "master",
    }},
    properties={
        "cleanup_workspace": True,
        "force_repo_quicksync_artifacts_download": False,
        "buildername_providing_repo_quicksync_artifacts": repo_sync_builder.name,

        "produce_sdks_artifacts": False,
        "reuse_sdks_artifacts": True,
        "buildername_providing_sdks_artifacts": reference_clipos_builder.name,

        "produce_cache_artifacts": False,
        "reuse_cache_artifacts": True,
        "buildername_providing_cache_artifacts": reference_clipos_builder.name,

        "produce_build_artifacts": True,
    },
)

clipos_build_nightly_sched = schedulers.Nightly(
    name='clipos-master-nightly-build',
    builderNames=[
        reference_clipos_builder.name,
    ],
    dayOfWeek='1,2,3,4,5',  # only work days: from Monday (1) to Friday (5)
    hour=0, minute=45,  # at 00:45
    codebases={"": {
        "repository": setup.clipos_manifest_git_url,
        "branch": "master",
    }},
    properties={
        "cleanup_workspace": True,
        "force_repo_quicksync_artifacts_download": False,
        "buildername_providing_repo_quicksync_artifacts": repo_sync_builder.name,

        "produce_sdks_artifacts": True,
        "reuse_sdks_artifacts": False,

        "produce_cache_artifacts": True,
        "reuse_cache_artifacts": False,

        "produce_build_artifacts": True,
    },
)

clipos_build_weekly_sched = schedulers.Nightly(
    name='clipos-master-weekly-build',
    builderNames=[
        *(builder.name for builder in clipos_on_all_flavors_builders),
    ],
    dayOfWeek='6',  # on Saturdays
    hour=12, minute=0,  # at noon
    codebases={"": {
        "repository": setup.clipos_manifest_git_url,
        "branch": "master",
    }},
    properties={
        "cleanup_workspace": True,
        "force_repo_quicksync_artifacts_download": True,
        "buildername_providing_repo_quicksync_artifacts": repo_sync_builder.name,

        "produce_sdks_artifacts": True,
        "reuse_sdks_artifacts": False,

        "produce_cache_artifacts": True,
        "reuse_cache_artifacts": False,

        "produce_build_artifacts": True,
    },
)

# Buildbot worker Dockerized environment build schedulers:
docker_buildenv_image_rebuild_weekly_sched = schedulers.Nightly(
    name='docker-workers-weekly-rebuild',
    builderNames=[
        # Rebuild all the Dockerized CLIP OS build environments on weekends:
        *(builder.name for builder in docker_buildenv_image_builders),
    ],
    dayOfWeek='6',  # on Saturdays
    hour=9, minute=0,  # at 09:00
    codebases={"": {
        "repository": setup.config_git_clone_url,
        "branch": setup.config_git_revision,
    }},
)


# Force rebuild Docker images
docker_buildenv_image_rebuild_force_sched = schedulers.ForceScheduler(
    name='force-docker-buildenv-image',
    buttonName="Force a rebuild of this Dockerized build environment now",
    label="Rebuild now a Docker build environement image",
    builderNames=[
        *(builder.name for builder in docker_buildenv_image_builders),
    ],
    codebases=oneCodebase(
        project=None,
        repository=setup.config_git_clone_url,
        branch=setup.config_git_revision,
        revision=None,
    ),
)

# Scheduler to force a resynchronizaion from scratch of the CLIP OS source
# tree:
repo_sync_force_sched = schedulers.ForceScheduler(
    name='force-repo-sync-clipos',
    buttonName="Force a synchronization from scratch of the source tree",
    label="Synchronization from scratch",
    builderNames=[
        # Only one builder is eligible to this scheduler:
        repo_sync_builder.name,
    ],
    codebases=oneCodebase(
        project=None,
        repository=setup.clipos_manifest_git_url,
        branch="master",
        revision=None,
    ),
    properties=[
        util.FixedParameter(
            "cleanup_workspace", default=True,
        ),
    ],
)

# Scheduler for the CLIP OS on-demand custom builds:
clipos_custom_build_force_sched = schedulers.ForceScheduler(
    name='clipos-custom-build',
    buttonName="Start a custom build",
    label="Custom build",
    builderNames=[
        clipos_ondemand_builder.name,
    ],
    codebases = [
        util.CodebaseParameter(
            codebase='',
            label="CLIP OS source manifest",
            project='clipos',
            repository=util.StringParameter(
                name="repository",
                label="Manifest repository URL",
                default=setup.clipos_manifest_git_url,
                size=100,
            ),
            branch=util.StringParameter(
                name="branch",
                label="Manifest branch to use",
                default="master",
                size=50,
            ),
            revision=None,
        ),
    ],
    properties=[
        util.FixedParameter(
            "buildername_providing_repo_quicksync_artifacts",
            default=repo_sync_builder.name,
        ),

        # Note: we deliberately leave the name argument to empty strings for
        # the NestedParameter classes below because parameter namespacing is
        # way too cumbersome to manage in the build factories.
        util.NestedParameter(name="", layout="tabs", fields=[
            util.NestedParameter(name="",
                label="Source tree checkout",
                layout="vertical",
                fields=[
                    util.BooleanParameter(
                        name="use_local_manifest",
                        label="Use the repo local manifest below",
                        default=False,
                    ),
                    util.TextParameter(
                        name="local_manifest_xml",
                        label="Local manifest to apply changes to the source tree",
                        default=r"""
<manifest>
  <!--
    This is an example of a local-manifest file: tweak it to your needs.
    You can find examples below that use dummy values and that illustrate the
    operations you may want to do with such a local-manifest file:
  -->

  <!-- How to declare an additional Git remote you can use futher down: -->
  <remote name="foobaros-github" fetch="https://github.com/foobaros" />

  <!-- How to remove from the source tree a useless item: -->
  <remove-project name="src_external_uselessproject" />

  <!-- How to declare and checkout a new item in the source tree: -->
  <project path="products/foobaros" name="products_foobaros" remote="foobaros-github" revision="refs/changes/42/1342/7" />

  <!-- How to change the checkout properties of an existing source tree item: -->
  <remove-project name="src_portage_clipos" />
  <project path="src/portage/clipos" name="src_portage_clipos" revision="refs/changes/37/1337/2" />
</manifest>
                        """.strip(),
                        cols=80, rows=6,
                    ),
                ],
            ),

            util.NestedParameter(name="",
                label="Workspace settings",
                layout="vertical",
                fields=[
                    util.BooleanParameter(
                        name="cleanup_workspace",
                        label="Clean up the workspace beforehand (strongly advised)",
                        default=True,
                    ),
                    util.BooleanParameter(
                        name="force_repo_quicksync_artifacts_download",
                        label="Force the fetch of the source tree quick-sync artifacts",
                        default=False,
                    ),
                ],
            ),

            util.NestedParameter(name="",
                label="CLIP OS build process options",
                layout="vertical",
                columns=1,
                fields=[
                    util.BooleanParameter(
                        name="produce_sdks_artifacts",
                        label="Produce SDKs artifacts and upload them on the buildmaster",
                        default=False,
                    ),
                    util.BooleanParameter(
                        name="reuse_sdks_artifacts",
                        label="Reuse SDKs artifacts instead of bootstrapping SDKs from scratch",
                        default=True,
                    ),
                    util.ChoiceStringParameter(
                        name="buildername_providing_sdks_artifacts",
                        label="Builder name from which retrieving SDKs artifact (latest artifacts will be used)",
                        choices=[
                            *[b.name for b in clipos_on_all_flavors_builders],
                        ],
                        default=reference_clipos_builder.name,
                    ),

                    util.BooleanParameter(
                        name="produce_cache_artifacts",
                        label="Produce cache artifacts (binary packages, etc.) and upload them on the buildmaster",
                        default=False,
                    ),
                    util.BooleanParameter(
                        name="reuse_cache_artifacts",
                        label="Reuse cache artifacts instead of bootstrapping SDKs from scratch",
                        default=True,
                    ),
                    util.ChoiceStringParameter(
                        name="buildername_providing_cache_artifacts",
                        label="Builder name from which retrieving cache artifact (latest artifacts will be used)",
                        choices=[
                            *[b.name for b in clipos_on_all_flavors_builders],
                        ],
                        default=reference_clipos_builder.name,
                    ),

                    util.BooleanParameter(
                        name="produce_build_artifacts",
                        label="Produce build result artifacts and upload them on the buildmaster",
                        default=False,
                    ),
                ],
            ),

        ],
    )],
)

c['schedulers'] = [
    # Intra-day schedulers:
    clipos_incremental_build_intraday_sched,

    # Nightly schedulers:
    repo_sync_nightly_sched,
    clipos_build_nightly_sched,

    # Weekly schedulers:
    clipos_build_weekly_sched,
    docker_buildenv_image_rebuild_weekly_sched,

    # Forced schedulers:
    repo_sync_force_sched,
    docker_buildenv_image_rebuild_force_sched,
    clipos_custom_build_force_sched,
]



#
# CHANGESOURCES
#
# The 'change_source' setting tells the buildmaster how it should find out
# about source code changes.
#

c['change_source'] = []

# TODO, FIXME: Declare a proper change_source poller:
#if setup.config_git_clone_url:
#    c['change_source'].append(
#        changes.GitPoller(
#            repourl=setup.config_git_clone_url,
#            workdir='gitpoller-workdir',
#            branch=setup.config_git_revision,
#            pollInterval=600,     # every 10 minutes
#        )
#    )



#
# JANITORS
#

# Configure a janitor which will delete all build logs to avoid clogging up the
# database.
c['configurators'] = [
    util.JanitorConfigurator(
        logHorizon=datetime.timedelta(weeks=12), # older than roughly 3 months
        dayOfWeek=0,  # on Sundays
        hour=12, minute=0,  # at noon
    ),
]



#
# BUILDBOT SERVICES
#
# 'services' is a list of BuildbotService items like reporter targets. The
# status of each build will be pushed to these targets. buildbot/reporters/*.py
# has a variety to choose from, like IRC bots.
#

c['services'] = []


# vim: set ft=python ts=4 sts=4 sw=4 tw=79 et:
