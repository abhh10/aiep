"""The restricted egress proxy container: pin, policy context, injection, and a manager with the same fail-closed shape as the gateway's.

States, each entered only from the previous one::

    new -> created -> started -> verified -> ready -> stopped -> removed

 * created   policy-checked create on the proxy network (inert PID 1, fixed entrypoint), the egress network attached, ``inspect`` re-checked
             (exactly {proxy network, egress network}, no secret in the environment);
 * started   the container is running; nothing of ours runs in it yet;
 * verified  the proxy code tar matched its manifest entry by entry, and only then ``python -m aiep.egress_proxy`` was started;
 * ready     a TCP connect to the proxy's own loopback listener succeeded while its process was alive.

The proxy starts BEFORE the gateway and is removed AFTER it (agent, gateway, proxy). It never receives the real key, the run token or any other
secret; ``forbidden_secrets`` makes the policy refuse a create request or an inspect that contains one. Tested with fakes here; the real behaviour is
measured by ``scripts/m2_runtime_validation.py``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .container_exec import REGISTERED_SCRIPTS, ExecSession, inject_tar, list_tree, quiesce, run_exec
from .container_policy import (GATEWAY_ENTRYPOINT, GATEWAY_KEEPALIVE_ARGS, PROXY_COMMAND_TAIL, PROXY_LOG_PATH, PROXY_PORT, READY_SCRIPT,
                               ExecContext, PolicyViolation, ProxyContext, harden, harden_exec, proxy_tmpfs, verify_inspect)
from .gateway_runner import GatewayImagePin
from .seed import Seed, build_component_archive, verify_archive, verify_listing

PROXY_DEST = "/opt/aiep-proxy"
PROXY_FILES = ("src/aiep/__init__.py", "src/aiep/egress_proxy.py", "src/aiep/listen.py", "config/experiment.json")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ProxyConfigError(RuntimeError):
    """``config/proxy.json`` is unusable, or its identity collides with another container's."""


class ProxyStartError(RuntimeError):
    """The proxy could not be brought to the verified-and-ready state."""


class ProxyIntegrityError(RuntimeError):
    """The injected proxy files are not exactly the manifest; the proxy was NOT started."""


@dataclass(frozen=True)
class ProxyPin:
    uid: int
    gid: int
    port: int


def load_proxy_pin(path: Path, *, gateway_pin: GatewayImagePin, agent_uid: int | None = None) -> ProxyPin:
    """The proxy's own identity. It must differ from the gateway's and (when given) the agent's: no two roles share a uid or a gid."""
    cfg = json.loads(Path(path).read_text())
    if cfg.get("status") != "verified":
        raise ProxyConfigError(f"proxy config status is {cfg.get('status')!r}, not 'verified'")
    uid, gid, port = cfg.get("uid"), cfg.get("gid"), cfg.get("port")
    if not (type(uid) is int and type(gid) is int and uid > 0 and gid > 0):
        raise ProxyConfigError("the proxy needs an explicit non-root uid and gid")
    if port != PROXY_PORT:
        raise ProxyConfigError(f"the proxy port must be {PROXY_PORT}")
    taken = {gateway_pin.uid, gateway_pin.gid} | ({agent_uid} if agent_uid is not None else set())
    if uid in taken or gid in taken:
        raise ProxyConfigError("the proxy's uid/gid must differ from the gateway's and the agent's")
    return ProxyPin(uid, gid, port)


def proxy_context(gateway_pin: GatewayImagePin, pin: ProxyPin, *, proxy_network: str, egress_network: str,
                  proxy_cidr: str | None = None, forbidden_secrets: tuple[str, ...] = ()) -> ProxyContext:
    return ProxyContext(proxy_network=proxy_network, egress_network=egress_network, image_id=gateway_pin.image_id, python=gateway_pin.python,
                        uid=pin.uid, gid=pin.gid, extra_tmpfs=gateway_pin.declared_volumes, proxy_cidr=proxy_cidr,
                        forbidden_secrets=forbidden_secrets)


def build_proxy_component(repo_root: Path) -> Seed:
    """The proxy's injection tar plus its own manifest (modes 0644, directories 0755). It contains no secret and no gateway code."""
    root = Path(repo_root)
    return build_component_archive({rel: ((root / rel).read_bytes(), 0o644) for rel in PROXY_FILES})


def proxy_exec_context(ctx: ProxyContext, *, readable_paths: frozenset[str] = frozenset()) -> ExecContext:
    return ExecContext(user=ctx.user, allowed_scripts=REGISTERED_SCRIPTS, readable_paths=readable_paths,
                       forbidden_secrets=ctx.forbidden_secrets, python=ctx.python, may_start_proxy=True,
                       env={"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})


@dataclass(frozen=True)
class ProxySpec:
    run_id: str
    image: str
    ctx: ProxyContext
    code: Seed = field(repr=False)
    port: int = PROXY_PORT
    ready_timeout_s: float = 30.0
    start_timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id must be a plain identifier")
        if self.port != PROXY_PORT:
            raise ValueError(f"the proxy port is fixed at {PROXY_PORT}")

    @property
    def name(self) -> str:
        return f"aiep.proxy.{self.run_id}"

    @property
    def endpoint(self) -> str:
        """What the gateway is told (``AIEP_UPSTREAM_PROXY``)."""
        return f"{self.name}:{self.port}"


def build_proxy_create_kwargs(spec: ProxySpec, *, preflight: bool = False) -> dict[str, Any]:
    """Policy-checked kwargs for ``containers.create``. ``preflight`` (no Docker, no subnet yet) substitutes a placeholder network."""
    import dataclasses
    from .container_policy import PREFLIGHT_CIDR
    ctx = dataclasses.replace(spec.ctx, proxy_cidr=PREFLIGHT_CIDR) if (preflight and spec.ctx.proxy_cidr is None) else spec.ctx
    cidr = ctx.proxy_cidr
    if cidr is None:
        raise PolicyViolation("proxy: AIEP_PROXY_LISTEN: the intended internal network's subnet has not been resolved from Docker")
    env = {"AIEP_PROXY_LISTEN": f"net:{cidr}:{spec.port}", "AIEP_PROXY_LOG": PROXY_LOG_PATH, "PYTHONPATH": f"{PROXY_DEST}/src",
           "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC", "HOME": "/tmp"}
    raw = dict(image=spec.image, command=list(GATEWAY_KEEPALIVE_ARGS), entrypoint=list(GATEWAY_ENTRYPOINT), name=spec.name, user=ctx.user,
               detach=True, environment=env, network=ctx.proxy_network, labels={"aiep.run_id": spec.run_id}, read_only=True,
               tmpfs=proxy_tmpfs(ctx))
    return harden("proxy", raw, ctx)


class ProxyManager:
    """One proxy container, driven step by step. Every failure leaves it in a state ``teardown`` can clean up."""

    STATES = ("new", "created", "started", "verified", "ready", "stopped", "removed")

    def __init__(self, client: Any, spec: ProxySpec, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.client, self.spec, self.clock, self.sleep = client, spec, clock, sleep
        self.state = "new"
        self.container: Any = None
        self.session: ExecSession | None = None
        self.ectx = proxy_exec_context(spec.ctx, readable_paths=frozenset({PROXY_LOG_PATH}))
        self.errors: list[str] = []

    def _need(self, *states: str) -> None:
        if self.state not in states:
            raise ProxyStartError(f"proxy is in state {self.state!r}; this step needs {' or '.join(states)}")

    @property
    def api(self) -> Any:
        return self.client.api

    def create(self) -> None:
        self._need("new")
        kwargs = build_proxy_create_kwargs(self.spec)
        image, command = kwargs.pop("image"), kwargs.pop("command")
        self.container = self.client.containers.create(image, command, **kwargs)
        self.state = "created"  # from here on a container exists and must be torn down
        self.client.networks.get(self.spec.ctx.egress_network).connect(self.container)
        verify_inspect("proxy", self.api.inspect_container(self.container.id), self.spec.ctx)

    def start(self) -> None:
        self._need("created")
        self.container.start()
        t0 = self.clock()
        while True:
            self.container.reload()
            if self.container.status == "running":
                break
            if self.container.status in ("exited", "dead") or self.clock() - t0 >= self.spec.start_timeout_s:
                raise ProxyStartError(f"proxy container did not reach running (status {self.container.status!r})")
            self.sleep(0.1)
        self.state = "started"

    def inject_verify_start(self) -> None:
        """Inject, verify against the manifest, and only then start the proxy process."""
        self._need("started")
        ctx, ectx, code = self.spec.ctx, self.ectx, self.spec.code
        verify_archive(code)
        inject_tar(self.api, self.container.id, code.tar, PROXY_DEST, ctx=ectx)
        problems = verify_listing(code.manifest, list_tree(self.api, self.container.id, PROXY_DEST, ctx=ectx), expect_owner=(ctx.uid, ctx.gid))
        if problems:
            raise ProxyIntegrityError(f"{PROXY_DEST} does not match its manifest: {'; '.join(problems[:5])}")
        self.session = self._start_process()  # only after the files matched their manifest
        self.state = "verified"

    def _start_process(self) -> ExecSession:
        """The production start. An integration test may override ONLY this (to start the process through its Python-only test seam)."""
        ctx = self.spec.ctx
        request = harden_exec([ctx.python, *PROXY_COMMAND_TAIL], user=ctx.user, workdir="/", env={}, ctx=self.ectx)
        return ExecSession(self.api, self.container.id, request).start()

    def wait_ready(self) -> None:
        self._need("verified")
        t0 = self.clock()
        argv = [self.spec.ctx.python, "-I", "-c", READY_SCRIPT, f"127.0.0.1:{self.spec.port}"]
        while True:
            if self.session is not None and self.session.done():
                out = self.session.outcome(timed_out=True)
                raise ProxyStartError(f"the proxy process exited before it was ready; stderr: {out.stderr[-300:].decode('utf-8', 'replace')!r}")
            probe = run_exec(self.api, self.container.id, argv, ctx=self.ectx, workdir="/", timeout_s=5)
            if probe.exit_code == 0 and not probe.timed_out:
                self.state = "ready"
                return
            if self.clock() - t0 >= self.spec.ready_timeout_s:
                raise ProxyStartError("the proxy did not accept connections before the readiness deadline")
            self.sleep(0.2)

    def setup(self) -> None:
        """create, start, verify, ready. Raises on any failure; ``teardown`` must still be called."""
        self.create()
        self.start()
        self.inject_verify_start()
        self.wait_ready()

    def alive(self) -> bool:
        if self.session is not None and self.session.done():
            return False
        try:
            self.container.reload()
            return self.container.status not in ("exited", "dead")
        except Exception:
            return False

    def stop(self) -> None:
        """Kill every process of the proxy's user (PID 1 is immune) and close the exec socket."""
        if self.session is not None:
            self.session.close()
        if self.state not in ("started", "verified", "ready"):
            return
        try:
            quiesce(self.api, self.container.id, ctx=self.ectx)
        except Exception as e:
            self.errors.append(f"proxy stop: {type(e).__name__}: {e}")
        self.state = "stopped"

    def teardown(self) -> tuple[bool, str | None]:
        """Always kill and remove (with anonymous volumes). Returns (removed, error); ordinary errors are reported, never raised."""
        if self.session is not None:
            self.session.close()
        if self.container is None:
            return True, None
        removed, error = False, None
        try:
            try:
                self.container.kill()
            except Exception:
                pass
        finally:
            try:
                self.container.remove(force=True, v=True)
                self.state, removed = "removed", True
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
        return removed, error
