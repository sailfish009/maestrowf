###############################################################################
# Copyright (c) 2017, Lawrence Livermore National Security, LLC.
# Produced at the Lawrence Livermore National Laboratory
# Written by Francesco Di Natale, dinatale3@llnl.gov.
#
# LLNL-CODE-734340
# All rights reserved.
# This file is part of MaestroWF, Version: 1.0.0.
#
# For details, see https://github.com/LLNL/maestrowf.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
###############################################################################

"""Flux Scheduler interface implementation."""
from datetime import datetime
import logging
import os
import re
import json
import subprocess as sp

from maestrowf.abstracts.interfaces import SchedulerScriptAdapter
from maestrowf.abstracts.enums import JobStatusCode, State, SubmissionCode, \
    CancelCode

LOGGER = logging.getLogger(__name__)
status_re = re.compile(r"Job \d+ status: (.*)$")
# env_filter = re.compile(r"^(SSH_|LSF)")
env_filter = re.compile(r"^SSH_")


def get_environment():
    """Filter environment variables based on a naming filter."""
    env = dict()
    for key in os.environ:
        if env_filter.match(key):
            continue
        env[key] = os.environ[key]
    env.pop("HOSTNAME", None)
    env.pop("ENVIRONMENT", None)
    # Make MVAPICH behave...
    env["MPIRUN_RSH_LAUNCH"] = "1"
    return env


class SpectrumFluxScriptAdapter(SchedulerScriptAdapter):
    """Interface class for the flux scheduler (on Spectrum MPI)."""

    key = "flux-spectrum"

    def __init__(self, **kwargs):
        """
        Initialize an instance of the FluxScriptAdapter.

        The FluxScriptAdapter is this package interface to the Flux
        scheduler. This adapter constructs Flux scripts for a StudyStep based
        on user set defaults and local settings present in each step.

        The expected keyword arguments that are expected when the Flux adapter
        is instantiated are as follows:
        * host: The cluster to execute scripts on.
        * bank: The account to charge computing time to.
        * queue: Scheduler queue scripts should be submitted to.
        * nodes: The number of compute nodes to be reserved for computing.

        :param **kwargs: A dictionary with default settings for the adapter.
        """
        super(SpectrumFluxScriptAdapter, self).__init__(**kwargs)

        # NOTE: These libraries are compiled at runtime when an allocation
        # is spun up.
        self.flux = __import__("flux")
        self.kvs = __import__("flux.kvs", globals(), locals(), ["kvs"])

        # NOTE: Host doesn"t seem to matter for FLUX. sbatch assumes that the
        # current host is where submission occurs.
        self.add_batch_parameter("nodes", kwargs.pop("nodes", "1"))
        self._mpi_exe = kwargs.pop("mpi")
        self._addl_args = kwargs.pop("args", [])

        # Header is only for informational purposes.
        self._header = {
            "nodes": "#SBATCH -N {nodes}",
            "walltime": "#SBATCH -t {walltime}",
        }

        self._cmd_flags = {
            "cmd": "mpirun",
            "ntasks": "-n",
            "nodes": "-N",
        }
        self._extension = "flux.sh"
        self.h = None

    @property
    def extension(self):
        return self._extension

    def _convert_walltime_to_seconds(self, walltime):
        # Convert walltime to seconds.
        wt = \
            (datetime.strptime(walltime, "%H:%M:%S") - datetime(1900, 1, 1))
        return int(wt.total_seconds())

    def get_header(self, step):
        """
        Generate the header present at the top of Flux execution scripts.

        :param step: A StudyStep instance.
        :returns: A string of the header based on internal batch parameters and
                  the parameter step.
        """
        run = dict(step.run)
        batch_header = dict(self._batch)
        batch_header["walltime"] = \
            str(self._convert_walltime_to_seconds(step.run["walltime"]))

        if run["nodes"]:
            batch_header["nodes"] = run.pop("nodes")
        batch_header["job-name"] = step.name.replace(" ", "_")
        batch_header["comment"] = step.description.replace("\n", " ")

        modified_header = [self._exec]
        for key, value in self._header.items():
            # If we"re looking at the bank and the reservation header exists,
            # skip the bank to prefer the reservation.
            if key == "bank" and "reservation" in self._batch:
                continue
            modified_header.append(value.format(**batch_header))
        modified_header.append("HOSTF_SINGLE=$(mktemp /tmp/hostls-XXXXX)")
        modified_header.append("HOSTF=$(mktemp /tmp/hostl-XXXXX)")
        if step.run["nodes"] > 1:
            modified_header.append("instance-nodes > $HOSTF_SINGLE")
        else:
            modified_header.append("echo localhost > $HOSTF_SINGLE")
        modified_header.append("""sed -e "s/$/:44/" $HOSTF_SINGLE > $HOSTF """)
        modified_header.append("""ulimit -s 10240""")

        return "\n".join(modified_header)

    def get_parallelize_command(self, procs, nodes=None, **kwargs):
        """
        Generate the FLUX parallelization segement of the command line.

        :param procs: Number of processors to allocate to the parallel call.
        :param nodes: Number of nodes to allocate to the parallel call
                      (default = 1).
        :returns: A string of the parallelize command configured using nodes
                  and procs.
        """
        args = [
            "env",
            "-u", "FLUX_JOB_ID",
            "-u", "PMI_FD",
            "-u", "PMI_RANK",
            "-u", "PMI_SIZE",
            self._mpi_exe]

        for item in self._addl_args:
            args.append(item)

        args.extend(["-hostfile", "$HOSTF"])
        args.extend([
            "-n",
            str(procs),
            ])
        return " ".join(args)

    def submit(self, step, path, cwd, job_map=None, env=None):
        """
        Submit a script to the Flux scheduler.

        :param step: The StudyStep instance this submission is based on.
        :param path: Local path to the script to be executed.
        :param cwd: Path to the current working directory.
        :param job_map: A dictionary mapping step names to their job
                        identifiers.
        :param env: A dict containing a modified environment for execution.
        :returns: The return status of the submission command and job
                  identiifer.
        """
        # # Leading command is "sbatch"
        # cmd = ["flux", "submit"]

        # # Append the script path and working directory.
        # cmd += ["-N", str(step.run["nodes"])]
        # cmd += ["-n", "1"]
        # cmd += ["-T", step.run["walltime"]]
        # cmd += ["-O", os.path.join(cwd,"job-{{id}}.out")
        # cmd += [path]
        # cmd = " ".join(cmd)

        # # LOGGER.debug("cwd = %s", cwd)
        # LOGGER.debug("Command to execute: %s", cmd)
        # p = Popen(
        #       cmd, shell=True, stdout=PIPE, stderr=PIPE, cwd=cwd, env=env)
        # output, err = p.communicate()
        # retcode = p.wait()

        # # TODO: We need to check for dependencies here. The sbatch is where
        # # dependent batch jobs are specified. If we"re trying to launch
        # # everything at once then that should happen here.

        # if retcode == 0:
        #     LOGGER.info("Submission returned status OK.")
        #     return SubmissionCode.OK, re.search("[0-9]+", output).group(0)
        # else:
        #     LOGGER.warning("Submission returned an error.")
        #     return SubmissionCode.ERROR, -1

        walltime = self._convert_walltime_to_seconds(step.run["walltime"])
        cores_per_task = step.run.get("cores per task", 1)
        jobspec = {
            "nnodes": step.run["nodes"],
            # NOTE: interface doesn"t allow multiple here yet
            "ntasks":   step.run["nodes"],
            "ncores":   cores_per_task * step.run["procs"],
            "gpus":     step.run.get("gpus", 0),
            "environ":  get_environment(),          # TODO: revisit
            "options":  {"stdio-delay-commit": 1},
            "opts": {
                "nnodes": step.run["nodes"],
                "ntasks": step.run["nodes"],
                "cores-per-task": step.run["cores per task"],
                "tasks-per-node": 1,
            },
            # "environ": {"PATH" : os.environ["PATH"]},
            "cwd": cwd,
            "walltime": walltime,
            # "output" : {
            #   "files" : {
            #     "stdout" : os.path.join(cwd,step.name + "-{{id}}.out"),
            #     "stderr" : os.path.join(cwd,step.name + "-{{id}}.err"),
            #     },
            #   },
        }
        LOGGER.debug("Submission Spec -- \n%s", jobspec)
        if step.run["nodes"] > 1:
            jobspec["cmdline"] = ["flux", "broker", path]
        else:
            jobspec["cmdline"] = [path]
        if self.h is None:
            self.h = self.flux.Flux()
        resp = self.h.rpc_send("job.submit", json.dumps(jobspec))
        if resp is None:
            LOGGER.warning("RPC response invalid")
            return SubmissionCode.ERROR, -1
        if resp.get("errnum", None) is not None:
            LOGGER.warning("Job creation failed with error code {}".format(
                resp["errnum"]))
            return SubmissionCode.ERROR, -1
        if resp.get("state", None) != "submitted":
            LOGGER.warning("Job creation failed")
            return SubmissionCode.ERROR, -1

        LOGGER.info("Submission returned status OK. -- "
                    "Assigned identifier (%s)", resp["jobid"])
        return SubmissionCode.OK, resp["jobid"]

    def check_jobs(self, joblist):
        """
        For the given job list, query execution status.

        This method uses the scontrol show job <jobid> command and does a
        regex search for job information.

        :param joblist: A list of job identifiers to be queried.
        :returns: The return code of the status query, and a dictionary of job
                  identifiers to their status.
        """
        LOGGER.debug("Joblist type -- %s", type(joblist))
        LOGGER.debug("Joblist contents -- %s", joblist)
        if not joblist:
            LOGGER.debug("Empty job list specified.")
            return JobStatusCode.OK, {}
        if not isinstance(joblist, list):
            LOGGER.debug("Specified parameter is not a list.")
            if isinstance(joblist, int):
                LOGGER.debug("Integer found.")
                joblist = [joblist]
            else:
                LOGGER.debug("Unknown type. Returning an error.")
                return JobStatusCode.ERROR, {}

        if self.h is None:
            LOGGER.debug("Class instance is None. Initializing a new Flux "
                         "instance.")
            self.h = self.flux.Flux()

        resp = self.h.rpc_send("job.kvspath", json.dumps({"ids": joblist}))
        paths = resp["paths"]
        status = {}
        for jobid in joblist:
            status[jobid] = None
        for i in range(0, len(joblist)):
            jobid = joblist[i]
            path = paths[i]
            LOGGER.debug("Checking jobid %s", jobid)
            try:
                flux_state = str(self.kvs.get(self.h, path + ".state"))
                # "complete" covers three cases:
                # 1. Normal exit
                # 2. Killed via signal
                # 3. Failure in execution
                LOGGER.debug("Encountered '%d' with state '%s'",
                             i, flux_state)
                if flux_state == "complete":
                    flux_status = self.kvs.get(self.h, path + ".exit_status")
                    # Use kvs to grab the max error code encountered.
                    rcode = flux_status["max"]
                    LOGGER.debug("State 'complete' found. Exit code -- %s",
                                 rcode)
                    # If retcode is not 0, not normal execution
                    if rcode != 0:
                        # If retcode is in the signaled set, we cancelled.
                        if os.WIFSIGNALED(rcode):
                            LOGGER.debug(
                                "Return code -- %d (WIFSIGNALED)", rcode
                            )
                            flux_state = "killed"
                        # Otherwise, something abnormal happened.
                        else:
                            LOGGER.debug(
                                "Return code -- %d (failed)", rcode
                            )
                            flux_state = "failed"
                    # Otherwise, completed normally.
                    else:
                        LOGGER.debug(
                            "Return code -- %d (complete)", rcode
                        )
                        flux_state = "complete"

                status[jobid] = self._state(flux_state)
                LOGGER.debug(
                    "Returned code for state (%s) -- %s",
                    flux_state, status[jobid]
                )
            except IOError:
                LOGGER.error(
                    "Error seen on path {} Unexpected behavior encountered."
                    .format(path)
                )
                # NOTE: I don't know if we should actually be returning here.
                # It feels like we may not want to.
                return JobStatusCode.ERROR, status
            except EnvironmentError:
                LOGGER.warning("Job ID (%s) not found in kvs. Setting state"
                               "to UNKNOWN.", jobid)
                status[jobid] = self._state("unknown")

        if not status:
            return JobStatusCode.NOJOBS, status
        else:
            return JobStatusCode.OK, status

    def cancel_jobs(self, joblist):
        """
        For the given job list, cancel each job.

        :param joblist: A list of job identifiers to be cancelled.
        :returns: The return code to indicate if jobs were cancelled.
        """
        # If we don"t have any jobs to check, just return status OK.
        if not joblist:
            return CancelCode.OK

        cancelcode = CancelCode.OK

        term_status = set([State.FINISHED, State.CANCELLED, State.FAILED])
        with open(os.devnull, "w") as FNULL:
            for job in joblist:
                LOGGER.debug("Cancelling JobID = %s", job)
                retcode = sp.call(
                    ["flux", "wreck", "cancel", str(job)],
                    stdout=FNULL, stderr=FNULL
                )

                if retcode != 0:
                    LOGGER.debug("'flux wreck cancel' failed, trying kill.")
                    retcode = sp.call(
                        ["flux", "wreck", "kill", str(job)],
                        stdout=FNULL, stderr=FNULL
                    )

                if retcode != 0:
                    LOGGER.debug("'flux wreck kill' failed, checking status.")
                    retcode, status = self.check_jobs([job])
                    if status and status.get(job, None) in term_status:
                        retcode = 0

                if retcode != 0:
                    LOGGER.warning("Error code '{}' seen. Unexpected behavior "
                                   "encountered.".format(retcode))
                    cancelcode = CancelCode.ERROR
        return cancelcode

    def _state(self, flux_state):
        """
        Map a scheduler specific job state to a Study.State enum.

        :param flux_state: String representation of scheduler job status.
        :returns: A Study.State enum corresponding to parameter job_state.
        """
        LOGGER.debug("Received FLUX State -- %s", flux_state)
        if flux_state == "running":
            return State.RUNNING
        elif flux_state == "pending" or flux_state == "runrequest" \
                or flux_state == "allocated" or flux_state == "starting":
            return State.PENDING
        elif flux_state == "submitted":
            return State.WAITING
        elif flux_state == "failed":
            return State.FAILED
        elif flux_state == "cancelled" or flux_state == "killed":
            return State.CANCELLED
        elif flux_state == "complete":
            return State.FINISHED
        elif flux_state == "unknown":
            return State.UNKNOWN
        else:
            return State.UNKNOWN

    def _write_script(self, ws_path, step):
        """
        Write a Flux script to the workspace of a workflow step.

        The job_map optional parameter is a map of workflow step names to job
        identifiers. This parameter so far is only planned to be used when a
        study is configured to be launched in one go (more or less a script
        chain using a scheduler dependency setting). The functionality of
        the parameter may change depending on both future intended use.

        :param ws_path: Path to the workspace directory of the step.
        :param step: An instance of a StudyStep.
        :returns: Boolean value (True if to be scheduled), the path to the
        written script for run["cmd"], and the path to the script written for
        run["restart"] (if it exists).
        """
        to_be_scheduled, cmd, restart = self.get_scheduler_command(step)

        fname = "{}.{}".format(step.name, self._extension)
        script_path = os.path.join(ws_path, fname)
        with open(script_path, "w") as script:
            if to_be_scheduled:
                script.write(self.get_header(step))
            else:
                script.write(self._exec)

            cmd = "\n\n{}\n".format(cmd)
            script.write(cmd)

        if restart:
            rname = "{}.restart.{}".format(step.name, self._extension)
            restart_path = os.path.join(ws_path, rname)

            with open(restart_path, "w") as script:
                if to_be_scheduled:
                    script.write(self.get_header(step))
                else:
                    script.write(self._exec)

                cmd = "\n\n{}\n".format(restart)
                script.write(cmd)
        else:
            restart_path = None

        return to_be_scheduled, script_path, restart_path


class FluxScriptAdapter(SchedulerScriptAdapter):
    """Interface class for the flux scheduler (on Spectrum MPI)."""

    key = "flux"

    def __init__(self, **kwargs):
        """
        Initialize an instance of the FluxScriptAdapter.

        The FluxScriptAdapter is this package interface to the Flux
        scheduler. This adapter constructs Flux scripts for a StudyStep based
        on user set defaults and local settings present in each step.

        The expected keyword arguments that are expected when the Flux adapter
        is instantiated are as follows:
        * host: The cluster to execute scripts on.
        * bank: The account to charge computing time to.
        * queue: Scheduler queue scripts should be submitted to.
        * nodes: The number of compute nodes to be reserved for computing.

        :param **kwargs: A dictionary with default settings for the adapter.
        """
        super(FluxScriptAdapter, self).__init__(**kwargs)

        # NOTE: These libraries are compiled at runtime when an allocation
        # is spun up.
        self.flux = __import__("flux")
        self.kvs = __import__("flux.kvs", globals(), locals(), ["kvs"])

        # NOTE: Host doesn"t seem to matter for FLUX. sbatch assumes that the
        # current host is where submission occurs.
        self.add_batch_parameter("nodes", kwargs.pop("nodes", "1"))
        self._addl_args = kwargs.get("args", [])

        # Header is only for informational purposes.
        self._header = {
            "nodes": "#INFO (nodes) {nodes}",
            "walltime": "#INFO (walltime) {walltime}",
        }

        self._cmd_flags = {
            "ntasks": "-n",
            "nodes": "-N",
        }
        self._extension = "flux.sh"
        self.h = None

    @property
    def extension(self):
        return self._extension

    def _convert_walltime_to_seconds(self, walltime):
        # Convert walltime to seconds.
        wt = \
            (datetime.strptime(walltime, "%H:%M:%S") - datetime(1900, 1, 1))
        return int(wt.total_seconds())

    def get_header(self, step):
        """
        Generate the header present at the top of Flux execution scripts.

        :param step: A StudyStep instance.
        :returns: A string of the header based on internal batch parameters and
                  the parameter step.
        """
        run = dict(step.run)
        batch_header = dict(self._batch)
        batch_header["walltime"] = \
            str(self._convert_walltime_to_seconds(step.run["walltime"]))

        if run["nodes"]:
            batch_header["nodes"] = run.pop("nodes")
        batch_header["job-name"] = step.name.replace(" ", "_")
        batch_header["comment"] = step.description.replace("\n", " ")

        modified_header = [self._exec]
        for key, value in self._header.items():
            # If we"re looking at the bank and the reservation header exists,
            # skip the bank to prefer the reservation.
            if key == "bank" and "reservation" in self._batch:
                continue
            modified_header.append(value.format(**batch_header))

        return "\n".join(modified_header)

    def get_parallelize_command(self, procs, nodes=None, **kwargs):
        """
        Generate the FLUX parallelization segement of the command line.

        :param procs: Number of processors to allocate to the parallel call.
        :param nodes: Number of nodes to allocate to the parallel call
                      (default = 1).
        :returns: A string of the parallelize command configured using nodes
                  and procs.
        """
        args = ["flux", "wreckrun", "-n", str(procs)]

        # if we've specified nodes, add that to wreckrun
        args.append("-N")
        args.append(str(nodes))

        # flux has additional arguments that can be passed via the '-o' flag.
        if self._addl_args:
            addtl = ["-o"] + self._addl_args
            args.append(",".join(addtl))

        return " ".join(args)

    def submit(self, step, path, cwd, job_map=None, env=None):
        """
        Submit a script to the Flux scheduler.

        :param step: The StudyStep instance this submission is based on.
        :param path: Local path to the script to be executed.
        :param cwd: Path to the current working directory.
        :param job_map: A dictionary mapping step names to their job
                        identifiers.
        :param env: A dict containing a modified environment for execution.
        :returns: The return status of the submission command and job
                  identiifer.
        """
        walltime = self._convert_walltime_to_seconds(step.run["walltime"])
        nodes = step.run.get("nodes")
        processors = step.run.get("procs", 0)

        # Compute cores per task
        cores_per_task = step.run.get("cores per task", 1)
        if not cores_per_task:
            cores_per_task = 1

        # Calculate ngpus
        ngpus = step.run.get("gpus", 0)
        if not ngpus:
            ngpus = 0

        # Calculate nprocs
        ncores = cores_per_task * nodes
        # Check to make sure that cores_per_task matches if processors
        # is specified.
        if processors > 0 and processors != ncores:
            msg = "Calculated ncores (nodes * cores per task) = {} " \
                  "-- procs = {}".format(ncores, processors)
            LOGGER.error(msg)
            raise ValueError(msg)

        # Raise an exception if ncores is 0
        if ncores <= 0:
            msg = "Invalid number of cores specified. " \
                  "Aborting. (ncores = {})".format(ncores)
            LOGGER.error(msg)
            raise ValueError(msg)

        jobspec = {
            "nnodes": step.run["nodes"],
            # NOTE: interface doesn"t allow multiple here yet
            "ntasks":   nodes,
            "ncores":   ncores,
            "ngpus":    ngpus,
            "environ":  get_environment(),          # TODO: revisit
            "options":  {"stdio-delay-commit": 1},
            "opts": {
                "nnodes": nodes,
                "ntasks": nodes,
                "cores-per-task": cores_per_task,
                "tasks-per-node": 1,
            },
            # "environ": {"PATH" : os.environ["PATH"]},
            "cwd": cwd,
            "walltime": walltime,
            # "output" : {
            #   "files" : {
            #     "stdout" : os.path.join(cwd,step.name + "-{{id}}.out"),
            #     "stderr" : os.path.join(cwd,step.name + "-{{id}}.err"),
            #     },
            #   },
        }
        LOGGER.debug("Submission Spec -- \n%s", jobspec)
        jobspec["cmdline"] = ["flux", "broker", path]

        if self.h is None:
            self.h = self.flux.Flux()
        resp = self.h.rpc_send("job.submit", json.dumps(jobspec))
        if resp is None:
            LOGGER.warning("RPC response invalid")
            return SubmissionCode.ERROR, -1
        if resp.get("errnum", None) is not None:
            LOGGER.warning("Job creation failed with error code {}".format(
                resp["errnum"]))
            return SubmissionCode.ERROR, -1
        if resp.get("state", None) != "submitted":
            LOGGER.warning("Job creation failed")
            return SubmissionCode.ERROR, -1

        LOGGER.info("Submission returned status OK. -- "
                    "Assigned identifier (%s)", resp["jobid"])
        return SubmissionCode.OK, resp["jobid"]

    def check_jobs(self, joblist):
        """
        For the given job list, query execution status.

        This method uses the scontrol show job <jobid> command and does a
        regex search for job information.

        :param joblist: A list of job identifiers to be queried.
        :returns: The return code of the status query, and a dictionary of job
                  identifiers to their status.
        """
        LOGGER.debug("Joblist type -- %s", type(joblist))
        LOGGER.debug("Joblist contents -- %s", joblist)
        if not joblist:
            LOGGER.debug("Empty job list specified.")
            return JobStatusCode.OK, {}
        if not isinstance(joblist, list):
            LOGGER.debug("Specified parameter is not a list.")
            if isinstance(joblist, int):
                LOGGER.debug("Integer found.")
                joblist = [joblist]
            else:
                LOGGER.debug("Unknown type. Returning an error.")
                return JobStatusCode.ERROR, {}

        if self.h is None:
            LOGGER.debug("Class instance is None. Initializing a new Flux "
                         "instance.")
            self.h = self.flux.Flux()

        resp = self.h.rpc_send("job.kvspath", json.dumps({"ids": joblist}))
        paths = resp["paths"]
        status = {}
        for jobid in joblist:
            status[jobid] = None
        for i in range(0, len(joblist)):
            jobid = joblist[i]
            path = paths[i]
            LOGGER.debug("Checking jobid %s", jobid)
            try:
                flux_state = str(self.kvs.get(self.h, path + ".state"))
                # "complete" covers three cases:
                # 1. Normal exit
                # 2. Killed via signal
                # 3. Failure in execution
                LOGGER.debug("Encountered '%d' with state '%s'",
                             i, flux_state)
                if flux_state == "complete":
                    flux_status = self.kvs.get(self.h, path + ".exit_status")
                    # Use kvs to grab the max error code encountered.
                    rcode = flux_status["max"]
                    LOGGER.debug("State 'complete' found. Exit code -- %s",
                                 rcode)
                    # If retcode is not 0, not normal execution
                    if rcode != 0:
                        # If retcode is in the signaled set, we cancelled.
                        if os.WIFSIGNALED(rcode):
                            LOGGER.debug(
                                "Return code -- %d (WIFSIGNALED)", rcode
                            )
                            flux_state = "killed"
                        # Otherwise, something abnormal happened.
                        else:
                            LOGGER.debug(
                                "Return code -- %d (failed)", rcode
                            )
                            flux_state = "failed"
                    # Otherwise, completed normally.
                    else:
                        LOGGER.debug(
                            "Return code -- %d (complete)", rcode
                        )
                        flux_state = "complete"

                status[jobid] = self._state(flux_state)
                LOGGER.debug(
                    "Returned code for state (%s) -- %s",
                    flux_state, status[jobid]
                )
            except IOError:
                LOGGER.error(
                    "Error seen on path {} Unexpected behavior encountered."
                    .format(path)
                )
                # NOTE: I don't know if we should actually be returning here.
                # It feels like we may not want to.
                return JobStatusCode.ERROR, status
            except EnvironmentError:
                LOGGER.warning("Job ID (%s) not found in kvs. Setting state"
                               "to UNKNOWN.", jobid)
                status[jobid] = self._state("unknown")

        if not status:
            return JobStatusCode.NOJOBS, status
        else:
            return JobStatusCode.OK, status

    def cancel_jobs(self, joblist):
        """
        For the given job list, cancel each job.

        :param joblist: A list of job identifiers to be cancelled.
        :returns: The return code to indicate if jobs were cancelled.
        """
        # If we don"t have any jobs to check, just return status OK.
        if not joblist:
            return CancelCode.OK

        cancelcode = CancelCode.OK

        term_status = set([State.FINISHED, State.CANCELLED, State.FAILED])
        with open(os.devnull, "w") as FNULL:
            for job in joblist:
                LOGGER.debug("Cancelling JobID = %s", job)
                retcode = sp.call(
                    ["flux", "wreck", "cancel", str(job)],
                    stdout=FNULL, stderr=FNULL
                )

                if retcode != 0:
                    LOGGER.debug("'flux wreck cancel' failed, trying kill.")
                    retcode = sp.call(
                        ["flux", "wreck", "kill", str(job)],
                        stdout=FNULL, stderr=FNULL
                    )

                if retcode != 0:
                    LOGGER.debug("'flux wreck kill' failed, checking status.")
                    retcode, status = self.check_jobs([job])
                    if status and status.get(job, None) in term_status:
                        retcode = 0

                if retcode != 0:
                    LOGGER.warning("Error code '{}' seen. Unexpected behavior "
                                   "encountered.".format(retcode))
                    cancelcode = CancelCode.ERROR
        return cancelcode

    def _state(self, flux_state):
        """
        Map a scheduler specific job state to a Study.State enum.

        :param flux_state: String representation of scheduler job status.
        :returns: A Study.State enum corresponding to parameter job_state.
        """
        LOGGER.debug("Received FLUX State -- %s", flux_state)
        if flux_state == "running":
            return State.RUNNING
        elif flux_state == "pending" or flux_state == "runrequest" \
                or flux_state == "allocated" or flux_state == "starting":
            return State.PENDING
        elif flux_state == "submitted":
            return State.WAITING
        elif flux_state == "failed":
            return State.FAILED
        elif flux_state == "cancelled" or flux_state == "killed":
            return State.CANCELLED
        elif flux_state == "complete":
            return State.FINISHED
        elif flux_state == "unknown":
            return State.UNKNOWN
        else:
            return State.UNKNOWN

    def _write_script(self, ws_path, step):
        """
        Write a Flux script to the workspace of a workflow step.

        The job_map optional parameter is a map of workflow step names to job
        identifiers. This parameter so far is only planned to be used when a
        study is configured to be launched in one go (more or less a script
        chain using a scheduler dependency setting). The functionality of
        the parameter may change depending on both future intended use.

        :param ws_path: Path to the workspace directory of the step.
        :param step: An instance of a StudyStep.
        :returns: Boolean value (True if to be scheduled), the path to the
                  written script for run["cmd"], and the path to the script
                  written for run["restart"] (if it exists).
        """
        to_be_scheduled, cmd, restart = self.get_scheduler_command(step)

        fname = "{}.{}".format(step.name, self._extension)
        script_path = os.path.join(ws_path, fname)
        with open(script_path, "w") as script:
            if to_be_scheduled:
                script.write(self.get_header(step))
            else:
                script.write(self._exec)

            cmd = "\n\n{}\n".format(cmd)
            script.write(cmd)

        if restart:
            rname = "{}.restart.{}".format(step.name, self._extension)
            restart_path = os.path.join(ws_path, rname)

            with open(restart_path, "w") as script:
                if to_be_scheduled:
                    script.write(self.get_header(step))
                else:
                    script.write(self._exec)

                cmd = "\n\n{}\n".format(restart)
                script.write(cmd)
        else:
            restart_path = None

        return to_be_scheduled, script_path, restart_path
