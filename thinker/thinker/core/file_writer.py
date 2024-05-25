# This file taken from
#    https://github.com/facebookresearch/torchbeast/
#        blob/main/torchbeast/core/file_writer.py
# and modified.

# Copyright (c) Facebook, Inc. and its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import csv
import datetime
import json
import logging
import os
import time
from typing import Dict
import warnings

def gather_metadata() -> Dict:
    date_start = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    # Gathering git metadata.
    try:
        import git

        try:
            repo = git.Repo(search_parent_directories=True)
            git_sha = repo.commit().hexsha
            git_data = dict(
                commit=git_sha,
                branch=None if repo.head.is_detached else repo.active_branch.name,
                # is_dirty=repo.is_dirty(),
                path=repo.git_dir,
            )
        except git.InvalidGitRepositoryError:
            git_data = None
    except ImportError:
        git_data = None
    # Gathering slurm metadata.
    if "SLURM_JOB_ID" in os.environ:
        slurm_env_keys = [k for k in os.environ if k.startswith("SLURM")]
        slurm_data = {}
        for k in slurm_env_keys:
            d_key = k.replace("SLURM_", "").replace("SLURMD_", "").lower()
            slurm_data[d_key] = os.environ[k]
    else:
        slurm_data = None
    return dict(
        date_start=date_start,
        date_end=None,
        successful=False,
        git=git_data,
        slurm=slurm_data,
        env=os.environ.copy(),
    )


class FileWriter:
    def __init__(
        self,
        xpid: str = None,
        xp_args: dict = None,
        rootdir: str = "~/logs",
        symlink_to_latest: bool = True,
        suffix: str = "",
        overwrite: bool = False,
    ):
        if not xpid:
            # Make unique id.
            xpid = "{proc}_{unixtime}".format(
                proc=os.getpid(), unixtime=int(time.time())
            )
        self.xpid = xpid
        self._tick = 0

        # Metadata gathering.
        if xp_args is None:
            xp_args = {}
        self.metadata = gather_metadata()
        # We need to copy the args, otherwise when we close the file writer
        # (and rewrite the args) we might have non-serializable objects (or
        # other unwanted side-effects).
        self.metadata["args"] = copy.deepcopy(xp_args)
        self.metadata["xpid"] = self.xpid

        formatter = logging.Formatter("%(message)s")
        self._logger = logging.getLogger("logs/out")

        # To stdout handler.
        if not self._logger.hasHandlers():
            shandle = logging.StreamHandler()
            shandle.setFormatter(formatter)
            self._logger.addHandler(shandle)
            self._logger.setLevel(logging.INFO)

        rootdir = os.path.expandvars(os.path.expanduser(rootdir))
        # To file handler.
        self.basepath = os.path.join(rootdir, self.xpid)
        if not os.path.exists(self.basepath):
            self._logger.info("Creating log directory: %s", self.basepath)
            os.makedirs(self.basepath, exist_ok=True)
        else:
            pass
            #self._logger.info("Found log directory: %s", self.basepath)

        if symlink_to_latest:
            # Add 'latest' as symlink unless it exists and is no symlink.
            symlink = os.path.join(rootdir, "latest")
            try:
                if os.path.islink(symlink):
                    os.remove(symlink)
                if not os.path.exists(symlink):
                    os.symlink(self.basepath, symlink)
                    self._logger.info("Symlinked log directory: %s", symlink)
            except OSError:
                # os.remove() or os.symlink() raced. Don't do anything.
                pass

        self.paths = dict(
            msg="{base}/out{suffix}.log".format(base=self.basepath, suffix=suffix),
            logs="{base}/logs{suffix}.csv".format(base=self.basepath, suffix=suffix),
            meta="{base}/meta{suffix}.json".format(base=self.basepath, suffix=suffix),
        )

        self._logger.info("Saving arguments to %s", self.paths["meta"])
        if os.path.exists(self.paths["meta"]):
            if not overwrite:
                self._logger.warning(
                    "Path to meta file already exists. " "Not overriding meta."
                )
            else:
                os.remove(self.paths["meta"])
                self._save_metadata()
        else:
            self._save_metadata()

        self._logger.info("Saving messages to %s", self.paths["msg"])
        if os.path.exists(self.paths["msg"]):
            self._logger.warning(
                "Path to message file already exists. " "New data will be appended."
            )

        fhandle = logging.FileHandler(self.paths["msg"])
        fhandle.setFormatter(formatter)
        self._logger.addHandler(fhandle)

        self._logger.info("Saving logs data to %s", self.paths["logs"])
        self.fieldnames = ["_tick", "_time"]
        if os.path.exists(self.paths["logs"]):
            self._logger.warning(
                "Path to log file already exists. " "New data will be appended."
            )
            # Override default tick: use the last tick from the logs file plus 1.
            with open(self.paths["logs"], "r") as csvfile:
                reader = csv.reader(csvfile)
                lines = list(reader)
                # Need at least two lines in order to read the last tick:
                # the first is the csv header and the second is the first line
                # of data.
                if len(lines) > 0:
                    self.fieldnames = lines[0]
                    if "# _tick" in self.fieldnames:
                        self.fieldnames = [x if x != "# _tick" else "_tick" for x in self.fieldnames]
                if len(lines) >= 2 and len(lines[-2]) > 0:
                    try:
                        self._tick = int(lines[-2][0]) + 1
                    except Exception as e:
                        warnings.warn(f"Warning: An exception occurred: {e}")
            
        self._logfile = open(self.paths["logs"], "a")
        self._logwriter = csv.DictWriter(self._logfile, fieldnames=self.fieldnames)

    def log(self, to_log: Dict, verbose: bool = False) -> None:     
        to_log["_tick"] = self._tick
        self._tick += 1
        to_log["_time"] = time.time()        

        old_len = len(self.fieldnames)
        for k in to_log:
            if k not in self.fieldnames:
                self.fieldnames.append(k)

        if to_log["_tick"] == 0:
            self._logfile.write("%s\n" % ",".join(self.fieldnames))
            self.fieldnames                

        if verbose:
            self._logger.info(
                "LOG | %s",
                ", ".join(["{}: {}".format(k, to_log[k]) for k in sorted(to_log)]),
            )

        self._logwriter.writerow(to_log)
        self._logfile.flush()

    def close(self, successful: bool = True) -> None:
        self.metadata["date_end"] = datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        self.metadata["successful"] = successful
        self._save_metadata()

        self._logfile.close()

    def _save_metadata(self) -> None:
        with open(self.paths["meta"], "w") as jsonfile:
            json.dump(self.metadata, jsonfile, indent=4, sort_keys=True)
