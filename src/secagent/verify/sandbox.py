"""Run untrusted code, because that is exactly what a generated test is.

READ THIS BEFORE SIMPLIFYING ANY OF IT TO A PLAIN `subprocess.run`.

Everything else in secagent guards what goes INTO the model — `wrap_untrusted`,
`harden_system_prompt`, the marker-forgery stripping. Nothing guarded executing what comes
OUT, because until now nothing executed it. A generated test is a file of model-authored
code that this harness compiles and runs. It can delete files, open sockets, fork-bomb,
read anything the process can read, or simply never return. It is hostile by default and
the fact that it usually is not changes nothing.

So: containers, always, with the network off. There is deliberately NO local-execution
implementation in this module — not an unsafe one behind a flag, not one "for tests". If
the runtime is unavailable the harness reports `unverifiable` and checks nothing. Refusing
to check is a fine outcome; checking by running hostile code on the host is not.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# Images that already exist in this project. C/C++ uses the analysis image because it
# already carries clang++/g++/cmake for IKOS; nothing new is built to run these gates.
IMAGES = {
    # A dedicated runner, not the agent image: the agent image has no pytest, and with
    # --network none there is no install step at run time. See docker/verify.Dockerfile.
    "Python": "secagent-verify:latest",
    "C": "secagent-uc3:latest",
    "C++": "secagent-uc3:latest",
}


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Sandbox(Protocol):
    """Run ``argv`` with ``workdir`` as the working directory, and come back."""

    def run(self, workdir: Path, argv: list[str], language: str,
            timeout_s: float) -> RunResult: ...

    def available(self, language: str) -> bool: ...


class DockerSandbox:
    """The only real implementation. Every flag here is load-bearing."""

    def __init__(self, runtime: str = "docker", memory: str = "1g",
                 pids: int = 256, cpus: str = "2") -> None:
        self.runtime = runtime
        self.memory = memory
        self.pids = pids
        self.cpus = cpus

    def available(self, language: str) -> bool:
        if language not in IMAGES or not shutil.which(self.runtime):
            return False
        probe = subprocess.run(  # noqa: S603
            [self.runtime, "image", "inspect", IMAGES[language]],
            capture_output=True, text=True, check=False)
        return probe.returncode == 0

    def command(self, workdir: Path, argv: list[str], language: str) -> list[str]:
        """The full container command. Split out so the containment flags are TESTABLE.

        The previous test reimplemented this list and asserted on its own copy, which
        passes whatever the real one does — it did not notice that argv was appended to
        the image's ENTRYPOINT rather than replacing it, so every Python run died with
        "sh: 0: cannot open sh" and was scored `wrong`: a verdict about our own
        invocation, reported as a verdict about the test.
        """
        return [
            self.runtime, "run", "--rm",
            # No network. A test that needs the network is a test we decline to run:
            # it could exfiltrate the repo, and it makes the result depend on the world.
            "--network", "none",
            # A fork bomb in generated code should cost one container, not the host.
            "--pids-limit", str(self.pids),
            "--memory", self.memory, "--cpus", self.cpus,
            # Nothing here needs to be root, and a container escape by a root process is
            # worth more to an attacker than one by nobody.
            "--user", "65534:65534",
            # The scratch tree is mounted writable ON PURPOSE — compilers write objects —
            # but it is a COPY. `harness` never hands the caller's repo to this method.
            "-v", f"{workdir}:/w", "-w", "/w",
            # Override the image's ENTRYPOINT rather than appending to it.
            "--entrypoint", argv[0],
            IMAGES[language], *argv[1:],
        ]

    def run(self, workdir: Path, argv: list[str], language: str,
            timeout_s: float) -> RunResult:
        if language not in IMAGES:
            return RunResult(127, "", f"no sandbox image for {language}")
        try:
            proc = subprocess.run(  # noqa: S603
                self.command(workdir, argv, language),
                capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            # A hang is a verdict, not a stall. `--rm` reaps the container.
            return RunResult(124, "", f"timed out after {timeout_s}s", timed_out=True)
        return RunResult(proc.returncode, proc.stdout[-8000:], proc.stderr[-8000:])
