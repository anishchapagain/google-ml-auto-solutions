# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base task file for a test job."""

import abc
import dataclasses
import datetime
import shlex
from dags.common.quarantined_tests import QuarantineTests
from typing import Optional, Tuple, Union
import airflow
from airflow.models.taskmixin import DAGNode
from airflow.utils.task_group import TaskGroup
from xlml.apis import gcp_config, metric_config, test_config
from xlml.utils import gpu, metric, name_format, ssh, tpu, xpk, gke


class BaseTask(abc.ABC):
  """This is a class to set up base tasks."""

  @abc.abstractmethod
  def run(self) -> DAGNode:
    """Run a test job.

    Returns:
      A DAG node that executes this test.
    """
    ...

  def run_with_quarantine(self, quarantine_task_group):
    """Run a test job. If the test job is flaky, wrap it in a special task grop.

    Returns:
      A DAG node that executes this test.
    """
    test_name = self.task_test_config.benchmark_id
    if QuarantineTests.is_quarantined(test_name):
      with quarantine_task_group:
        return self.run()
    else:
      return self.run()


def run_queued_resource_test(
    # TODO(wcromar): make these args less verbose
    task_test_config: test_config.TestConfig[test_config.Tpu],
    task_gcp_config: gcp_config.GCPConfig,
    task_metric_config: Optional[metric_config.MetricConfig] = None,
    tpu_create_timeout: datetime.timedelta = datetime.timedelta(minutes=60),
    tpu_name_env_var: bool = False,
    all_workers: bool = True,
):
  """This is a class to set up tasks for TPU provisioned by Queued Resource.

  Test steps:
  1. Generates a random TPU name and SSH keys, creates a Queued Resource, and
     runs the test config's setup script on the TPU when it is ready.
  2. Run the TPU test in `task_test_config` via SSH.
  3. Process metrics and metadata, then insert them into BigQuery tables.
  4. Clean up TPU resources created by for this test

  Attributes:
    task_test_config: Test configs to run on this TPU.
    task_gcp_config: Runtime TPU creation parameters.
    task_metric_config: Metric configs to process metrics.
    tpu_create_timeout: Time to provision the machine.
    tpu_name_env_var: The flag to define if set up env variable for tpu name.
    all_workers: The flag to define if run commands on all workers or worker 0
      only.

  Returns:
      A task group with the following tasks chained: provision, run_model,
      post_process and clean_up.
  """

  with TaskGroup(
      group_id=task_test_config.benchmark_id, prefix_group_id=True
  ) as test:
    with TaskGroup(group_id="provision") as provision:
      with TaskGroup(group_id="initialize"):
        tpu_name = tpu.generate_tpu_name(
            task_test_config.benchmark_id, tpu_name_env_var
        )
        ssh_keys = ssh.generate_ssh_keys()
        output_location = name_format.generate_gcs_folder_location(
            task_test_config.gcs_subfolder,
            task_test_config.benchmark_id,
        )

      queued_resource_op, queued_resource_name = tpu.create_queued_resource(
          tpu_name,
          task_gcp_config,
          ssh_keys,
          tpu_create_timeout,
          task_test_config,
      )

      queued_resource_op >> tpu.ssh_tpu.override(task_id="setup")(
          queued_resource_name,
          task_test_config.setup_script,
          ssh_keys,
          True if task_test_config.test_name.startswith("tf_") else all_workers,
      )

    run_model = tpu.ssh_tpu.override(
        task_id="run_model",
        execution_timeout=task_test_config.timeout,
        owner=task_test_config.task_owner,
    )(
        queued_resource_name,
        task_test_config.test_script,
        ssh_keys,
        all_workers,
        env={metric_config.SshEnvVars.GCS_OUTPUT.name: output_location},
    )

    with TaskGroup(group_id="post_process") as post_process:
      process_id = metric.generate_process_id.override(retries=0)()
      metric.process_metrics.override(retries=0)(
          process_id,
          task_test_config,
          task_metric_config,
          task_gcp_config,
          folder_location=output_location,
      )

    clean_up = tpu.delete_queued_resource.override(group_id="clean_up")(
        queued_resource_name
    )

    provision >> run_model >> post_process >> clean_up

  return test


@dataclasses.dataclass
class XpkTask(BaseTask):
  """This is a class to set up tasks for TPU/GPU provisioned by XPK tool.

  Attributes:
    task_test_config: Test configs to run on this TPU/GPU.
    task_gcp_config: Runtime TPU/GPU creation parameters.
    task_metric_config: Metric configs to process metrics.
    workload_provision_timeout: Time allowed for provisioning a workload.
  """

  task_test_config: Union[
      test_config.TpuGkeTest, test_config.GpuXpkTest, test_config.CpuGkeTest
  ]
  task_gcp_config: gcp_config.GCPConfig
  task_metric_config: Optional[metric_config.MetricConfig] = None
  workload_provision_timeout: datetime.timedelta = datetime.timedelta(
      minutes=300
  )

  def run(
      self,
      *,
      gcs_location: Optional[airflow.XComArg] = None,
      use_vertex_tensorboard: bool = False,
      use_pathways: bool = False,
      skip_post_process: bool = False,
      ramdisk_directory: str = "",
      mtc_enabled: bool = False,
      xpk_branch: str = xpk.MAIN_BRANCH,
  ) -> DAGNode:
    """Run a test job within a docker image.

    Attributes:
      gcs_location: GCS path for all artifacts of the test.
      use_vertex_tensorboard: Set to True to view workload data on
        Vertex AI Tensorboard.

    Returns:
      A task group with the following tasks chained: run_model and
      post_process.
    """
    with TaskGroup(group_id=self.task_test_config.benchmark_id) as group:
      run_model, gcs_path = self.run_model(
          gcs_location,
          use_vertex_tensorboard,
          use_pathways,
          ramdisk_directory,
          mtc_enabled,
          xpk_branch,
      )
      if not skip_post_process:
        run_model >> self.post_process(gcs_path)

    return group

  def run_with_name_gen_and_quarantine(
      self,
      quarantine_task_group,
      use_pathways: bool = False,
      xpk_branch: str = xpk.MAIN_BRANCH,
      run_name_env: str = "M_RUN_NAME",
      nested_run_name_in_tb_file_location: bool = True,
  ) -> DAGNode:
    test_name = self.task_test_config.benchmark_id
    if QuarantineTests.is_quarantined(test_name):
      with quarantine_task_group:
        return self.run_with_run_name_generation(
            use_pathways,
            xpk_branch,
            run_name_env,
            nested_run_name_in_tb_file_location,
        )
    else:
      return self.run_with_run_name_generation(
          use_pathways,
          xpk_branch,
          run_name_env,
          nested_run_name_in_tb_file_location,
      )

  def run_with_run_name_generation(
      self,
      use_pathways: bool = False,
      xpk_branch: str = xpk.MAIN_BRANCH,
      run_name_env: str = "M_RUN_NAME",
      nested_run_name_in_tb_file_location: bool = True,
  ) -> DAGNode:
    """Generate a unique run name, tensorboard file location,
    and profile file location (if metric config has profile),
    then run a test job within a docker image.

    Returns:
      A task group with the following tasks chained: generate_run_name,
      generate_tb_file_location, generate_profile_file_location (optional),
      run provision, run_model, post_process.
    """
    with TaskGroup(
        group_id=self.task_test_config.benchmark_id, prefix_group_id=True
    ) as group:
      run_name = name_format.generate_run_name(
          self.task_test_config.benchmark_id
      )
      tb_file_location = name_format.generate_tb_file_location(
          run_name,
          self.task_metric_config.tensorboard_summary.file_location,
          nested_run_name_in_tb_file_location,
      )

      # Set run_name in run_model_cmds
      new_run_model_cmds = [f"export {run_name_env}={run_name}"]
      for cmd in self.task_test_config.run_model_cmds:
        new_run_model_cmds.append(cmd)
      self.task_test_config.run_model_cmds = new_run_model_cmds

      # Update tensorboard file location
      self.task_metric_config.tensorboard_summary.file_location = (
          tb_file_location
      )

      # Update profile file location
      if self.task_metric_config.profile:
        profile_file_location = name_format.generate_profile_file_location(
            run_name, self.task_metric_config.profile.file_location
        )
        self.task_metric_config.profile.file_location = profile_file_location
        run_model, gcs_path = self.run_model(
            use_pathways=use_pathways, xpk_branch=xpk_branch
        )
        (
            run_name
            >> (tb_file_location, profile_file_location)
            >> run_model
            >> self.post_process(gcs_path)
        )
      else:
        run_model, gcs_path = self.run_model(
            use_pathways=use_pathways, xpk_branch=xpk_branch
        )
        (
            run_name
            >> tb_file_location
            >> run_model
            >> self.post_process(gcs_path)
        )
    return group

  def run_model(
      self,
      gcs_location: Optional[airflow.XComArg] = None,
      use_vertex_tensorboard: bool = False,
      use_pathways: bool = False,
      ramdisk_directory: str = "",
      mtc_enabled: bool = False,
      xpk_branch: str = xpk.MAIN_BRANCH,
  ) -> DAGNode:
    """Run the TPU/GPU test in `task_test_config` using xpk.

    Attributes:
      gcs_location: GCS path for all artifacts of the test.
      use_vertex_tensorboard: Set to True to view workload data on
        Vertex AI Tensorboard.

    Returns:
      A DAG node that executes the model test.
    """
    with TaskGroup(group_id="run_model") as group:
      workload_id = xpk.generate_workload_id(self.task_test_config.benchmark_id)
      if gcs_location:
        gcs_path = gcs_location
      else:
        gcs_path = name_format.generate_gcs_folder_location(
            self.task_test_config.gcs_subfolder,
            self.task_test_config.benchmark_id,
        )
      launch_workload = self.launch_workload(
          workload_id,
          gcs_path,
          use_vertex_tensorboard,
          use_pathways,
          ramdisk_directory,
          mtc_enabled,
          xpk_branch,
      )
      wait_for_workload_completion = xpk.wait_for_workload_completion.override(
          timeout=int(self.task_test_config.timeout.total_seconds()),
      )(
          workload_id=workload_id,
          project_id=self.task_gcp_config.project_name,
          region=self.task_gcp_config.zone[:-2],
          cluster_name=self.task_test_config.cluster_name,
      )

      clean_up_workload = xpk.clean_up_workload(
          workload_id=workload_id,
          project_id=self.task_gcp_config.project_name,
          zone=self.task_gcp_config.zone,
          cluster_name=self.task_test_config.cluster_name,
      )

      (
          (workload_id, gcs_path)
          >> launch_workload
          >> wait_for_workload_completion
          >> clean_up_workload
      )
      return group, gcs_path

  def launch_workload(
      self,
      workload_id: str,
      gcs_path: str,
      use_vertex_tensorboard: bool,
      use_pathways: bool = False,
      ramdisk_directory: str = "",
      mtc_enabled: bool = False,
      xpk_branch: str = xpk.MAIN_BRANCH,
  ) -> DAGNode:
    """Create the workload and wait for it to provision."""
    with TaskGroup(group_id="launch_workload") as group:
      run_workload = xpk.run_workload.override(
          owner=self.task_test_config.task_owner
      )(
          task_id="run_workload",
          cluster_project=self.task_gcp_config.project_name,
          zone=self.task_gcp_config.zone,
          cluster_name=self.task_test_config.cluster_name,
          benchmark_id=self.task_test_config.benchmark_id,
          workload_id=workload_id,
          gcs_path=gcs_path,
          docker_image=self.task_test_config.docker_image,
          accelerator_type=self.task_test_config.accelerator.name,
          run_cmds=self.task_test_config.test_script,
          num_slices=self.task_test_config.num_slices,
          use_vertex_tensorboard=use_vertex_tensorboard,
          use_pathways=use_pathways,
          ramdisk_directory=ramdisk_directory,
          mtc_enabled=mtc_enabled,
          xpk_branch=xpk_branch,
      )
      wait_for_workload_start = xpk.wait_for_workload_start.override(
          timeout=self.workload_provision_timeout.total_seconds()
      )(
          workload_id=workload_id,
          project_id=self.task_gcp_config.project_name,
          region=self.task_gcp_config.zone[:-2],
          cluster_name=self.task_test_config.cluster_name,
      )
      run_workload >> wait_for_workload_start
      return group

  def post_process(self, result_location: Optional[str] = None) -> DAGNode:
    """Process metrics and metadata, and insert them into BigQuery tables.

    Returns:
      A DAG node that executes the post process.
    """
    with TaskGroup(group_id="post_process") as group:
      process_id = metric.generate_process_id.override(retries=0)()
      post_process_metrics = metric.process_metrics.override(retries=0)(
          process_id,
          self.task_test_config,
          self.task_metric_config,
          self.task_gcp_config,
          folder_location=result_location,
      )

      if self.task_metric_config and self.task_metric_config.profile:
        self.task_metric_config.profile.metrics = (
            metric.xplane_to_metrics.override(retries=0)(
                self.task_metric_config.profile.file_location
            )
        )
        (
            process_id
            >> self.task_metric_config.profile.metrics
            >> post_process_metrics
        )
      else:
        process_id >> post_process_metrics

      return group


@dataclasses.dataclass
class GpuCreateResourceTask(BaseTask):
  """This is a class to set up tasks for GPU.

  Attributes:
    image_project: the project that an image belongs to.
    image_family: the family group that an image belongs to.
    task_test_config: task configutation.
    task_gcp_config: gcp related config (e.g., zone, project) for the task.
    task_metric_config: metric configuration (e.g., result gcs path).
    gpu_create_timeout: timeout when waiting for the GPU vm creation.
    install_nvidia_drivers: whether to install Nvidia drivers.
    existing_instance_name: whether an existing GPU instance shall be used.
    reservation: use a specific reservation for the VM instance, if available
  """

  image_project: str
  image_family: str
  task_test_config: test_config.TestConfig[test_config.Gpu]
  task_gcp_config: gcp_config.GCPConfig
  task_metric_config: Optional[metric_config.MetricConfig] = None
  gpu_create_timeout: datetime.timedelta = datetime.timedelta(minutes=60)
  install_nvidia_drivers: bool = False
  existing_instance_name: str = None
  reservation: bool = False

  def run(self) -> DAGNode:
    """Run a test job.

    Returns:
      A task group with the following tasks chained: provision, run_model,
      post_process, clean_up.
    """
    # piz: We skip the queued resource for GPU for now since there is no queued
    # resource command for GPU.
    if self.existing_instance_name is not None:
      return self.run_with_existing_instance()

    with TaskGroup(
        group_id=self.task_test_config.benchmark_id, prefix_group_id=True
    ) as group:
      (
          provision,
          ip_address,
          instance_name,
          ssh_keys,
          gcs_location,
      ) = self.provision()
      # If you already specify `task_metric_config.json_lines` value in the
      # test config script, then `gcs_location` will take no effect.
      if (
          self.task_metric_config
          and self.task_metric_config.use_runtime_generated_gcs_folder
      ):
        env_variable = {
            f"{metric_config.SshEnvVars.GCS_OUTPUT.name}": gcs_location
        }
      else:
        env_variable = None
      run_model = self.run_model(ip_address, ssh_keys, env_variable)
      post_process = self.post_process(gcs_location)
      clean_up = self.clean_up(
          instance_name,
          self.task_gcp_config.project_name,
          self.task_gcp_config.zone,
      )
      provision >> run_model >> post_process >> clean_up
    return group

  def run_with_existing_instance(self) -> DAGNode:
    """Run a test job via existing instance.

    Returns:
      A task group with the following tasks chained:  provision, run_model and post_process, clean_up.
    """
    with TaskGroup(
        group_id=self.task_test_config.benchmark_id, prefix_group_id=True
    ) as group:
      (
          provision,
          ip_address,
          ssh_keys,
          gcs_location,
      ) = self.provision_via_existing_instance()
      if (
          self.task_metric_config
          and self.task_metric_config.use_runtime_generated_gcs_folder
      ):
        env_variable = {
            f"{metric_config.SshEnvVars.GCS_OUTPUT.name}": gcs_location
        }
      else:
        env_variable = None
      post_process = self.post_process(gcs_location)
      run_model = self.run_model(ip_address, ssh_keys, env_variable)
      clean_up = self.clean_up_existing_instance(ssh_keys)
      provision >> run_model >> post_process >> clean_up
    return group

  def provision_via_existing_instance(
      self,
  ) -> Tuple[DAGNode, airflow.XComArg, airflow.XComArg, airflow.XComArg,]:
    """Provision an existing GPU accelerator.

    Returns:
      A DAG node that will provision a GPU, an XCome value of the ip address
      for the host,an XCom value for the SSH keys.
    """
    with TaskGroup(group_id="provision") as group:
      ssh_keys = ssh.generate_ssh_keys()
      ip_address = gpu.get_existing_resource(
          instance_name=self.existing_instance_name,
          ssh_keys=ssh_keys,
          gcp=self.task_gcp_config,
      )
      gcs_location = name_format.generate_gcs_folder_location(
          self.task_test_config.gcs_subfolder,
          self.task_test_config.benchmark_id,
      )
      return group, ip_address, ssh_keys, gcs_location

  def provision(
      self,
  ) -> Tuple[
      DAGNode,
      airflow.XComArg,
      airflow.XComArg,
      airflow.XComArg,
      airflow.XComArg,
  ]:
    """Provision a GPU accelerator via a resource creation.

    Generates a random GPU name and SSH keys, creates a VM Resource, and
    runs the test config's setup script on the GPU when it is ready.

    Returns:
      A DAG node that will provision a GPU, an XCome value of the ip address
      for the host, an XCom value for the GPU name, and an XCom value for
      the SSH keys.

    Raises:
      AirflowTaskTimeout: An error occurs when execution_timeout is breached.
    """
    with TaskGroup(group_id="provision") as group:
      with TaskGroup(group_id="initialize"):
        gpu_name = gpu.generate_gpu_name()
        ssh_keys = ssh.generate_ssh_keys()
        gcs_location = name_format.generate_gcs_folder_location(
            self.task_test_config.gcs_subfolder,
            self.task_test_config.benchmark_id,
        )

      ip_address = gpu.create_resource(
          gpu_name,
          self.image_project,
          self.image_family,
          self.task_test_config.accelerator,
          self.task_gcp_config,
          ssh_keys,
          timeout=self.gpu_create_timeout,
          install_nvidia_drivers=self.install_nvidia_drivers,
          reservation=self.reservation,
      )

      ip_address >> gpu.ssh_host.override(task_id="setup")(
          ip_address,
          self.task_test_config.setup_script,
          ssh_keys,
      )

    return group, ip_address, gpu_name, ssh_keys, gcs_location

  def run_model(
      self,
      resource: airflow.XComArg,
      ssh_keys: airflow.XComArg,
      env: Optional[airflow.XComArg] = None,
  ) -> DAGNode:
    """Run the GPU test in `task_test_config`.

    Args:
      gpu_name: XCom value for the GPU name (string).
      ssh_keys: And XCom value for the GPU's SSH keys (SshKeys).

    Returns:
      A DAG node that executes the model test.
    """
    return gpu.ssh_host.override(
        task_id="run_model",
        execution_timeout=self.task_test_config.timeout,
        owner=self.task_test_config.task_owner,
    )(
        resource,
        self.task_test_config.test_script,
        ssh_keys,
        env,
    )

  def post_process(
      self,
      result_location: Optional[airflow.XComArg] = None,
  ) -> DAGNode:
    """Process metrics and metadata, and insert them into BigQuery tables.

    Returns:
      A DAG node that executes the post process.
    """
    with TaskGroup(group_id="post_process") as group:
      process_id = metric.generate_process_id.override(retries=0)()
      metric.process_metrics.override(retries=0)(
          process_id,
          self.task_test_config,
          self.task_metric_config,
          self.task_gcp_config,
          folder_location=result_location,
      )
      return group

  def clean_up(
      self, resource: airflow.XComArg, project_id: str, zone: str
  ) -> DAGNode:
    """Clean up GPU resources created by `provision`.

    Args:
      resource: an XCom value for the qualified instance name.
      project_id: project of the instance.
      zone: zone of the instance.
    Returns:
      A DAG node that deletes the resource and its owned nodes.

    Raises:
      AirflowTaskTimeout: An error occurs when execution_timeout is breached.
    """
    return gpu.delete_resource.override(group_id="clean_up")(
        resource, project_id, zone
    )

  def clean_up_existing_instance(self, ssh_keys: airflow.XComArg) -> DAGNode:
    """Clean up existing GPU resources - remove the one-time use generated ssh_keys.

    Args:
      ssh_keys: generated GPU's one-time use SSH keys to be removed.
    Returns:
      A DAG node that cleaned up the ssh_keys.
    """
    return gpu.clean_up_ssh_keys(
        instance_name=self.existing_instance_name,
        ssh_keys=ssh_keys,
        gcp=self.task_gcp_config,
    )


# TODO(ranran): This class is big. Let's move it to a new file.
@dataclasses.dataclass
class GpuGkeTask(BaseTask):
  """This is a class to set up tasks for GPU on a GKE cluster.

  Attributes:
    task_test_config: task configutation.
    task_gcp_config: gcp related config (e.g., zone, project) for the task.
    cluster_name: Name of the GCP cluster.
    job_create_timeout: Amount of time to wait for all pods to become active.
    task_metric_config: metric configuration (e.g., result gcs path).
  """

  task_test_config: test_config.GpuGkeTest
  task_gcp_config: gcp_config.GCPConfig
  cluster_name: str
  job_create_timeout: datetime.timedelta = datetime.timedelta(minutes=10)
  task_metric_config: Optional[metric_config.MetricConfig] = None

  def run(self) -> DAGNode:
    """Run a test job and do post data process.

    Returns:
      A task group that runs the given test config on a GKE cluster.
    """
    with TaskGroup(
        group_id=self.task_test_config.benchmark_id, prefix_group_id=True
    ) as group:
      gcs_location = name_format.generate_gcs_folder_location(
          self.task_test_config.gcs_subfolder,
          self.task_test_config.benchmark_id,
      )

      job_body = self._get_job_manifest()

      gke_run = gke.run_job.override(group_id="run_model")(
          job_body,
          self.task_gcp_config,
          self.cluster_name,
          self.job_create_timeout,
          gcs_location,
      )
      post_process = self.post_process(gcs_location)
      gcs_location >> gke_run >> post_process
    return group

  def post_process(
      self, result_location: Optional[airflow.XComArg] = None
  ) -> DAGNode:
    """Process metrics and metadata, and insert them into BigQuery tables.

    Returns:
      A DAG node that executes the post process.
    """
    with TaskGroup(group_id="post_process") as group:
      process_id = metric.generate_process_id.override(retries=0)()
      metric.process_metrics.override(retries=0)(
          process_id,
          self.task_test_config,
          self.task_metric_config,
          self.task_gcp_config,
          folder_location=result_location,
      )
      return group

  def _get_job_manifest(self):
    # pylint: disable=line-too-long
    accelerator = self.task_test_config.accelerator
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "generateName": f"{self.task_test_config.test_name}",
            "labels": {
                "accelerator": accelerator.name,
                "benchmarkId": self.task_test_config.benchmark_id,
            },
        },
        "spec": {
            "activeDeadlineSeconds": int(
                self.task_test_config.timeout.total_seconds()
            )
            or 3600,
            "backoffLimit": 0,
            "completionMode": "Indexed",
            "completions": self.task_test_config.num_hosts,
            "parallelism": self.task_test_config.num_hosts,
            "template": {
                "metadata": {
                    # Matches `headless-svc` in GKE cluster.
                    # See deployments directory.
                    "labels": {"headless-svc": "true"},
                },
                "spec": {
                    "subdomain": "headless-svc",
                    "nodeSelector": {
                        "cloud.google.com/gke-accelerator": (
                            accelerator.accelerator_type
                        ),
                    },
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "main",
                            "image": self.task_test_config.docker_image,
                            "imagePullPolicy": "Always",
                            "command": shlex.split(
                                self.task_test_config.setup_script
                            ),
                            "args": shlex.split(
                                self.task_test_config.test_script
                            ),
                            "resources": {
                                "limits": {
                                    "nvidia.com/gpu": accelerator.count,
                                }
                            },
                            "env": [
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {
                                        "fieldRef": {
                                            "fieldPath": "metadata.name"
                                        }
                                    },
                                },
                                {
                                    "name": "POD_NAMESPACE",
                                    "valueFrom": {
                                        "fieldRef": {
                                            "fieldPath": "metadata.namespace"
                                        }
                                    },
                                },
                                {
                                    "name": "JOB_NAME",
                                    "valueFrom": {
                                        "fieldRef": {
                                            "fieldPath": (
                                                "metadata.labels['job-name']"
                                            )
                                        }
                                    },
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "mountPath": "/dev/shm",
                                    "name": "dshm",
                                    "readOnly": False,
                                },
                            ],
                        },
                    ],
                    "volumes": [
                        {"emptyDir": {"medium": "Memory"}, "name": "dshm"},
                    ],
                },
            },
        },
    }
